# ClipBridge

Copy on your Windows PC, paste on your iPhone. Paste on your iPhone, it lands in the PC clipboard.
Text **and images**, no cloud account, nothing installed on the phone: the PC runs a tiny web server
and the phone uses a web page (or an iOS Shortcut).

```
 Windows PC  ──clipbridge.py──►  http://<pc-address>:48765/?t=<key>  ◄──  iPhone (Safari / Home-Screen app / Shortcut)
 Windows PC  ──clipbridge.py──►  Mac (mac_relay.py, optional)  ──Universal Clipboard──►  iPhone paste menu, no tap
 Windows PC  ◄─clipbridge.py─►  iPad (looping Shortcut, optional)  ◄─Universal Clipboard─►  iPhone, both ways, no tap
```

## 1. Start it on the PC

Double-click **`ClipBridge.bat`** (tray icon, no window) or **`ClipBridge (console).bat`** (shows a log and a QR code).

Requirements: Python 3.10+ with `Pillow`, `pywin32`, `qrcode`, `pystray` (`pip install -r requirements.txt`).
On this PC everything is already installed.

Right-click the tray icon for *Open ClipBridge*, *Copy phone link*, *Pause sharing the PC clipboard*, *Quit*.
To start it automatically at login run `install_autostart.bat` once (`uninstall_autostart.bat` undoes it).

## 2. Pair the iPhone (once)

Open <http://localhost:48765/> on the PC. The **Pair your iPhone** card shows QR codes. Scan one with the
iPhone camera:

| Link | When it works |
|---|---|
| **Tailscale** (`http://100.x.x.x:48765/?t=…`) | Always, as long as the Tailscale app is on in the phone. Works on MIT Wi-Fi, at home, on cellular. This is the reliable one: the PC is on wired MIT with a private 10.93.x address and MIT may not route Wi-Fi ↔ wired traffic. |
| **Local network** (`http://10.93.x.x:48765/?t=…`) | Only if the phone can reach the PC's wired address (same building network / no client isolation). Try it; if the page does not load, use Tailscale. |

The link contains the pairing key (`t=…`). Keep it private; anyone with the link can read the shared clipboard.
Then in Safari: Share → **Add to Home Screen** to get an app icon that opens straight into ClipBridge.

**Firewall:** Windows may ask "Allow Python to communicate on networks" the first time: tick both boxes and allow.
If the phone still cannot connect, run `allow_firewall.bat` once (adds an inbound rule for TCP 48765).

## 3. Use it

**Phone → PC:** on the phone page tap the box, long-press → *Paste* (text, or a copied image / screenshot),
then **Send to PC**. It is now in the PC clipboard; just Ctrl+V. *Photo…* picks an image from Photos and sends it.
Pasting in the editor puts the text in the box first, so you can edit before sending.

**PC → phone:** whatever you copy on the PC (Ctrl+C, Win+Shift+S screenshots, copied image files) appears at the
top of the phone page within a second.
Text: tap **Copy**. Image: tap **Open to copy**, then long-press the image → *Copy* (or *Save to Photos*).
With the HTTPS address (next section) image copy is one tap and *Share* works too.

The list keeps the last 60 items (both directions). *→ PC clipboard* puts an older item back into the PC
clipboard; *Delete* removes an item from the list on all devices. **Pause** stops the PC from sharing new copies
(handy before copying a password).

### Optional: one-tap image copy & share (HTTPS through Tailscale)

Safari only exposes the clipboard-write and share APIs on HTTPS pages. Tailscale can give the PC a real
certificate for free. Run once on the PC (PowerShell):

```bash
tailscale serve --bg --https=443 http://127.0.0.1:48765
```

Then open `https://<pc-name>.<tailnet>.ts.net/?t=<key>` on the phone (the PC pair card shows the exact link,
labelled *Tailscale HTTPS*). If the command says HTTPS certificates are not enabled, turn on
*MagicDNS* and *HTTPS Certificates* in the Tailscale admin console (DNS page) and rerun it.
`tailscale serve reset` removes it.

## 4. iOS Shortcuts (optional, zero taps)

The server has two raw endpoints that iOS Shortcuts can call directly. Copy your phone link from the PC pair
card; it looks like `http://100.81.162.90:48765/?t=KEY`.

**"Send to PC"** (Shortcuts app → + → add actions):
1. **Get Clipboard**
2. **Get Images from Input** — input *Clipboard*
3. **Get Contents of URL** — URL `http://100.81.162.90:48765/api/push?t=KEY` · tap **Show More** · Method **POST** ·
   Request Body **Form** with two fields: **File** `image` = *Images*, **Text** `text` = *Clipboard*

ClipBridge uses the picture when there is one and the text otherwise. Rich text (RTF) and copied web pages
become plain text; iPhone HEIC photos are converted (needs `pillow-heif`).
To stop the permission prompts: Settings → Apps → Shortcuts → **Paste from Other Apps → Allow**, and answer
**Always Allow** when the Shortcut first asks to connect to the PC.

**"Get from PC"**:
1. **Get Contents of URL** — URL `http://100.81.162.90:48765/api/latest?t=KEY` (Method GET)
2. **Copy to Clipboard** — *Contents of URL*
3. **Show Notification** "Copied from PC"

Works for text and images alike (`/api/latest?type=text` or `?type=image` to pick one kind).

## 5. Mac relay (optional): copy on the PC, paste on the iPhone with no tap at all

If the MacBook is usually next to you, a small agent on the Mac closes the last gap. `mac_relay.py` mirrors
every new PC copy into the Mac clipboard; Apple's **Universal Clipboard** then advertises it to the iPhone
(and iPad) nearby, so the PC copy is simply there in the iPhone's paste menu, in any app, with no page to
open and no Back Tap. Copies made on the Mac land in the PC clipboard too (they show up with a 🍎 Mac badge).
iPhone → PC still needs a gesture (section 4): iOS lets nothing but Apple read its clipboard in the background.

```
 Windows PC ──ClipBridge──► Mac clipboard (mac_relay.py) ──Universal Clipboard──► iPhone paste menu
```

### Install: three commands in Terminal on the Mac

Replace `KEY` with the pairing key from the PC pair card (the `t=…` part of the Tailscale link,
`http://100.81.162.90:48765/?t=KEY`). The Mac's Tailscale app must be connected. Once `tailscale serve` is set
up (section 3) the HTTPS address `https://chaoyi.taile4b7ba.ts.net` works in place of `http://100.81.162.90:48765`.

1. Fetch the relay and its installer from the PC (the key is required, the files are not public):

   ```bash
   mkdir -p ~/ClipBridge && cd ~/ClipBridge && curl -fsS -H "X-Token: KEY" -o mac_relay.py http://100.81.162.90:48765/mac_relay.py -o install_mac_relay.sh http://100.81.162.90:48765/install_mac_relay.sh
   ```

2. Install it as a login agent. The installer picks a `python3`, checks the connection to the PC and the
   pasteboard, writes `~/Library/LaunchAgents/com.clipbridge.macrelay.plist` and loads it with `launchctl`:

   ```bash
   bash install_mac_relay.sh --url http://100.81.162.90:48765 --token KEY
   ```

   (`bash install_mac_relay.sh --url "http://100.81.162.90:48765/?t=KEY"`, the pairing link itself, works too.)

3. To stop and remove it later:

   ```bash
   bash ~/ClipBridge/install_mac_relay.sh --uninstall
   ```

   `launchctl bootout gui/$(id -u)/com.clipbridge.macrelay` stops it as well; `--status` shows whether it runs.

Test: copy a sentence on the PC, then long-press in any text field on the iPhone → *Paste*. The relay logs one
line per transfer to `~/Library/Logs/clipbridge-relay.log` (`tail -f` it while testing). It starts at login,
is restarted by launchd if it ever dies, and keeps retrying while the PC or Tailscale is unreachable.

Options (append to the install command, or run `python3 mac_relay.py --help`):

| Option | Effect |
|---|---|
| `--no-push` | one-directional: PC → Mac only, nothing copied on the Mac goes to the PC |
| `--pull-remote` | experimental: also forward iPhone/iPad copies that Universal Clipboard advertises to the Mac on to the PC. iPhone → PC becomes automatic too, but every iPhone copy then triggers a "Pasting from iPhone" transfer on the Mac |
| `--python /path/to/python3` | use a specific interpreter |

To run it by hand instead of as an agent: `python3 mac_relay.py --url http://100.81.162.90:48765 --token KEY -v`
(Ctrl+C stops it; `--check` only tests the connection and the pasteboard and exits).

### Requirements on the Mac

* **Python 3.** The one that comes with Apple's Command Line Tools (`xcode-select --install`) is ideal:
  it includes PyObjC, so the relay talks to `NSPasteboard` directly and polls every 0.5 s. With any other
  `python3` (Homebrew, python.org) the relay falls back to `osascript` (JavaScript for Automation) for the
  same pasteboard calls, which is slower (polls once a second, ~0.1 s per call). No pip packages are needed;
  `python3 -m pip install --user pyobjc-framework-Cocoa` gives any interpreter the fast path.
* **Universal Clipboard working between the Mac and the iPhone**: same Apple ID on both, Bluetooth and Wi-Fi
  on on both, Handoff enabled (Mac: System Settings → General → AirDrop & Handoff → *Allow Handoff between this
  Mac and your iCloud devices*; iPhone: Settings → General → AirPlay & Handoff → *Handoff*). Quick check without
  ClipBridge: copy on the Mac, paste on the iPhone.
* The Mac and the PC on the same tailnet (or the PC's address reachable some other way).

### What to expect, and limitations

* **The Mac must be awake and nearby.** Universal Clipboard works over Bluetooth LE + peer-to-peer Wi-Fi within
  roughly 10 m; a sleeping Mac (lid closed, no display) relays nothing. When the Mac wakes up, the newest PC
  copy is mirrored right away, older ones are not replayed.
* **Paste within about two minutes.** Universal Clipboard offers a copy to the iPhone only for a short while
  after it was made. Later, use the ClipBridge page or the *Get from PC* Shortcut as before; the item is still
  in the history.
* **Images** are written to the Mac clipboard as PNG + TIFF, so every Mac app and Universal Clipboard accept
  them. On the iPhone a big image is transferred when you paste it (a progress bar appears). ClipBridge's upload
  limit (60 MB) applies to Mac → PC as well.
* **Everything you copy on the PC now also lands in the Mac clipboard** and in the iPhone's paste menu. Use
  **Pause** on the PC (tray menu / Pause button) before copying a password, and remember that clipboard
  managers on the Mac keep a history too.
* **No ping-pong.** The relay remembers what it wrote in each direction (pasteboard change count + content hash)
  and ignores it when it comes back, and the server tags Mac items with `source=mac` so the relay never mirrors
  its own pushes. If the same text is copied on both machines, nothing happens twice.
* **The pairing key is stored on the Mac** in the agent plist (mode 600, readable only by your account) and in
  `~/ClipBridge/` if you keep the downloaded files there. `python clipbridge.py --regen-token` on the PC
  invalidates it; rerun the installer with the new key afterwards.
* **Built and tested on the PC side only** (server endpoints, source tagging and the relay logic with a fake
  pasteboard). The macOS pasteboard code is written defensively but had not been run on a Mac when this was
  written; if the installer or the relay fails, run `python3 mac_relay.py --url … --token … --check` and
  look at the message.

## 6. iPad as the hub (optional, no Mac needed): both directions with no gesture

Universal Clipboard works between an iPad and an iPhone just as it does from a Mac. So an iPad that keeps a
small Shortcut running can do the Mac relay's job, and more:

* **PC → iPhone:** the iPad fetches each new PC copy and puts it in its clipboard; Universal Clipboard offers
  it in the iPhone's paste menu. No tap on the iPhone.
* **iPhone → PC:** whatever you copy on the iPhone is advertised to the iPad; the Shortcut reads it and sends it
  to the PC clipboard. No tap either. (These items show a 📲 iPad badge, since the iPad delivered them.)

```
 Windows PC ◄──ClipBridge (Tailscale)──► iPad: "PC Hub" Shortcut, on screen ◄──Universal Clipboard──► iPhone
```

The catch: iPadOS lets a Shortcut use the clipboard only while it runs in the foreground. The iPad becomes a
small dedicated hub: plugged in, unlocked, with the Shortcut running on screen.

### Build the "PC Hub" Shortcut on the iPad (once)

Use the Tailscale link from the PC pair card; `KEY` is its `t=…` part. Tailscale must be on in the iPad.
Shortcuts app → **+** → add these actions in order (*If* and *Repeat* insert their own *Otherwise* /
*End If* / *End Repeat*):

1. **Repeat** `1000` times
2. **Get Contents of URL** — `http://100.81.162.90:48765/api/next?client=ipad&wait=8&t=KEY` (Method **GET**)
3. **If** *Contents of URL* **has any value**
4. **Copy to Clipboard** — *Contents of URL*. Tap the arrow on the action: **Local Only off** (the default),
   *Expire At* empty. Local Only on would keep it from the iPhone.
5. **Otherwise**
6. **Get Clipboard**
7. **If** *Clipboard* **has any value**
8. **Get Contents of URL** — `http://100.81.162.90:48765/api/push?source=ipad&t=KEY` · Method **POST** ·
   Request Body **File** → *Clipboard*
9. **End If** (inner) · **End If** (outer) · **End Repeat**
10. Optional, last action after *End Repeat*: **Run Shortcut** → *PC Hub*, so it starts over by itself.

Name it **PC Hub**. `client=ipad` in step 2 and `source=ipad` in step 8 must match; that is how the PC knows
what the iPad already holds.

### Run it

1. iPad: Settings → Display & Brightness → **Auto-Lock → Never** (at least while the hub runs), and plug it in.
2. Open the Shortcuts app and tap **PC Hub**. Leave it on screen. Split View next to another app works; locking
   the iPad or switching away for long stops it.
3. The first run asks *Allow "PC Hub" to connect to 100.81.162.90?* and *…to paste from …?*: choose
   **Always Allow** for both, otherwise every round asks again.
4. Test: copy a sentence on the PC, then long-press a text field on the iPhone → **Paste**. Copy something on
   the iPhone: it is in the PC clipboard within about 10 s.

To stop: tap the stop button on the running Shortcut, or lock the iPad.

### How it behaves

* Each round waits up to 8 s for a new PC item (`/api/next`). If one comes, the iPad copies it; otherwise it
  sends its current clipboard to the PC. So PC → iPhone takes about a second, iPhone → PC up to ~10 s.
* **No ping-pong.** The server remembers per device what it delivered and what the device last sent. An item
  that comes back from the iPad unchanged (or re-encoded by iOS: images are compared by size and a small
  thumbnail, not by bytes) is ignored, and the iPad never gets its own pushes back. The same clipboard sent
  every round is ignored too, so the PC clipboard is not overwritten while nothing changes.
* `/api/next` hands out only the newest unseen item, once. When the hub starts it begins from *now*; it never
  replays the history.
* `Repeat 1000` with 8 s rounds lasts about 2 hours when idle; step 10 restarts it.

### Limitations

* **Foreground only.** The Shortcut must stay running on screen on an unlocked iPad. This is an iPadOS rule
  (no background clipboard access), not a ClipBridge one.
* **Universal Clipboard rules apply:** iPad and iPhone on the same Apple ID, Bluetooth and Wi-Fi on, Handoff on
  (Settings → General → AirPlay & Handoff), within ~10 m. A copy is offered for about two minutes after it was
  made; later, use the ClipBridge page or the *Get from PC* Shortcut (the item is still in the history).
* **iPhone → PC needs the iPad to read the clipboard,** which triggers a short "pasting from iPhone" transfer
  each time you copy on the iPhone. Copies made on the iPad itself go to the PC too.
* **Images in the iPad clipboard are uploaded again every round** (the server drops them as duplicates). That is
  a few MB every 8 s while a big image sits in the clipboard: fine on Wi-Fi, noticeable on cellular data.
* **Pause** on the PC stops sharing PC copies with the iPad as well. Anything you copy on the PC reaches every
  device in reach of the iPad, so pause before copying a password.
* Built and tested on the PC side only (the endpoint, echo suppression and image round trips with re-encoded
  images); the Shortcut itself has not been run on the iPad yet.

## Why it is not as seamless as iPhone ↔ Mac (and what gets closest)

Apple's Universal Clipboard works because iOS lets *only Apple* read and write the clipboard in the background.
Third-party apps and web pages cannot: an app can only touch the clipboard while it is in the foreground and,
since iOS 16, writing from a web page needs a tap. So no Windows ↔ iPhone tool can make "copy on the PC, paste
in any iPhone app" fully automatic; this includes every clipboard-sync app on the App Store.

What gets closest, in order of smoothness:

1. **Back Tap / Action button + the two Shortcuts above.** Copy on the PC, double-tap the back of the phone,
   paste in any app. Reverse: triple-tap sends the phone clipboard to the PC. One gesture, system-wide,
   text and images, no page to open.
2. **Share sheet → "Send to PC".** From any app, Share → the shortcut. Good for photos and screenshots.
3. **The web page / Home-Screen app.** Needed only for the history, or when you want to see what is on the PC.
4. **Mac as a relay (if the MacBook is usually next to you).** `mac_relay.py` (section 5) mirrors the PC
   clipboard into the Mac clipboard and Universal Clipboard hands it to the iPhone: PC → iPhone becomes fully
   automatic, no gesture at all, whenever the Mac is awake and nearby. iPhone → PC still needs a gesture.
5. **iPad as the hub (section 6).** A looping Shortcut on an unlocked iPad makes *both* directions automatic
   through Universal Clipboard, as long as the Shortcut stays on screen. Good for a desk setup.

## Files

| File | Purpose |
|---|---|
| `clipbridge.py` | the server: clipboard watcher, history, HTTP API, tray icon |
| `index.html` | the web app served to phone and PC |
| `ClipBridge.bat` / `ClipBridge (console).bat` | launchers |
| `allow_firewall.bat` | adds the inbound firewall rule (asks for admin) |
| `install_autostart.bat` / `uninstall_autostart.bat` | start at login |
| `mac_relay.py` | the Mac relay (section 5): PC clipboard ↔ Mac clipboard, so Universal Clipboard reaches the iPhone |
| `install_mac_relay.sh` / `com.clipbridge.macrelay.plist` | its installer (run on the Mac) and the launchd agent template |

Config (pairing key, port) and the log live in `%APPDATA%\ClipBridge\`. `python clipbridge.py --regen-token`
makes a new key (re-pair the phone afterwards); `--port 9000` changes the port.

## HTTP API

All endpoints need the key, as `?t=KEY`, header `X-Token: KEY` or `Authorization: Bearer KEY`
(requests from the PC itself over `localhost` need no key).

| Method | Path | Description |
|---|---|---|
| GET | `/api/state?since=N&wait=1` | history as JSON; with `wait=1` blocks up to 25 s until something changes (long-poll) |
| GET | `/api/next?client=ipad&wait=8` | for the iPad hub: blocks up to `wait` s (max 25) and returns the newest item this `client` has not received, raw (`text/plain` or `image/png`), once; 204 if none. Skips the client's own pushes and what it already holds |
| GET | `/api/latest[?type=text\|image]` | newest item as raw `text/plain` or `image/png` (204 if empty) |
| GET | `/api/item/<id>[.png]` | one item raw; `&dl=1` forces download |
| POST | `/api/push[?source=phone]` | body = text (`text/plain`) or an image (`image/*`); put in PC clipboard + history. `source` = `phone` (default), `shortcut`, `mac` or `ipad` (sets the badge). A device that polls `/api/next` gets `{"duplicate": true}` back when it sends what it already holds |
| POST | `/api/copy/<id>` | put a history item back into the PC clipboard |
| POST | `/api/delete/<id>` · `/api/clear` | remove one / all items |
| POST | `/api/pause` `{"paused": true}` | pause / resume sharing the PC clipboard |
| GET | `/api/info` | PC name, pairing links, Tailscale state |
| GET | `/mac_relay.py` · `/install_mac_relay.sh` · `/com.clipbridge.macrelay.plist` | the Mac relay files, for `curl` from the Mac (key required, served with LF line endings) |

## Troubleshooting

* **Page does not load on the phone** – try the Tailscale link (phone's Tailscale app must be *Connected*).
  On the PC, `http://localhost:48765/` must work; if not, ClipBridge is not running (check the tray / log).
* **Loads over Tailscale but not over MIT Wi-Fi** – expected; MIT's wired and wireless networks are separate.
* **"Not paired"** – open the page with the full link (`?t=…`). A Home-Screen app keeps its own key, so add it
  to the Home Screen from a tab opened with the full link.
* **Images from the phone come out without transparency on the PC** – normal for the Windows bitmap format;
  apps that read the PNG format (browsers, Slack, Paint.NET) keep transparency.
* **Nothing appears after copying on the PC** – is sharing paused (tray menu / Pause button)? Copied *files*
  only sync when they are images.
