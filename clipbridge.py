#!/usr/bin/env python3
"""ClipBridge - share clipboard text and images between this Windows PC and your phone.

The PC runs this script. It watches the Windows clipboard and serves a small web app.
Open the link (or scan the QR code) on your iPhone:
  * anything you copy on the PC shows up on the phone (tap "Copy" or long-press an image)
  * anything you paste on the phone page is put straight into the PC clipboard
  * optional: mac_relay.py on a Mac mirrors PC copies into the Mac pasteboard, so Universal
    Clipboard hands them to the iPhone automatically (see README, "Mac relay"); an iPad can do
    the same with a looping Shortcut that polls /api/next (README, "iPad as the hub")

Run:
    python clipbridge.py              tray icon + server (default)
    python clipbridge.py --console    no tray icon, log to the console, print QR code
Options:
    --port N          listen on port N (default 48765, remembered in config)
    --regen-token     make a new pairing token (old phone links stop working)
    --open            open the local page in the browser on start
    --no-tray         same as --console but without the ASCII QR code

Config + log live in %APPDATA%\\ClipBridge\\
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageGrab

try:  # iPhone photos are often HEIC
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

import win32clipboard as wc
import win32con

APP_NAME = "ClipBridge"
DEFAULT_PORT = 48765
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("APPDATA", HERE), APP_NAME)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "clipbridge.log")
INDEX_PATH = os.path.join(HERE, "index.html")
# files the Mac fetches with curl (pairing key required): the relay, its installer, the launchd template
MAC_FILES = {
    "/mac_relay.py": ("mac_relay.py", "text/x-python; charset=utf-8"),
    "/install_mac_relay.sh": ("install_mac_relay.sh", "text/x-shellscript; charset=utf-8"),
    "/com.clipbridge.macrelay.plist": ("com.clipbridge.macrelay.plist", "application/xml; charset=utf-8"),
}

MAX_ITEMS = 60                      # history length
MAX_HISTORY_BYTES = 250 * 1024 * 1024
MAX_BODY = 60 * 1024 * 1024         # largest upload accepted
MAX_TEXT = 2 * 1024 * 1024          # characters of text kept per item
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")
CREATE_NO_WINDOW = 0x08000000

LOG = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- helpers

def to_png(img: Image.Image) -> bytes:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if ("A" in img.mode or img.mode == "P") else "RGB")
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def sniff_image(data: bytes) -> bool:
    return (
        data[:8] == b"\x89PNG\r\n\x1a\n"
        or data[:3] == b"\xff\xd8\xff"
        or data[:6] in (b"GIF87a", b"GIF89a")
        or data[:2] == b"BM"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif")
        or data[:4] in (b"II*\x00", b"MM\x00*")
    )


def make_icon(size: int) -> Image.Image:
    """Blue rounded square with two white arrows (up / down)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.22), fill=(37, 99, 235, 255))
    s = size
    w = s * 0.11          # shaft half width
    hw = s * 0.21         # head half width
    top, bot = s * 0.2, s * 0.8
    head = s * 0.26
    # up arrow (left)
    cx = s * 0.34
    d.rectangle((cx - w, top + head, cx + w, bot), fill="white")
    d.polygon([(cx, top), (cx - hw, top + head), (cx + hw, top + head)], fill="white")
    # down arrow (right)
    cx = s * 0.66
    d.rectangle((cx - w, top, cx + w, bot - head), fill="white")
    d.polygon([(cx, bot), (cx - hw, bot - head), (cx + hw, bot - head)], fill="white")
    return img


def icon_png(size: int) -> bytes:
    return to_png(make_icon(size))


def qr_png(text: str) -> bytes | None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        return None
    q = qrcode.QRCode(box_size=7, border=2)
    q.add_data(text)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white").convert("RGB")
    return to_png(img)


def split_multipart(body: bytes, content_type: str) -> list[tuple[str, str, bytes]]:
    """multipart/form-data -> [(field name, part content type, data)]."""
    from email.parser import BytesParser
    from email.policy import HTTP
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    out = []
    for part in msg.iter_parts() if msg.is_multipart() else []:
        data = part.get_payload(decode=True) or b""
        out.append((part.get_param("name", header="content-disposition") or "", part.get_content_type(), data))
    return out


def rtf_to_text(rtf: str) -> str:
    """Small RTF -> plain text converter (enough for Notes / Pages / TextEdit snippets)."""
    import re
    out, stack, ignorable, uc_skip = [], [], False, 1
    skip_dest = {"fonttbl", "colortbl", "stylesheet", "info", "expandedcolortbl", "listtable",
                 "listoverridetable", "pict", "themedata", "colorschememapping", "latentstyles", "datastore"}
    specials = {"par": "\n", "line": "\n", "tab": "\t", "emdash": "\u2014", "endash": "\u2013",
                "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c", "rdblquote": "\u201d", "bullet": "\u2022"}
    tok = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|[\r\n]+|(.)", re.S)
    skip = 0
    for m in tok.finditer(rtf):
        word, arg, hexc, char, brace, tchar = m.groups()
        if brace == "{":
            stack.append((ignorable, uc_skip))
        elif brace == "}":
            if stack:
                ignorable, uc_skip = stack.pop()
        elif char:
            if char == "*":
                ignorable = True
            elif not ignorable:
                if char in ("\\", "{", "}"):
                    out.append(char)
                elif char == "~":
                    out.append("\u00a0")
                elif char in "\n\r":
                    out.append("\n")
        elif word:
            if word in skip_dest:
                ignorable = True
            elif ignorable:
                pass
            elif word == "uc":
                uc_skip = int(arg or 1)
            elif word == "u":
                c = int(arg)
                out.append(chr(c + 65536 if c < 0 else c))
                skip = uc_skip
            elif word in specials:
                out.append(specials[word])
        elif hexc:
            if skip:
                skip -= 1
            elif not ignorable:
                out.append(bytes([int(hexc, 16)]).decode("cp1252", errors="replace"))
        elif tchar and not ignorable:
            if skip:
                skip -= 1
            else:
                out.append(tchar)
    return "".join(out).strip("\n")


def html_extract(html: str):
    """('image', bytes) for an inline data: image, else ('text', visible text)."""
    import base64, re
    from html.parser import HTMLParser
    m = re.search(r"""<img[^>]+src=["']data:image/[a-z+.-]+;base64,([^"']+)["']""", html, re.I)
    if m:
        try:
            return ("image", base64.b64decode(m.group(1)))
        except Exception:
            pass

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.skip = [], 0
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "head", "noscript", "svg"):
                self.skip += 1
            elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
                self.parts.append("\n")
        def handle_endtag(self, tag):
            if tag in ("script", "style", "head", "noscript", "svg") and self.skip:
                self.skip -= 1
        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)
    p = P()
    p.feed(html)
    text = re.sub(r"[ \t\u00a0]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return ("text", text.strip())


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith(("<!doctype html", "<html", "<meta", "<body", "<div", "<span", "<p>", "<img"))


# --------------------------------------------------------------------------- clipboard

class WinClipboard:
    """Thread-safe access to the Windows clipboard (text + images)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._user32 = ctypes.windll.user32
        self._png_fmt = wc.RegisterClipboardFormat("PNG")
        self.ignore_seq = -1  # sequence number produced by our own writes

    def seq(self) -> int:
        return int(self._user32.GetClipboardSequenceNumber())

    @staticmethod
    def _open(tries: int = 25) -> bool:
        for _ in range(tries):
            try:
                wc.OpenClipboard(0)
                return True
            except Exception:
                time.sleep(0.04)
        return False

    def read(self):
        """Return ('text', str) | ('image', png_bytes) | None."""
        with self.lock:
            if not self._open():
                LOG.warning("clipboard busy, could not read")
                return None
            text = None
            png = None
            has_img = False
            try:
                if wc.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    try:
                        text = wc.GetClipboardData(win32con.CF_UNICODETEXT)
                    except Exception:
                        text = None
                if text and text.strip():
                    return ("text", text[:MAX_TEXT])
                if wc.IsClipboardFormatAvailable(self._png_fmt):
                    try:
                        png = bytes(wc.GetClipboardData(self._png_fmt))
                    except Exception:
                        png = None
                has_img = any(
                    wc.IsClipboardFormatAvailable(f)
                    for f in (win32con.CF_DIB, win32con.CF_DIBV5, win32con.CF_BITMAP, win32con.CF_HDROP)
                )
            finally:
                wc.CloseClipboard()

            if png and png[:8] == b"\x89PNG\r\n\x1a\n":
                return ("image", png)
            if not has_img:
                return None
            try:
                grabbed = ImageGrab.grabclipboard()
            except Exception as e:  # pragma: no cover
                LOG.warning("grabclipboard failed: %s", e)
                return None
            img = None
            if isinstance(grabbed, list):
                for p in grabbed:
                    if str(p).lower().endswith(IMG_EXTS) and os.path.isfile(p):
                        try:
                            img = Image.open(p)
                            img.load()
                            break
                        except Exception:
                            img = None
            elif grabbed is not None:
                img = grabbed
            if img is None:
                return None
            return ("image", to_png(img))

    def set_text(self, text: str) -> None:
        with self.lock:
            if not self._open():
                raise RuntimeError("clipboard is busy")
            try:
                wc.EmptyClipboard()
                wc.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                wc.CloseClipboard()
            self.ignore_seq = self.seq()

    def set_image(self, png: bytes) -> None:
        img = Image.open(BytesIO(png))
        img.load()
        # CF_DIB has no reliable alpha: flatten onto white. PNG format keeps transparency.
        flat = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat.paste(rgba, mask=rgba.split()[3])
        else:
            flat.paste(img.convert("RGB"))
        buf = BytesIO()
        flat.save(buf, "BMP")
        dib = buf.getvalue()[14:]  # strip BITMAPFILEHEADER
        with self.lock:
            if not self._open():
                raise RuntimeError("clipboard is busy")
            try:
                wc.EmptyClipboard()
                wc.SetClipboardData(win32con.CF_DIB, dib)
                wc.SetClipboardData(self._png_fmt, png)
            finally:
                wc.CloseClipboard()
            self.ignore_seq = self.seq()


# --------------------------------------------------------------------------- history store

class Store:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.version = 0
        self.seq = 0                     # ever-increasing item counter (cursor for /api/next)
        self.clients: dict[str, dict] = {}   # per-device state for /api/next (the iPad hub)
        self.cond = threading.Condition()
        self.paused = False

    def _bump(self) -> None:
        self.version += 1
        self.cond.notify_all()

    def add(self, kind: str, data, source: str):
        raw = data.encode("utf-8") if kind == "text" else data
        h = hashlib.sha1(raw).hexdigest()
        with self.cond:
            if self.items and self.items[0]["hash"] == h:
                return self.items[0], False
            self.seq += 1
            item = {"id": secrets.token_hex(5), "type": kind, "source": source,
                    "ts": time.time(), "hash": h, "size": len(raw), "seq": self.seq}
            if kind == "text":
                item["text"] = data
            else:
                item["png"] = data
                try:
                    item["w"], item["h"] = Image.open(BytesIO(data)).size
                except Exception:
                    item["w"], item["h"] = 0, 0
            self.items.insert(0, item)
            total, keep = 0, []
            for it in self.items:
                total += it["size"]
                if len(keep) < MAX_ITEMS and (total <= MAX_HISTORY_BYTES or len(keep) < 3):
                    keep.append(it)
            self.items = keep
            self._bump()
            return item, True

    def get(self, item_id: str):
        with self.cond:
            for it in self.items:
                if it["id"] == item_id:
                    return it
        return None

    def latest(self, kind: str | None = None):
        with self.cond:
            for it in self.items:
                if kind in (None, it["type"]):
                    return it
        return None

    def remove(self, item_id: str) -> bool:
        with self.cond:
            n = len(self.items)
            self.items = [it for it in self.items if it["id"] != item_id]
            if len(self.items) != n:
                self._bump()
                return True
        return False

    def clear(self) -> None:
        with self.cond:
            self.items = []
            self._bump()

    def set_paused(self, paused: bool) -> None:
        with self.cond:
            self.paused = bool(paused)
            self._bump()

    def wait(self, since: int, timeout: float) -> None:
        with self.cond:
            end = time.time() + timeout
            while self.version <= since:
                rem = end - time.time()
                if rem <= 0:
                    break
                self.cond.wait(rem)

    # ---- per-device delivery: a device (the iPad hub) polls /api/next and gets each new item once ----
    @staticmethod
    def signature(kind: str, data):
        """What a device's clipboard holds, robust to iOS round trips: text is compared after normalising
        line endings and outer whitespace; images by size + a 16x16 grey thumbnail, because iOS may re-encode
        a copied image (different bytes, same picture)."""
        if kind == "text":
            t = data.replace("\r\n", "\n").strip()
            return ("text", hashlib.sha1(t.encode("utf-8")).hexdigest())
        try:
            img = Image.open(BytesIO(data))
            img.load()
            thumb = img.convert("L").resize((16, 16)).tobytes()
            return ("image", img.size, thumb)
        except Exception:
            return ("image", None, hashlib.sha1(data).digest())

    @staticmethod
    def same_content(a, b) -> bool:
        if not a or not b or a[0] != b[0]:
            return False
        if a[0] == "text" or a[1] is None or b[1] is None:
            return a == b
        if a[1] != b[1]:
            return False
        return sum(abs(x - y) for x, y in zip(a[2], b[2])) / len(a[2]) < 4.0

    def next_for(self, client: str, timeout: float):
        """Hand `client` the newest item it has not seen yet, once; None after `timeout` s without one.
        Skips the client's own pushes and content it already holds. First contact starts from now
        (history is never replayed)."""
        with self.cond:
            st = self.clients.setdefault(client, {"cursor": None, "clip": None, "seen": 0.0})
            if st["cursor"] is None:
                st["cursor"] = self.items[0]["seq"] if self.items else 0
            end = time.time() + timeout
            while True:
                st["seen"] = time.time()
                fresh = [it for it in self.items if it["seq"] > st["cursor"]]
                if fresh:
                    newest = fresh[0]                 # items are newest-first
                    st["cursor"] = newest["seq"]      # older unseen items are skipped: a clipboard holds one thing
                    if newest["source"] != client:
                        sig = self.signature(newest["type"], newest["text"] if newest["type"] == "text" else newest["png"])
                        if not self.same_content(sig, st["clip"]):
                            st["clip"] = sig          # what the device's clipboard holds now
                            return newest
                    continue
                rem = end - time.time()
                if rem <= 0:
                    return None
                self.cond.wait(rem)

    def client_echo(self, client: str, kind: str, data) -> bool:
        """True when a device that polls /api/next sends what it already holds: the item we just delivered
        coming back, or the same clipboard uploaded again. Unknown devices are never deduplicated here."""
        with self.cond:
            st = self.clients.get(client)
        if not st:
            return False
        sig = self.signature(kind, data)
        with self.cond:
            if self.same_content(sig, st["clip"]):
                return True
            st["clip"] = sig
            return False

    @staticmethod
    def public(it: dict) -> dict:
        d = {k: it[k] for k in ("id", "type", "source", "ts", "size")}
        if it["type"] == "text":
            d["text"] = it["text"]
        else:
            d["w"], d["h"] = it["w"], it["h"]
            d["url"] = f"/api/item/{it['id']}.png"
        return d

    def snapshot(self) -> list[dict]:
        with self.cond:
            return [self.public(it) for it in self.items]


# --------------------------------------------------------------------------- app

class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.token: str = cfg["token"]
        self.port: int = int(cfg["port"])
        self.clip = WinClipboard()
        self.store = Store()
        self.server: ThreadingHTTPServer | None = None
        self._net: dict = {}
        self._net_ts = 0.0
        self._net_lock = threading.Lock()
        self._stop = threading.Event()

    # ---- urls / network -------------------------------------------------
    def local_url(self) -> str:
        return f"http://localhost:{self.port}/"

    def _probe_net(self) -> dict:
        info: dict = {"lan_ip": None, "all_ips": [], "tailscale_ip": None,
                      "tailscale_dns": None, "tailscale_https": None, "tailscale_online": False}
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            info["lan_ip"] = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        try:
            ips = sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})
            info["all_ips"] = [ip for ip in ips if not ip.startswith(("127.", "169.254."))]
        except Exception:
            pass
        try:
            kw = dict(capture_output=True, text=True, timeout=6, creationflags=CREATE_NO_WINDOW)
            out = subprocess.run(["tailscale", "ip", "-4"], **kw).stdout.strip().splitlines()
            if out:
                info["tailscale_ip"] = out[0].strip()
            st = json.loads(subprocess.run(["tailscale", "status", "--json"], **kw).stdout or "{}")
            self_ = st.get("Self") or {}
            dns = (self_.get("DNSName") or "").rstrip(".")
            info["tailscale_dns"] = dns or None
            info["tailscale_online"] = st.get("BackendState") == "Running"
            # is `tailscale serve` proxying to us? then an https URL with a real certificate exists
            sv = subprocess.run(["tailscale", "serve", "status", "--json"], **kw).stdout
            if sv.strip():
                web = (json.loads(sv) or {}).get("Web") or {}
                for hostport, conf in web.items():
                    for _path, h in (conf.get("Handlers") or {}).items():
                        proxy = h.get("Proxy") or ""
                        if proxy.rstrip("/").endswith(f":{self.port}"):
                            host = hostport[:-4] if hostport.endswith(":443") else hostport
                            info["tailscale_https"] = f"https://{host}/"
        except Exception as e:
            LOG.debug("tailscale probe: %s", e)
        return info

    def net(self) -> dict:
        with self._net_lock:
            if time.time() - self._net_ts > 60:
                self._net = self._probe_net()
                self._net_ts = time.time()
            return dict(self._net)

    def urls(self) -> list[dict]:
        n = self.net()
        t = f"?t={self.token}"
        out = []
        if n.get("tailscale_https"):
            out.append({"label": "Tailscale HTTPS (best: one-tap image copy on iPhone)",
                        "url": n["tailscale_https"] + t, "kind": "tailscale"})
        if n.get("tailscale_ip"):
            out.append({"label": "Tailscale (works from anywhere while the phone's Tailscale is on)",
                        "url": f"http://{n['tailscale_ip']}:{self.port}/{t}", "kind": "tailscale"})
        if n.get("tailscale_dns"):
            out.append({"label": "Tailscale by name",
                        "url": f"http://{n['tailscale_dns']}:{self.port}/{t}", "kind": "tailscale"})
        if n.get("lan_ip"):
            out.append({"label": "Local network (phone must be able to reach the PC's wired address)",
                        "url": f"http://{n['lan_ip']}:{self.port}/{t}", "kind": "lan"})
        for ip in n.get("all_ips", []):
            if ip not in (n.get("lan_ip"), n.get("tailscale_ip")):
                out.append({"label": f"Other adapter ({ip})", "url": f"http://{ip}:{self.port}/{t}", "kind": "other"})
        return out

    def phone_url(self) -> str:
        u = self.urls()
        return u[0]["url"] if u else self.local_url() + f"?t={self.token}"

    def info(self, local: bool) -> dict:
        n = self.net()
        d = {"name": socket.gethostname(), "port": self.port, "paused": self.store.paused,
             "version": self.store.version, "urls": self.urls(),
             "tailscale_online": n.get("tailscale_online"), "tailscale_dns": n.get("tailscale_dns"),
             "tailscale_https": n.get("tailscale_https")}
        if local:
            d["token"] = self.token
        return d

    # ---- clipboard operations -------------------------------------------
    def push(self, kind: str, data, source: str) -> tuple[dict, bool]:
        copied = True
        try:
            if kind == "text":
                self.clip.set_text(data)
            else:
                self.clip.set_image(data)
        except Exception as e:
            copied = False
            LOG.warning("could not set PC clipboard: %s", e)
        item, new = self.store.add(kind, data, source)
        if new:
            LOG.info("%s -> PC: %s (%d bytes)%s", source, kind, item["size"], "" if copied else " [clipboard busy]")
        return item, copied

    def copy_item(self, item: dict) -> None:
        if item["type"] == "text":
            self.clip.set_text(item["text"])
        else:
            self.clip.set_image(item["png"])

    # ---- threads ----------------------------------------------------------
    def start_server(self) -> None:
        Handler.app = self

        class Server(ThreadingHTTPServer):
            allow_reuse_address = False  # a second instance must fail loudly, not share the port
            daemon_threads = True

        self.server = Server(("0.0.0.0", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, name="http", daemon=True).start()
        LOG.info("listening on 0.0.0.0:%d", self.port)

    def start_watcher(self) -> None:
        threading.Thread(target=self._watch, name="clipwatch", daemon=True).start()

    def _watch(self) -> None:
        last = self.clip.seq()
        while not self._stop.is_set():
            time.sleep(0.35)
            try:
                with self.clip.lock:
                    s = self.clip.seq()
                    if s == last:
                        continue
                    last = s
                    if s == self.clip.ignore_seq or self.store.paused:
                        continue
                    time.sleep(0.15)  # let the source app finish rendering all formats
                    res = self.clip.read()
                if not res:
                    continue
                kind, data = res
                item, new = self.store.add(kind, data, "pc")
                if new:
                    LOG.info("PC clipboard: %s (%d bytes)", kind, item["size"])
            except Exception as e:
                LOG.warning("watcher: %s", e)

    def shutdown(self) -> None:
        self._stop.set()
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "ClipBridge/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet access log
        LOG.debug("%s " + fmt, self.address_string(), *args)

    # ---- plumbing ---------------------------------------------------------
    def _send(self, code: int, body=b"", ctype="application/octet-stream", extra: dict | None = None) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _parse(self):
        u = urlparse(self.path)
        return u.path, parse_qs(u.query)

    def _is_local(self) -> bool:
        # direct loopback connection from this PC. Requests proxied by `tailscale serve`
        # also arrive from 127.0.0.1 but carry X-Forwarded-For, so they are NOT local.
        return self.client_address[0] in ("127.0.0.1", "::1") and "X-Forwarded-For" not in self.headers

    def _authed(self, qs) -> bool:
        tok = self.headers.get("X-Token") or (qs.get("t") or [None])[0]
        if not tok:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                tok = auth[7:].strip()
        if tok and hmac.compare_digest(tok, self.app.token):
            return True
        return self._is_local()

    def _body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = bytearray()
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b";")[0] or b"0", 16)
                if size == 0:
                    while self.rfile.readline().strip():
                        pass
                    break
                out += self.rfile.read(size)
                self.rfile.readline()
                if len(out) > MAX_BODY:
                    raise ValueError("body too large")
            return bytes(out)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def _item_from_path(self, prefix: str):
        rest = self.path.split("?", 1)[0][len(prefix):]
        for ext in (".png", ".txt", ".jpg"):
            if rest.endswith(ext):
                rest = rest[: -len(ext)]
        return self.app.store.get(rest)

    def _send_item(self, it: dict, download: bool) -> None:
        if it["type"] == "text":
            extra = {"Content-Disposition": f'attachment; filename="clip-{it["id"]}.txt"'} if download else None
            self._send(200, it["text"], "text/plain; charset=utf-8", extra)
        else:
            disp = "attachment" if download else "inline"
            self._send(200, it["png"], "image/png",
                       {"Content-Disposition": f'{disp}; filename="clip-{it["id"]}.png"'})

    # ---- routes -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path, qs = self._parse()
            if path in ("/", "/index.html"):
                try:
                    with open(INDEX_PATH, "rb") as f:
                        html = f.read()
                except OSError:
                    return self._send(500, "index.html is missing next to clipbridge.py", "text/plain")
                # iOS "Add to Home Screen" reads the manifest named in the HTML and starts the app at its
                # start_url, with storage separate from Safari. Bake the pairing key into that URL so the
                # Home-Screen app is paired from its first launch.
                t = (qs.get("t") or [""])[0]
                if t and hmac.compare_digest(t, self.app.token):
                    html = html.replace(b'href="/manifest.webmanifest"',
                                        b'href="/manifest.webmanifest?t=' + t.encode() + b'"', 1)
                return self._send(200, html, "text/html; charset=utf-8")
            if path in ("/favicon.ico", "/icon-192.png", "/apple-touch-icon.png"):
                return self._send(200, icon_png(192), "image/png", {"Cache-Control": "public, max-age=86400"})
            if path == "/icon-512.png":
                return self._send(200, icon_png(512), "image/png", {"Cache-Control": "public, max-age=86400"})
            if path == "/manifest.webmanifest":
                t = (qs.get("t") or [""])[0]
                start = "/" + (f"?t={t}" if t else "")
                man = {"name": APP_NAME, "short_name": APP_NAME, "display": "standalone", "start_url": start,
                       "background_color": "#0b1220", "theme_color": "#2563eb",
                       "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
                return self._send(200, json.dumps(man), "application/manifest+json")

            if not self._authed(qs):
                return self._json({"error": "unauthorized - open ClipBridge with the link/QR shown on the PC"}, 401)

            if path == "/api/info":
                return self._json(self.app.info(self._is_local()))
            if path in MAC_FILES:
                fname, ctype = MAC_FILES[path]
                try:
                    with open(os.path.join(HERE, fname), "rb") as f:
                        data = f.read().replace(b"\r\n", b"\n")  # scripts must reach the Mac with LF endings
                except OSError:
                    return self._json({"error": f"{fname} is missing next to clipbridge.py"}, 404)
                return self._send(200, data, ctype, {"Content-Disposition": f'inline; filename="{fname}"'})
            if path == "/api/state":
                since = int((qs.get("since") or ["-1"])[0])
                if qs.get("wait") and self.app.store.version <= since:
                    self.app.store.wait(since, 25)
                st = self.app.store
                return self._json({"version": st.version, "paused": st.paused, "items": st.snapshot()})
            if path == "/api/next":
                # the iPad hub: block up to `wait` s for an item this device has not seen, return it raw, once
                client = ((qs.get("client") or ["ipad"])[0]).strip()[:32] or "ipad"
                try:
                    wait = min(max(float((qs.get("wait") or ["20"])[0]), 0.0), 25.0)
                except ValueError:
                    wait = 20.0
                it = self.app.store.next_for(client, wait)
                if not it:
                    return self._send(204)
                LOG.info("PC -> %s: %s (%d bytes)", client, it["type"], it["size"])
                extra = {"X-Clip-Id": it["id"], "X-Clip-Type": it["type"], "X-Clip-Source": it["source"]}
                if it["type"] == "text":
                    return self._send(200, it["text"], "text/plain; charset=utf-8", extra)
                extra["Content-Disposition"] = f'inline; filename="clip-{it["id"]}.png"'
                return self._send(200, it["png"], "image/png", extra)
            if path.rstrip("/") == "/api/push":
                return self._json({"error": "ClipBridge: this URL needs Method POST (in Shortcuts: Get Contents of URL > Method > POST, Request Body > File)"}, 405)
            if path == "/api/latest":
                kind = (qs.get("type") or [None])[0]
                it = self.app.store.latest(kind if kind in ("text", "image") else None)
                if not it:
                    return self._send(204)
                return self._send_item(it, bool(qs.get("dl")))
            if path.startswith("/api/item/"):
                it = self._item_from_path("/api/item/")
                if not it:
                    return self._json({"error": "not found"}, 404)
                return self._send_item(it, bool(qs.get("dl")))
            if path == "/qr.png":
                target = (qs.get("u") or [""])[0]
                if not target.startswith(("http://", "https://")):
                    return self._json({"error": "bad url"}, 400)
                png = qr_png(target)
                if png is None:
                    return self._json({"error": "pip install qrcode"}, 501)
                return self._send(200, png, "image/png")
            return self._json({"error": "not found"}, 404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            LOG.exception("GET %s failed", self.path)
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            path, qs = self._parse()
            if not self._authed(qs):
                self._body()
                return self._json({"error": "unauthorized"}, 401)
            if len(path) > 1:
                path = path.rstrip("/")
            # "/" too: a Shortcut pointed at the plain pairing link should still just work
            if path in ("/", "/api/push"):
                body = self._body()
                source = (qs.get("source") or ["phone"])[0]
                source = source if source in ("phone", "pc", "shortcut", "mac", "ipad") else "phone"
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype == "multipart/form-data":
                    # iOS Shortcut "Request Body: Form" with an image field and a text field:
                    # use the first non-empty image, otherwise the text
                    parts = split_multipart(body, self.headers.get("Content-Type") or "")
                    img_parts = [d for _n, ct, d in parts if d and (ct.startswith("image/") or sniff_image(d))]
                    txt_parts = [d for _n, ct, d in parts if d.strip() and not (ct.startswith("image/") or sniff_image(d))]
                    if img_parts:
                        body, ctype = img_parts[0], "image/*"
                    elif txt_parts:
                        body, ctype = txt_parts[0], "text/plain"
                    else:
                        body = b""
                if not body:
                    return self._json({"error": "empty body"}, 400)
                if not (ctype.startswith("image/") or sniff_image(body)):
                    # rich text / web page copied on the phone: reduce to plain text (or the inline image)
                    head = body[:300].lstrip().lower()
                    if head.startswith(b"{\\rtf"):
                        body, ctype = rtf_to_text(body.decode("latin-1")).encode("utf-8"), "text/plain"
                    elif looks_like_html(body[:400].decode("utf-8", errors="replace")):
                        kind, val = html_extract(body.decode("utf-8", errors="replace"))
                        if kind == "image" and sniff_image(val):
                            body, ctype = val, "image/*"
                        else:
                            body, ctype = val.encode("utf-8"), "text/plain"
                if ctype.startswith("image/") or sniff_image(body):
                    try:
                        img = Image.open(BytesIO(body))
                        img.load()
                    except Exception:
                        return self._json({"error": "unsupported image format"}, 415)
                    png = body if body[:8] == b"\x89PNG\r\n\x1a\n" else to_png(img)
                    if self.app.store.client_echo(source, "image", png):
                        return self._json({"ok": True, "duplicate": True, "copied": False})
                    item, copied = self.app.push("image", png, source)
                else:
                    text = body.decode("utf-8", errors="replace")
                    if ctype == "application/x-www-form-urlencoded" and text.startswith("text="):
                        text = (parse_qs(text).get("text") or [""])[0]
                    text = text.replace("\r\n", "\n")
                    if not text.strip():
                        return self._json({"error": "empty text"}, 400)
                    text = text[:MAX_TEXT]
                    if self.app.store.client_echo(source, "text", text):
                        return self._json({"ok": True, "duplicate": True, "copied": False})
                    item, copied = self.app.push("text", text, source)
                return self._json({"ok": True, "copied": copied, "item": Store.public(item)})
            if path.startswith("/api/copy/"):
                it = self._item_from_path("/api/copy/")
                if not it:
                    return self._json({"error": "not found"}, 404)
                try:
                    self.app.copy_item(it)
                except Exception as e:
                    return self._json({"error": f"clipboard busy: {e}"}, 503)
                return self._json({"ok": True})
            if path.startswith("/api/delete/"):
                it = self._item_from_path("/api/delete/")
                return self._json({"ok": bool(it and self.app.store.remove(it["id"]))})
            if path == "/api/clear":
                self.app.store.clear()
                return self._json({"ok": True})
            if path == "/api/pause":
                body = self._body()
                try:
                    paused = bool(json.loads(body or b"{}").get("paused", not self.app.store.paused))
                except Exception:
                    paused = not self.app.store.paused
                self.app.store.set_paused(paused)
                LOG.info("PC clipboard sharing %s", "paused" if paused else "resumed")
                return self._json({"ok": True, "paused": paused})
            return self._json({"error": "not found"}, 404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            LOG.exception("POST %s failed", self.path)
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_DELETE(self):
        path, qs = self._parse()
        if not self._authed(qs):
            return self._json({"error": "unauthorized"}, 401)
        if path.startswith("/api/item/"):
            it = self._item_from_path("/api/item/")
            return self._json({"ok": bool(it and self.app.store.remove(it["id"]))})
        return self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- tray / main

def run_tray(app: App) -> bool:
    try:
        import pystray  # type: ignore
        from pystray import MenuItem as MI
    except ImportError:
        return False

    def open_page(icon, item):
        webbrowser.open(app.local_url())

    def copy_link(icon, item):
        try:
            app.clip.set_text(app.phone_url())
            icon.notify("Phone link copied to the clipboard", APP_NAME)
        except Exception as e:
            LOG.warning("copy link: %s", e)

    def toggle_pause(icon, item):
        app.store.set_paused(not app.store.paused)

    def quit_(icon, item):
        icon.stop()
        app.shutdown()

    menu = pystray.Menu(
        MI("Open ClipBridge", open_page, default=True),
        MI("Copy phone link", copy_link),
        MI("Pause sharing the PC clipboard", toggle_pause, checked=lambda it: app.store.paused),
        pystray.Menu.SEPARATOR,
        MI("Quit", quit_),
    )
    icon = pystray.Icon(APP_NAME, make_icon(64), f"{APP_NAME}  -  {app.local_url()}", menu)
    icon.run()
    return True


def is_clipbridge_running(port: int) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=2) as r:
            return r.headers.get("Server", "").startswith(APP_NAME)
    except Exception:
        return False


def load_config(args) -> tuple[dict, bool]:
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg: dict = {}
    first_run = not os.path.exists(CONFIG_PATH)
    if not first_run:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    changed = False
    if args.regen_token or not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        changed = True
    if args.port:
        if cfg.get("port") != args.port:
            changed = True
        cfg["port"] = args.port
    if "port" not in cfg:
        cfg["port"] = DEFAULT_PORT
        changed = True
    if changed:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    return cfg, first_run


def setup_logging(console: bool) -> None:
    LOG.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    LOG.addHandler(fh)
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        LOG.addHandler(sh)


def print_banner(app: App, ascii_qr: bool) -> None:
    if sys.stdout is None:
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(f"\n{APP_NAME} is running.  PC page: {app.local_url()}\n")
    print("Open ONE of these on your iPhone (or scan the QR code on the PC page):")
    urls = app.urls()
    for u in urls:
        print(f"  - {u['label']}\n      {u['url']}")
    if ascii_qr and urls:
        try:
            import qrcode  # type: ignore
            q = qrcode.QRCode(border=1)
            q.add_data(urls[0]["url"])
            q.make(fit=True)
            print()
            q.print_ascii(invert=True)
        except Exception:
            pass
    print("\nPress Ctrl+C to stop.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Clipboard bridge between this PC and your phone")
    ap.add_argument("--port", type=int, help=f"port to listen on (default {DEFAULT_PORT})")
    ap.add_argument("--console", action="store_true", help="no tray icon; log to the console and print a QR code")
    ap.add_argument("--no-tray", action="store_true", help="no tray icon (quiet)")
    ap.add_argument("--regen-token", action="store_true", help="generate a new pairing token")
    ap.add_argument("--open", action="store_true", help="open the PC page in the browser")
    args = ap.parse_args()

    cfg, first_run = load_config(args)
    setup_logging(console=args.console or args.no_tray or sys.stdout is not None)
    app = App(cfg)
    try:
        app.start_server()
    except OSError as e:
        LOG.error("cannot listen on port %d: %s", app.port, e)
        if getattr(e, "winerror", None) == 10048:
            if is_clipbridge_running(app.port):
                LOG.error("ClipBridge is already running - opening its page")
                webbrowser.open(app.local_url())
            else:
                LOG.error("another program uses port %d; start with  --port <free port>", app.port)
        return 1
    app.start_watcher()
    threading.Thread(target=app.net, daemon=True).start()  # warm the network probe

    if sys.stdout is not None:
        print_banner(app, ascii_qr=args.console)
    if args.open or first_run:
        webbrowser.open(app.local_url())

    if not (args.console or args.no_tray) and run_tray(app):
        return 0
    try:
        while not app._stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
