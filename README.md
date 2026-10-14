# localsorter: a self-contained Magic Sorter

Runs the Magic Sorter's card recognition **on the sorter's own Raspberry Pi**
instead of the vendor cloud. Card photos never leave the machine.
- Every scan is logged to a spreadsheet (CSV) on the sorter's network share.
- The only outbound traffic is a daily download of public card data and prices from [Scryfall](https://scryfall.com). It's the same file for everyone, and nothing about your cards is sent.

Supported: Magic: The Gathering on the v4.5 image (Raspberry Pi 4).
`docs/TEARDOWN.md` lists everything the stock software sends and where.

## How it works

```
camera ─► vendor sorter app ──POST /image/mtg──► localsorter (127.0.0.1:8765)
                    ▲                               │  find card outline, straighten
                    │                               │  perceptual hash ─► nearest of ~100k printings
                    └──── title/set/price/colors ◄──┘  log row + photo ─► \\sorter\Public\scans
```

- `localsorter` implements the same HTTP API as `api2.magic-sorter.com`: recognition, set lists, popular cards, results upload, and config backup.
- The vendor app, touchscreen and control panel only need their `endpoint` setting changed. No vendor files are modified.
- **Recognition, stage 1 (which card):**
  1. Find the card in the photo with OpenCV and straighten it.
  2. Compute a 256-bit perceptual hash of the whole card and a 64-bit hash of the art box.
  3. Compare against hashes of every paper printing's Scryfall image, about 20–100 ms per card.
- **Recognition, stage 2 (which printing):** runs when other printings share the card's artwork, as reprints often do.
  - **Set symbol:** the symbol's shape and rarity color are compared with close-ups of every look-alike printing. Only the pixels where those printings differ count, after correcting for exposure.
  - **Collector info:** the bottom text is read with Tesseract and checked against what each printing should say:
    - 2015+ frames: set code, collector number and rarity letter
    - 2003 frame: collector number
  - **Combining:** set code plus number agreeing settles it. Otherwise the symbol decides, and a weak text lead must agree with it. If the evidence doesn't single out one printing, the set is reported as **not detected** (the vendor's flag) instead of guessed.
  - **Every decision is logged** with its evidence (method, OCR text) in the scan CSV and the sorter's results.
  - **Extras:**
    - Foil detection on 2015+ frames: foils print ★ instead of •. Use the sorter's "detect foil" setting.
    - The card's language is read from the same line.
    - Printings that only exist in foil (or only non-foil) are marked automatically.
    - **Border color** (black / white, plus silver, gold and borderless as information): tells Beta (black) from Unlimited/Revised (white), which share art and have no set symbol. When a border blends into the sorter's background, it's reported as unsure rather than guessed. Black vs white works best on a mid-tone background.
- **Prices:** Scryfall's daily bulk file includes TCGplayer market prices (USD; Cardmarket EUR optional). It is refreshed nightly at about 04:30.

## Install: fresh SD card from a Windows PC

The Pi's only storage is its SD card. Use a **new card (32 GB or more)** and
keep your current one untouched: swapping cards takes you back to the
original setup in seconds.

**1. Flash the vendor image.** Download `magic_sorter_v4.5.zip` from the
vendor, extract `magic_sorter.img`, and write it to the new card with Rufus
(or Raspberry Pi Imager → "Use custom", or balenaEtcher).
After writing, **Windows will offer to format a disk: click Cancel.** That's
the Linux partition, which Windows can't read.

**2. Prepare the card from Windows.** A drive named **bootfs** appears:
- **Leave `magicsorter_config.txt` as it is** (placeholder values). Without your real device id, secret and download id, the unit never identifies itself to the vendor, and the vendor's self-updater can't download anything. Keep your old card's copy on your PC in case you ever want the vendor cloud back. With the placeholders, the Pi is named `magic-sorter-INSER`.
- Copy this repository onto bootfs as a folder named `localsorter`. On GitHub, use Code → Download ZIP, extract it, and rename the folder.

**3. First boot in the sorter.** Insert the card and power on. The vendor's
first-boot setup needs internet access: plug in Ethernet, or join the
"Magic Sorter WiFi Connection" network from your phone. It installs updates
and reboots a few times. Wait until the touchscreen app is up.

**4. Install, from your PC** (Windows Terminal or PowerShell; password `magicsorter`):

```
ssh pi@magic-sorter-INSER.local
passwd                                        # change the default password!
sudo smbpasswd pi                             # ...and the network share's
cd /boot/firmware/localsorter
sudo bash deploy/install.sh                   # user, files, services, Tesseract
sudo systemctl start localsorter-sync         # first sync: card data, image hashes and
journalctl -fu localsorter-sync               #   printing close-ups (hours, resumable; Ctrl+C to stop watching)
curl -s http://127.0.0.1:8765/status          # "indexed_images" should be ~100k when done
sudo bash deploy/lockdown.sh                  # switch over, block the vendor, firewall
sudo bash deploy/lockdown.sh --check          # see what's blocked and what got through
```

If `.local` names don't resolve on your network, use the Pi's IP address
(shown in the touchscreen's sidebar).

**No vendor contact, no automatic updates.** The stock software contacts the vendor on its own in several ways:
- the sorter software checks for, downloads and installs updates at every boot
- the touchscreen app does the same silently whenever it starts
- crash logs are uploaded automatically
- errors are reported to Sentry

`lockdown.sh` stops all of it in two independent layers:
1. **Host blocks:** every vendor and telemetry name in `/etc/hosts` points nowhere.
2. **Outbound firewall:** only the local network (SSH, VNC, Samba, your PC) and localsorter's nightly Scryfall sync may leave the Pi. Any address not on that list is dropped and logged, including future or unknown vendor addresses.

It also switches off Raspberry Pi OS's daily update checks. The image doesn't install OS updates by itself anyway.
`--allow-os-updates` lets `apt` through when *you* run it, and `--no-firewall` keeps only the host blocks.
Crash logs now go to `\\magic-sorter-INSER\Public\scans\logs` instead.

During first-boot setup, before the lockdown is in place, the vendor software may send one anonymous version check. With the placeholder config it can't download anything. For zero contact from the very first boot, block `magic-sorter.com` on your home router too, if it supports domain blocking.

`sudo bash deploy/lockdown.sh --undo` puts everything back to the vendor setup.

**Optional second photo after each drop (verifydrop):** switch it on in the touchscreen's
**System settings → Available plugins**. It takes effect from the next card, no restart.
- **What it does:** after each drop, the head moves the camera over the destination pile, using the same offset as the feeder photo, and photographs the top card. The local server checks it against the card that should be there.
- **When the photos disagree** (a different card, or two confident but different printings), the card is picked back up and put in the bin for unrecognised cards, for a manual check. Its record moves with it, tagged with the reason.
- **Other actions:** set `"verify_mismatch_action"` in `/etc/localsorter.json` to `"pause"` or `"warn"` instead.
- **Refining:** if the first photo couldn't confirm the printing and the second one can, the sorter's results are corrected.
- **Logs:** every check goes to `verify.csv` next to that day's scans, with the photo.
- **Cost:** a short move, a photo and an identification per card (expect roughly +0.5–1 s; measure it on your machine). Moving a card to the manual bin takes a few seconds more.

**Optional dark theme for the touchscreen:** `sudo bash deploy/theme.sh` (undo with `--undo`).
To change the background, replace `/home/pi/magic-sorter-touch/assets/background.jpg`.
Images on a black background blend best. The included artwork is SEGA's, so keep it to your own sorter and this repo private.

**Updating localsorter later:** copy the new version into the "Public" share (or onto bootfs), then
`cd` there and run `sudo bash deploy/install.sh` again. Settings, card data and scans are kept.

**What the first sync downloads** (measured with real Scryfall data, October 2026):

| Step | Amount | Stored on the Pi |
|---|---|---|
| Card data and prices | ~80 MB compressed, ~119k printings; under a minute on a PC | ~180 MB database |
| Fingerprints | ~113k small images, ~1.3 GB; about an hour on a PC | ~5 MB |
| Printing close-ups | ~79k normal-size images (72% of printings share their art), ~6 GB | ~400 MB |

On the Pi expect several hours in total. It's resumable, so an interruption only loses the current image.

**Keeping the card images (optional):** set `"image_cache_dir"` in `/etc/localsorter.json` (for example
`/var/lib/localsorter/image-cache`) and the sync keeps every image it downloads there. Only images that changed are
downloaded again, which makes re-indexing (after a recognition update, for example) offline-fast. Expect ~7 GB.
To put it on a USB drive instead (if the SD card is small), also add that folder to `ReadWritePaths=` in
`/etc/systemd/system/localsorter-sync.service`, then run `sudo systemctl daemon-reload`.
After that, the nightly sync only fetches prices plus whatever new cards and changed images appeared.

## Your scans

Open `\\magic-sorter-INSER\Public\scans` from your PC (user `pi`, password
`magicsorter` unless you changed it with `sudo smbpasswd pi`):

| Path | Contents |
|---|---|
| `2026-10-14/scans.csv` | one row per card: time, name, set, number, rarity, price, TCGplayer and Scryfall ids, match distance |
| | prices are the day's Scryfall prices; the sorter's price-trend rules use a smoothed 7-day average instead, so one odd day doesn't move a card to another bin |
| `2026-10-14/images/<uuid>.jpg` | the photo of each card (kept 30 days by default) |
| `sessions/results_*.csv` | the sorter's own end-of-run summaries (never auto-deleted here) |

## Review page

Open **`http://magic-sorter-INSER.local:8766`** (or the Pi's IP address, port 8766) from a PC or phone on your network:
- **Needs check:** cards that weren't recognised, cards whose set wasn't confirmed, and cards the second photo disagreed with (the manual-check bin).
- **✓ Correct:** confirms a card. **✎ Fix** lists every printing of the card with its set-symbol close-up next to your photo, plus a name search when it's a different card altogether.
- **Download collection CSV:** the day's cards with your corrections applied.
- **Tuning data:** every verdict is written to that day's `labels.csv`, which `python3 -m localsorter eval` reads directly.

**Re-sorting the manual-check bin (resortbin):** switch it on in the touchscreen's
**System settings → Available plugins**. When the feeders run out and the manual-check bin has cards:
1. **The sorter pauses.** The Logging page says how many cards are waiting and links to this review page.
2. **You review** them here, then press **Resume** on the touchscreen. Press **Stop** to finish without re-sorting.
3. **The machine sorts them again.** It measures the bin like a feeder, pulls the cards back out from the top, and sorts each one with your verdict. Pile order says which reviewed scan each card is, and a photo comparison confirms it.
4. **Cards still unresolved** go to the other bin for unrecognised cards, and the next pause offers another round.

While the sorter waits for you, and while it re-sorts, a banner at the top of this page says so (and how many cards are left).

Two identical-looking copies reviewed as different printings can't be told apart from a photo if the pile was disturbed. They go back to the manual check rather than being guessed.
It needs two bins for unrecognised cards: one is emptied while the other catches what still can't be sorted.

The page works offline. It uses no web fonts or online card images, so reviewing tells nobody what's in your collection.
To add a password, set `"review_password"` in `/etc/localsorter.json` (any user name works) and run `sudo systemctl restart localsorter`.
To turn the page off, set `"review_port": 0`.

## Tuning and testing

Settings live in `/etc/localsorter.json` (see `localsorter/settings.py`). Restart after changes:
`sudo systemctl restart localsorter`.

```bash
cd /opt/localsorter
# what does it think a photo is?
sudo -u localsorter python3 -m localsorter --config /etc/localsorter.json identify /home/pi/public/scans/2026-10-14/images/*.jpg
# accuracy on a labelled set: copy a day's scans.csv, fix any wrong set/collector_number rows, then
sudo -u localsorter python3 -m localsorter --config /etc/localsorter.json eval labelled.csv
```

The matching thresholds (`max_distance`, `min_margin`, `set_margin`) are
starting values from synthetic tests. `eval` prints the distances for correct
and wrong matches on your real scans, so you can set them properly.

### Accuracy on real cards (simulated photos)

`tools/realtest.py` turns real Scryfall card scans into simulated sorter photos and runs the full pipeline. Results from October 2026:
- **Test set:** 273 cards (200 random, 40 heavily reprinted, 33 old-border), plus 30 cards hidden from the index.
- **Right card:** 270; 3 not found, 0 wrong.
- **Hidden cards:** all 30 rejected.
- **Printing:** 222 confident and right, 48 flagged "set not detected" for a manual check, **0 confidently wrong**.
- **Text reading on 2015+ cards:** set code read 93% of the time, collector number 81%.

The photos come from the same scans as the references, so a real camera will do somewhat worse. Run `eval` on your own scans for the real numbers.

## Running it on a PC instead

The same server runs on any machine with Python 3.9+, numpy, Pillow and
opencv-python. Set `"listen_host": "0.0.0.0"` and point the sorter's endpoint
at `http://<pc-ip>:8765`.

## Development

```bash
pip install numpy pillow opencv-python-headless pytest
python3 -m pytest -q
```

Tests use synthetic cards (`tests/synthetic.py`). They check the pipeline
mechanics, not real-world accuracy.

## Limitations

- Magic only (Scryfall). English printings plus Scryfall's default printed language for each card.
- Foil detection reads the ★ on the collector line, so it only works on 2015-frame and newer cards. For older foils use the "all cards are foil" setting.
- 1993/1997-frame reprints with identical art and no set symbol (e.g. Revised, Unlimited) can't be told apart from the image. They're flagged "set not detected".
- Crop positions and thresholds come from synthetic tests. Calibrate them on real scans with `eval` before trusting set accuracy.
- The "popular cards" rule uses EDHREC rank among format-legal cards, an approximation of the vendor's list.
