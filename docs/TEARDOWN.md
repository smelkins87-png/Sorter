# Magic Sorter v4.5 image: what talks to the outside world

Source: `magic_sorter_v4.5.zip` (2.2 GB) from the vendor's public S3 bucket,
which contains `magic_sorter.img`: a Raspberry Pi OS (Bookworm, 32-bit
userspace, Python 3.11) SD card image. Analysed read-only; nothing was run.

## Software on the image

| Path | What it is |
|---|---|
| `/home/pi/magic-sorter/magicsorter/` | Python sorter app. Readable source; says it's GPL-3.0 ("FabbricaBinaria - Davide Tampellini"). Runs as `magicsorter_py.service`, and listens on `127.0.0.1:2000` (commands) and `:8976` (log websocket). |
| `/home/pi/magic-sorter-touch/` | Touchscreen UI, Electron/jQuery (`magicsorter_touch.service`) |
| `/home/pi/magic-sorter-cp/*.deb` | "Control Panel" UI, Electron/React 5.11.0 (`magicsorter_cp.service`) |
| `/home/pi/.magicsorter/config.json` | Settings, including `endpoint`, `device_id`, `secret` (filled from `/boot/firmware/magicsorter_config.txt` on first boot) |
| `lib/firmware/`, `hex/` | Firmware for the motion controller boards (Arduino/RAMPS `.hex`, ESP32 `.bin`), flashed over USB/serial/CAN |

The Pi only takes the photo, crops it, and uploads it. It has no card
recognition of its own (there's no recognition model, card images or card
database on the image). Motors are driven by the separate controller boards.

## How a card gets identified today

`lib/organizer/camera.py` → `lib/matcher/connector.py`:

1. The camera frame is cropped with the configured offsets and saved to `/tmp/temp.jpeg`.
2. `POST https://api{2,3,4,5}.magic-sorter.com/image/mtg?set_type=..&condition=..&lang=..[&foil=..][&set[]=..]`
   - multipart field `image` = the JPEG
   - header `X-Auth: <device_id>:<secret>`, a fixed `User-Agent`, cookie `XDEBUG_SESSION=PHPSTORM`
3. Response `200 {"result": {title, set, num, price, price_trend, foil, lang, local_title, rarity, ext_id (Scryfall id), ecommerce_id (TCGplayer id), detect_set, confidence, extra: "<JSON string: colors, rarity, type, cmc>"}}`,
   or `404 {"error": ...}` for "Card not found".

The server host is rewritten to a random `api2`–`api5` only **if the endpoint
contains "magic-sorter"**. Any other endpoint is used as is, which is what
makes a drop-in local server possible without patching vendor code.

## Every outbound connection found

| Destination | Who | What is sent | When |
|---|---|---|---|
| `api2`–`api5.magic-sorter.com/image/<game>` | Python | **Every card photo**, device id + secret, scan settings | every card |
| `<endpoint>/sets/<game>` | Python, touch UI, CP | device id + secret | set-based rules, set picker |
| `<endpoint>/popular/<format>` | Python | device id + secret | "popular cards" rules |
| `<endpoint>/mail` | Python | **Full results of each run** (zip of CSVs), to be emailed | end of run, if enabled |
| `<endpoint>/device/config` | Python, touch UI, CP | **Whole device config** (backup) / restore | on config save; restore button |
| `<endpoint>/user/preferences` | Python | price source preference | on config save |
| `o509538.ingest.sentry.io` | Python (`magicsorter.py`) | Warnings/errors **and log lines at INFO level as breadcrumbs**, tagged with device id; `send_default_pii=True` | continuously |
| `www.magic-sorter.com/index.php?option=com_ars...` | touch UI, CP, Python | download id | update checks |
| update download (`link&dlid=...`) | touch UI | — | **downloads and installs code with TLS verification disabled** (`NODE_TLS_REJECT_UNAUTHORIZED=0`) |
| update download | CP | — | installs the `.deb` with `sudo dpkg -i` |
| `api.ipify.org`, `ifconfig.me`, `icanhazip.com` | Python `lib/utils/webserver.py` | — | public IP lookup |
| `www.google.com` | touch UI, CP | — | "online" indicator, polled |
| `cdnjs.cloudflare.com` | CP | — | icon font |
| `github.com`, apt, npm, nvm | first-boot script | — | first boot only |

Also on the image:
- A Samba share `\\magic-sorter-xxxxx\Public` → `/home/pi/public` (user `pi`, password `magicsorter`).
- The vendor app copies end-of-run `results_*.csv` files there, but **deletes all of them once 15 have built up**.

## What localsorter changes

- `endpoint` → `http://127.0.0.1:8765`, the local server in this repo. It implements every endpoint above, using Scryfall data stored on the Pi.
- The hosts above (vendor, Sentry, IP lookups) are blocked in `/etc/hosts`.
- Optional `--strict` firewall: only the `localsorter` user (the Scryfall sync) and apt may reach the internet.
- No vendor files are modified. `deploy/lockdown.sh --undo` restores the original behaviour.
