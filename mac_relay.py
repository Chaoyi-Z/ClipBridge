#!/usr/bin/env python3
"""ClipBridge Mac relay - mirrors the ClipBridge (Windows PC) clipboard into the Mac pasteboard,
and the Mac pasteboard back to the PC.

Why: iOS lets only Apple's Universal Clipboard change the iPhone clipboard in the background.
When this relay puts every new PC copy into the Mac pasteboard, Universal Clipboard offers it
to the iPhone/iPad next to the Mac automatically: copy on the PC, paste anywhere on the iPhone,
no tap, no Shortcut, as long as the Mac is awake and nearby.

PC -> Mac : long-polls the ClipBridge server; every new PC item (text or image) is written to
            the Mac pasteboard (text as a plain string, images as PNG + TIFF so every app and
            Universal Clipboard accept them).
Mac -> PC : unless --no-push, every new Mac copy is POSTed to the PC clipboard (source "mac").

Needs only Python 3. Uses PyObjC's AppKit when it is importable (the python3 that comes with
Xcode / the Command Line Tools has it), otherwise falls back to `osascript` running a small
JavaScript-for-Automation helper that reaches the same NSPasteboard API. No pip packages.

Usage:
    python3 mac_relay.py --url http://100.81.162.90:48765 --token KEY
    python3 mac_relay.py --url "http://100.81.162.90:48765/?t=KEY"      (pairing link works too)
    CLIPBRIDGE_URL=... CLIPBRIDGE_TOKEN=... python3 mac_relay.py
    python3 mac_relay.py --url ... --token ... --check                   test and exit

Options:
    --no-push        one-directional: PC -> Mac only
    --poll SECONDS   how often to look at the Mac pasteboard (default 0.5; 1.0 with osascript)
    --pull-remote    also forward copies the iPhone/iPad advertises to the Mac (Universal
                     Clipboard) on to the PC. Experimental: each iPhone copy then triggers a
                     "Pasting from iPhone" transfer on the Mac.
    --backend auto|appkit|osascript
    -v / --verbose   debug log
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import os
import platform
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import parse_qs, unquote, urlparse

VERSION = "1.0"
SOURCE = "mac"
ENV_URL, ENV_TOKEN = "CLIPBRIDGE_URL", "CLIPBRIDGE_TOKEN"

PNG_SIG = b"\x89PNG\r\n\x1a\n"
# Pasteboard UTIs (the values behind NSPasteboardTypeString / PNG / TIFF; literals avoid bridging issues)
TYPE_STRING = "public.utf8-plain-text"
TYPE_PNG = "public.png"
TYPE_TIFF = "public.tiff"
TYPE_FILE_URL = "public.file-url"
TYPE_REMOTE = "com.apple.is-remote-clipboard"   # content promised by another device (Universal Clipboard)
NS_BITMAP_PNG = 4                                # NSBitmapImageFileTypePNG

MAX_IMAGE_BYTES = 60 * 1024 * 1024               # ClipBridge refuses bigger uploads
MAX_TEXT = 2 * 1024 * 1024
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic")
NET_ERRORS = (OSError, http.client.HTTPException)  # URLError/timeout are OSError subclasses

LOG = logging.getLogger("clipbridge-relay")


# --------------------------------------------------------------------------- helpers

def content_hash(kind: str, data) -> str:
    raw = data.encode("utf-8") if kind == "text" else bytes(data)
    return hashlib.sha1(raw).hexdigest()


def png_size(png: bytes) -> tuple[int, int]:
    if len(png) >= 24 and png[:8] == PNG_SIG and png[12:16] == b"IHDR":
        w, h = struct.unpack(">II", png[16:24])
        return int(w), int(h)
    return 0, 0


def fmt_size(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    n /= 1024
    if n < 1024:
        return f"{n:.1f} KB"
    return f"{n / 1024:.1f} MB"


def describe(kind: str, data) -> str:
    if kind == "text":
        preview = " ".join(data.split())
        if len(preview) > 48:
            preview = preview[:45] + "..."
        return f"text, {len(data):,} chars: {preview!r}"
    w, h = png_size(data)
    return f"image {w}x{h}, {fmt_size(len(data))}"


def short_err(e: BaseException) -> str:
    s = str(e) or e.__class__.__name__
    return s if len(s) < 160 else s[:157] + "..."


# --------------------------------------------------------------------------- pasteboard backends

class Pasteboard:
    """The macOS general pasteboard. read() -> None | ('text', str) | ('image', png_bytes) | ('remote', None)."""
    name = "none"

    def change_count(self) -> int:
        raise NotImplementedError

    def read(self, allow_remote: bool = False):
        raise NotImplementedError

    def write_text(self, text: str) -> int:
        """Put text on the pasteboard; return the resulting changeCount."""
        raise NotImplementedError

    def write_image(self, png: bytes) -> int:
        raise NotImplementedError


class AppKitPasteboard(Pasteboard):
    """NSPasteboard through PyObjC."""
    name = "AppKit (PyObjC)"

    def __init__(self) -> None:
        from AppKit import NSBitmapImageRep, NSImage, NSPasteboard  # type: ignore
        from Foundation import NSData  # type: ignore
        self._NSImage, self._NSBitmapImageRep, self._NSData = NSImage, NSBitmapImageRep, NSData
        self._pb = NSPasteboard.generalPasteboard()
        self._pb.changeCount()  # smoke test

    @staticmethod
    def _bytes(nsdata) -> bytes:
        if nsdata is None:
            return b""
        try:
            return bytes(nsdata)
        except Exception:
            return bytes(nsdata.bytes()[: nsdata.length()])

    def change_count(self) -> int:
        return int(self._pb.changeCount())

    def _types(self) -> list[str]:
        return [str(t) for t in (self._pb.types() or [])]

    def _png_from_tiff(self, tiff) -> bytes:
        if tiff is None or not tiff.length():
            return b""
        rep = self._NSBitmapImageRep.imageRepWithData_(tiff)
        if rep is None:
            return b""
        return self._bytes(rep.representationUsingType_properties_(NS_BITMAP_PNG, {}))

    def _png_from_image_data(self, data: bytes) -> bytes:
        nsd = self._NSData.dataWithBytes_length_(data, len(data))
        img = self._NSImage.alloc().initWithData_(nsd)
        if img is None:
            return b""
        return self._png_from_tiff(img.TIFFRepresentation())

    def _image_from_file_url(self):
        """Finder 'Copy' of an image file -> ('image', png) ; a non-image file -> None."""
        url = self._pb.propertyListForType_(TYPE_FILE_URL)
        if not url:
            return None
        path = unquote(urlparse(str(url)).path)
        if not path.lower().endswith(IMG_EXTS) or not os.path.isfile(path):
            return None
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            LOG.info("skipping %s: larger than %s", os.path.basename(path), fmt_size(MAX_IMAGE_BYTES))
            return None
        with open(path, "rb") as f:
            data = f.read()
        png = data if data[:8] == PNG_SIG else self._png_from_image_data(data)
        return ("image", png) if png else None

    def read(self, allow_remote: bool = False):
        types = self._types()
        if not types:
            return None
        if TYPE_REMOTE in types and not allow_remote:
            return ("remote", None)
        if TYPE_FILE_URL in types:
            return self._image_from_file_url()    # file copies: only image files are relayed
        if TYPE_STRING in types:
            s = self._pb.stringForType_(TYPE_STRING)
            if s is not None and str(s).strip():
                return ("text", str(s))
        png = b""
        if TYPE_PNG in types:
            png = self._bytes(self._pb.dataForType_(TYPE_PNG))
            if png[:8] != PNG_SIG:
                png = b""
        if not png and TYPE_TIFF in types:
            png = self._png_from_tiff(self._pb.dataForType_(TYPE_TIFF))
        return ("image", png) if png else None

    def write_text(self, text: str) -> int:
        self._pb.clearContents()
        self._pb.setString_forType_(text, TYPE_STRING)
        return self.change_count()

    def write_image(self, png: bytes) -> int:
        nsd = self._NSData.dataWithBytes_length_(png, len(png))
        img = self._NSImage.alloc().initWithData_(nsd)
        tiff = img.TIFFRepresentation() if img is not None else None
        types = [TYPE_PNG] + ([TYPE_TIFF] if tiff is not None and tiff.length() else [])
        self._pb.clearContents()
        self._pb.declareTypes_owner_(types, None)
        self._pb.setData_forType_(nsd, TYPE_PNG)
        if TYPE_TIFF in types:
            self._pb.setData_forType_(tiff, TYPE_TIFF)
        return self.change_count()


# JavaScript for Automation helper: the same NSPasteboard calls, run by /usr/bin/osascript.
JXA_HELPER = r"""
ObjC.import('AppKit');
var PNG_TYPE = 4;  // NSBitmapImageFileTypePNG
var UTF8 = 4;      // NSUTF8StringEncoding
var IMG_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff', '.heic'];
function endsWithAny(s, exts) {
  for (var i = 0; i < exts.length; i++) if (s.slice(-exts[i].length) === exts[i]) return true;
  return false;
}
function pngFromTIFF(tiff) {
  if (!tiff || tiff.isNil() || !tiff.length) return $();
  var rep = $.NSBitmapImageRep.imageRepWithData(tiff);
  if (rep.isNil()) return $();
  return rep.representationUsingTypeProperties(PNG_TYPE, $.NSDictionary.dictionary);
}
function pngFromImageData(data) {
  var img = $.NSImage.alloc.initWithData(data);
  if (img.isNil()) return $();
  return pngFromTIFF(img.TIFFRepresentation);
}
function run(argv) {
  var pb = $.NSPasteboard.generalPasteboard;
  var cmd = argv[0];
  if (cmd === 'count') return String(pb.changeCount);
  if (cmd === 'read') {
    var out = argv[1], allowRemote = argv[2] === '1';
    var types = ObjC.deepUnwrap(pb.types) || [];
    if (!types.length) return 'none';
    if (types.indexOf('com.apple.is-remote-clipboard') >= 0 && !allowRemote) return 'remote';
    if (types.indexOf('public.file-url') >= 0) {
      var u = pb.propertyListForType('public.file-url');
      if (u.isNil()) return 'none';
      var p = ObjC.unwrap($.NSURL.URLWithString(u).path) || '';
      var lower = p.toLowerCase();
      if (!endsWithAny(lower, IMG_EXTS)) return 'none';
      var fd = $.NSData.dataWithContentsOfFile(p);
      if (fd.isNil()) return 'none';
      var fpng = lower.slice(-4) === '.png' ? fd : pngFromImageData(fd);
      if (fpng.isNil()) return 'none';
      fpng.writeToFileAtomically(out, true);
      return 'image';
    }
    if (types.indexOf('public.utf8-plain-text') >= 0) {
      var s = pb.stringForType('public.utf8-plain-text');
      if (!s.isNil() && ObjC.unwrap(s).trim().length) {
        s.writeToFileAtomicallyEncodingError(out, true, UTF8, null);
        return 'text';
      }
    }
    var data = $();
    if (types.indexOf('public.png') >= 0) data = pb.dataForType('public.png');
    if (data.isNil() && types.indexOf('public.tiff') >= 0) data = pngFromTIFF(pb.dataForType('public.tiff'));
    if (!data.isNil() && data.length > 0) { data.writeToFileAtomically(out, true); return 'image'; }
    return 'none';
  }
  if (cmd === 'write-text') {
    var txt = $.NSString.stringWithContentsOfFileEncodingError(argv[1], UTF8, null);
    pb.clearContents;
    pb.setStringForType(txt, 'public.utf8-plain-text');
    return String(pb.changeCount);
  }
  if (cmd === 'write-image') {
    var pngData = $.NSData.dataWithContentsOfFile(argv[1]);
    var img = $.NSImage.alloc.initWithData(pngData);
    var tiff = img.isNil() ? $() : img.TIFFRepresentation;
    var ts = ['public.png'];
    if (!tiff.isNil()) ts.push('public.tiff');
    pb.clearContents;
    pb.declareTypesOwner(ts, $());
    pb.setDataForType(pngData, 'public.png');
    if (!tiff.isNil()) pb.setDataForType(tiff, 'public.tiff');
    return String(pb.changeCount);
  }
  throw new Error('unknown command ' + cmd);
}
"""


class OsascriptPasteboard(Pasteboard):
    """NSPasteboard through `osascript -l JavaScript` (no Python packages needed; ~0.1 s per call)."""
    name = "osascript (JavaScript for Automation)"

    def __init__(self) -> None:
        if not shutil.which("osascript"):
            raise RuntimeError("osascript not found (is this macOS?)")
        self._dir = tempfile.mkdtemp(prefix="clipbridge-relay-")
        os.chmod(self._dir, 0o700)
        self._helper = os.path.join(self._dir, "pasteboard.js")
        with open(self._helper, "w", encoding="utf-8") as f:
            f.write(JXA_HELPER)
        self._in = os.path.join(self._dir, "in.bin")
        self._out = os.path.join(self._dir, "out.bin")
        self.change_count()  # smoke test

    def _run(self, *args: str) -> str:
        p = subprocess.run(["osascript", "-l", "JavaScript", self._helper, *args],
                           capture_output=True, text=True, timeout=180)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip() or f"osascript exit {p.returncode}")
        return p.stdout.strip()

    def change_count(self) -> int:
        return int(self._run("count"))

    def read(self, allow_remote: bool = False):
        kind = self._run("read", self._out, "1" if allow_remote else "0")
        if kind == "text":
            with open(self._out, encoding="utf-8", errors="replace") as f:
                return ("text", f.read())
        if kind == "image":
            with open(self._out, "rb") as f:
                return ("image", f.read())
        if kind == "remote":
            return ("remote", None)
        return None

    def write_text(self, text: str) -> int:
        with open(self._in, "w", encoding="utf-8") as f:
            f.write(text)
        return int(self._run("write-text", self._in))

    def write_image(self, png: bytes) -> int:
        with open(self._in, "wb") as f:
            f.write(png)
        return int(self._run("write-image", self._in))


def make_pasteboard(backend: str = "auto") -> Pasteboard:
    if platform.system() != "Darwin":
        raise RuntimeError(f"this relay runs on macOS (this is {platform.system() or 'an unknown OS'})")
    errors = []
    if backend in ("auto", "appkit"):
        try:
            return AppKitPasteboard()
        except Exception as e:  # ImportError, or anything odd in the bridge
            errors.append(f"AppKit: {short_err(e)}")
            if backend == "appkit":
                raise RuntimeError("; ".join(errors))
    if backend in ("auto", "osascript"):
        try:
            return OsascriptPasteboard()
        except Exception as e:
            errors.append(f"osascript: {short_err(e)}")
    raise RuntimeError("no usable pasteboard backend (" + "; ".join(errors) + ")")


# --------------------------------------------------------------------------- ClipBridge client

class AuthError(Exception):
    pass


class Rejected(Exception):
    """The PC understood the request and said no (4xx) - retrying will not help."""


class Client:
    def __init__(self, url: str, token: str, timeout: float = 15.0) -> None:
        self.base = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None,
                 timeout: float | None = None) -> bytes:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("X-Token", self.token)
        req.add_header("User-Agent", f"ClipBridge-MacRelay/{VERSION}")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read() or b"{}").get("error") or ""
            except Exception:
                pass
            if e.code == 401:
                raise AuthError("the PC rejected the pairing key (HTTP 401)") from None
            if 400 <= e.code < 500:
                raise Rejected(f"HTTP {e.code} {detail}".strip()) from None
            raise

    def info(self) -> dict:
        return json.loads(self._request("GET", "/api/info"))

    def state(self, since: int, wait: bool) -> dict:
        path = f"/api/state?since={int(since)}" + ("&wait=1" if wait else "")
        return json.loads(self._request("GET", path, timeout=45 if wait else None))

    def item_png(self, url: str) -> bytes:
        if not url.startswith("/"):
            url = "/" + url
        return self._request("GET", url, timeout=90)

    def push(self, kind: str, data) -> dict:
        if kind == "text":
            body, ctype = data.encode("utf-8"), "text/plain; charset=utf-8"
        else:
            body, ctype = bytes(data), "image/png"
        return json.loads(self._request("POST", f"/api/push?source={SOURCE}", body, ctype, timeout=120))


# --------------------------------------------------------------------------- relay

class Relay:
    def __init__(self, client: Client, pb: Pasteboard, push: bool = True, poll: float = 0.5,
                 pull_remote: bool = False) -> None:
        self.client = client
        self.pb = pb
        self.push_enabled = push
        self.poll = max(0.1, float(poll))
        self.pull_remote = pull_remote
        self.stop = threading.Event()
        self.lock = threading.RLock()        # serialises pasteboard access between the two directions
        self.own_counts: deque = deque(maxlen=16)   # changeCounts produced by our own writes
        self.last_pc_hash: str | None = None  # last content written PC -> Mac
        self.last_mac_hash: str | None = None  # last content pushed Mac -> PC
        self.version = -1                     # server version we have seen
        self.last_ts = 0.0                    # newest server item timestamp we have handled
        self.last_id: str | None = None
        self.synced = threading.Event()       # set once the first /api/state answer arrived
        self.stats = {"pc_to_mac": 0, "mac_to_pc": 0}
        self._remote_noted = False

    # ---- PC -> Mac --------------------------------------------------------
    def _pull_loop(self) -> None:
        delay = 1.0
        while not self.stop.is_set():
            try:
                first = self.version < 0
                st = self.client.state(self.version, wait=not first)
                if not isinstance(st, dict) or "version" not in st:
                    raise ValueError("unexpected answer from the PC")
                version = int(st["version"])
                items = st.get("items") or []
                if first:
                    self.version = version
                    if items:
                        self.last_ts = float(items[0].get("ts") or 0)
                        self.last_id = items[0].get("id")
                    LOG.info("connected to the PC (%d items in its history); new PC copies will appear on this Mac",
                             len(items))
                    self.synced.set()
                    delay = 1.0
                    continue
                if version < self.version:
                    LOG.info("ClipBridge on the PC restarted")
                self.version = version
                self._handle_newest(items)
                delay = 1.0
            except AuthError as e:
                LOG.error("%s - copy the current pairing link from http://localhost:48765/ on the PC "
                          "and reinstall with the new key; retrying in 60 s", e)
                self.stop.wait(60)
            except NET_ERRORS as e:
                LOG.warning("PC not reachable (%s); retrying in %.0f s", short_err(e), delay)
                self.stop.wait(delay)
                delay = min(delay * 2, 30.0)
            except Exception as e:
                LOG.exception("PC -> Mac loop: %s", short_err(e))
                self.stop.wait(delay)
                delay = min(delay * 2, 30.0)

    def _handle_newest(self, items: list) -> None:
        if not items:
            return
        it = items[0] if isinstance(items[0], dict) else {}
        iid, ts = it.get("id"), float(it.get("ts") or 0)
        if iid == self.last_id:
            return
        self.last_id = iid
        if ts <= self.last_ts:
            return                      # an older item moved to the top (the newest was deleted)
        self.last_ts = ts
        if it.get("source") == SOURCE:
            return                      # our own Mac -> PC push coming back
        kind = it.get("type")
        if kind == "text":
            data = it.get("text") or ""
            if not isinstance(data, str) or not data.strip():
                return
        elif kind == "image":
            try:
                data = self.client.item_png(str(it.get("url") or f"/api/item/{iid}.png"))
            except Exception as e:
                LOG.warning("could not download image %s from the PC: %s", iid, short_err(e))
                return
            if data[:8] != PNG_SIG:
                LOG.warning("item %s from the PC is not a PNG; skipped", iid)
                return
        else:
            return
        h = content_hash(kind, data)
        with self.lock:
            if h in (self.last_pc_hash, self.last_mac_hash):
                return                  # already on this pasteboard (or it came from here)
            try:
                count = self.pb.write_text(data) if kind == "text" else self.pb.write_image(data)
            except Exception as e:
                LOG.warning("could not write to the Mac pasteboard: %s", short_err(e))
                return
            self.own_counts.append(int(count))
            self.last_pc_hash = h
        self.stats["pc_to_mac"] += 1
        LOG.info("PC -> Mac: %s", describe(kind, data))

    # ---- Mac -> PC --------------------------------------------------------
    def _push_loop(self) -> None:
        last: int | None = None
        pending = None          # (kind, data, hash) waiting to reach the PC
        retry_at, backoff = 0.0, 2.0
        while not self.stop.is_set():
            self.stop.wait(self.poll)
            if self.stop.is_set():
                break
            try:
                with self.lock:
                    count = self.pb.change_count()
                if last is None:
                    last = count            # do not push whatever was on the pasteboard already
                    continue
                if count != last:
                    last = count
                    with self.lock:
                        if count in self.own_counts:
                            continue        # that was us
                    time.sleep(0.15)        # the copying app may still be adding formats
                    with self.lock:
                        last = self.pb.change_count()
                        if last in self.own_counts:
                            continue
                        res = self.pb.read(self.pull_remote)
                    if not res:
                        continue
                    kind, data = res
                    if kind == "remote":
                        if not self._remote_noted:
                            LOG.info("an iPhone/iPad copy is advertised to this Mac; not forwarded to the PC "
                                     "(start with --pull-remote to relay those too)")
                            self._remote_noted = True
                        continue
                    if kind == "image" and len(data) > MAX_IMAGE_BYTES:
                        LOG.info("Mac image of %s skipped (larger than %s)",
                                 fmt_size(len(data)), fmt_size(MAX_IMAGE_BYTES))
                        continue
                    if kind == "text" and len(data) > MAX_TEXT:
                        data = data[:MAX_TEXT]
                    h = content_hash(kind, data)
                    with self.lock:
                        if h in (self.last_pc_hash, self.last_mac_hash):
                            continue        # it came from the PC, or we already sent it
                    pending, retry_at, backoff = (kind, data, h), 0.0, 2.0
                if pending and time.time() >= retry_at:
                    kind, data, h = pending
                    try:
                        self.client.push(kind, data)
                    except AuthError as e:
                        LOG.error("%s; will retry in 60 s", e)
                        retry_at = time.time() + 60
                        continue
                    except Rejected as e:
                        LOG.warning("the PC refused the item (%s); dropped", e)
                        pending = None
                        continue
                    except NET_ERRORS as e:
                        LOG.warning("could not send to the PC (%s); retrying in %.0f s", short_err(e), backoff)
                        retry_at = time.time() + backoff
                        backoff = min(backoff * 2, 30.0)
                        continue
                    with self.lock:
                        self.last_mac_hash = h
                    self.stats["mac_to_pc"] += 1
                    LOG.info("Mac -> PC: %s", describe(kind, data))
                    pending = None
            except Exception as e:
                LOG.exception("Mac -> PC loop: %s", short_err(e))
                self.stop.wait(2)

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._pull_loop, name="pc-to-mac", daemon=True).start()
        if self.push_enabled:
            threading.Thread(target=self._push_loop, name="mac-to-pc", daemon=True).start()

    def run(self) -> None:
        self.start()
        while not self.stop.is_set():
            self.stop.wait(1)


# --------------------------------------------------------------------------- main

def split_pairing_link(url: str | None, token: str | None) -> tuple[str | None, str | None]:
    """Accept the pairing link (http://host:port/?t=KEY) as --url and take the key from it."""
    if not url:
        return url, token
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    u = urlparse(url)
    if not token:
        token = (parse_qs(u.query).get("t") or [None])[0]
    return f"{u.scheme}://{u.netloc}", (token.strip() if token else token)


def usage_hint() -> str:
    return (
        "ClipBridge Mac relay needs the PC's address and its pairing key:\n"
        "    python3 mac_relay.py --url http://100.81.162.90:48765 --token KEY\n"
        "or  python3 mac_relay.py --url \"http://100.81.162.90:48765/?t=KEY\"\n"
        "(the key is the t=... part of the pairing link shown at http://localhost:48765/ on the PC;\n"
        f" the environment variables {ENV_URL} and {ENV_TOKEN} work too)"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Mirror the ClipBridge PC clipboard into the Mac pasteboard and back.")
    ap.add_argument("--url", default=os.environ.get(ENV_URL),
                    help="ClipBridge address, e.g. http://100.81.162.90:48765 (the pairing link works too)")
    ap.add_argument("--token", default=os.environ.get(ENV_TOKEN), help="pairing key (t=... in the pairing link)")
    ap.add_argument("--no-push", action="store_true", help="one-directional: PC -> Mac only")
    ap.add_argument("--poll", type=float, default=0.5, help="seconds between looks at the Mac pasteboard (default 0.5)")
    ap.add_argument("--pull-remote", action="store_true",
                    help="also forward iPhone/iPad copies advertised to this Mac (Universal Clipboard) to the PC")
    ap.add_argument("--backend", choices=("auto", "appkit", "osascript"), default="auto")
    ap.add_argument("--check", action="store_true", help="test the PC connection and the pasteboard, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"ClipBridge Mac relay {VERSION}")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        stream=sys.stdout)
    url, token = split_pairing_link(args.url, args.token)
    if not url or not token:
        print(usage_hint(), file=sys.stderr)
        return 2
    client = Client(url, token)

    if args.check:
        ok = True
        try:
            info = client.info()
            print(f"PC reachable: {url}  ({info.get('name', '?')}, {len(info.get('urls') or [])} pairing links)")
        except AuthError as e:
            print(f"PC reachable but {e}", file=sys.stderr)
            return 3
        except Exception as e:
            print(f"PC not reachable at {url}: {short_err(e)}\n"
                  "  - is ClipBridge running on the PC?  - is Tailscale connected on both machines?", file=sys.stderr)
            ok = False
        try:
            pb = make_pasteboard(args.backend)
            print(f"Pasteboard: {pb.name}, changeCount {pb.change_count()}")
        except Exception as e:
            print(f"Pasteboard not usable: {short_err(e)}", file=sys.stderr)
            return 5
        return 0 if ok else 4

    try:
        pb = make_pasteboard(args.backend)
    except Exception as e:
        LOG.error("cannot access the Mac pasteboard: %s", short_err(e))
        return 5
    poll = args.poll
    if isinstance(pb, OsascriptPasteboard) and poll < 1.0:
        poll = 1.0
        LOG.info("osascript backend: polling the pasteboard every 1 s (install PyObjC for 0.5 s: "
                 "pip3 install --user pyobjc-framework-Cocoa)")
    LOG.info("ClipBridge Mac relay %s - PC: %s - pasteboard: %s - Mac -> PC: %s%s", VERSION, url, pb.name,
             "off (--no-push)" if args.no_push else "on",
             " - iPhone copies forwarded (--pull-remote)" if args.pull_remote else "")
    try:
        info = client.info()
        LOG.info("PC '%s' answered", info.get("name", "?"))
    except AuthError as e:
        LOG.error("%s. Copy the current pairing link from http://localhost:48765/ on the PC and start again "
                  "with its key. Waiting and retrying meanwhile.", e)
    except Exception as e:
        LOG.warning("PC not reachable yet at %s (%s). Is ClipBridge running on the PC and Tailscale connected "
                    "on both machines? Retrying in the background.", url, short_err(e))

    relay = Relay(client, pb, push=not args.no_push, poll=poll, pull_remote=args.pull_remote)

    def _term(signum, frame):  # launchctl bootout sends SIGTERM
        relay.stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _term)
        except Exception:
            pass
    try:
        relay.run()
    except KeyboardInterrupt:
        relay.stop.set()
    LOG.info("stopped (PC -> Mac: %d, Mac -> PC: %d)", relay.stats["pc_to_mac"], relay.stats["mac_to_pc"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
