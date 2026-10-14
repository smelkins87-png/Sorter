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
- Copy `magicsorter_config.txt` from your *current* card's bootfs over the new one. It holds your device id; it's only needed if you ever switch back to the vendor cloud, and it names the Pi `magic-sorter-xxxxx`.
- Copy this repository onto bootfs as a folder named `localsorter`. On GitHub, use Code → Download ZIP, extract it, and rename the folder.

**3. First boot in the sorter.** Insert the card and power on. The vendor's
first-boot setup needs internet access: plug in Ethernet, or join the
"Magic Sorter WiFi Connection" network from your phone. It installs updates
and reboots a few times. Wait until the touchscreen app is up.

**4. Install, from your PC** (Windows Terminal or PowerShell; password `magicsorter`):

```
ssh pi@magic-sorter-xxxxx.local
passwd                                        # change the default password!
cd /boot/firmware/localsorter
sudo bash deploy/install.sh                   # user, files, services, Tesseract
sudo systemctl start localsorter-sync         # first sync: card data, image hashes and
journalctl -fu localsorter-sync               #   printing close-ups (hours, resumable; Ctrl+C to stop watching)
curl -s http://127.0.0.1:8765/status          # "indexed_images" should be ~100k when done
sudo bash deploy/lockdown.sh                  # switch the sorter over + block vendor hosts
```

If `.local` names don't resolve on your network, use the Pi's IP address
(shown in the touchscreen's sidebar).

`sudo bash deploy/lockdown.sh --strict` also installs an outbound firewall. Only
the Scryfall sync and OS updates can reach the internet. The local network,
SSH, VNC and Samba keep working.

`sudo bash deploy/lockdown.sh --undo` puts everything back to the vendor setup.

**Updating localsorter later:** copy the new version into the "Public" share (or onto bootfs), then
`cd` there and run `sudo bash deploy/install.sh` again. Settings, card data and scans are kept.

## Your scans

Open `\\magic-sorter-xxxxx\Public\scans` from your PC (user `pi`, password
`magicsorter`, the vendor default; consider changing it with `sudo smbpasswd pi`):

| Path | Contents |
|---|---|
| `2026-10-14/scans.csv` | one row per card: time, name, set, number, rarity, price, TCGplayer and Scryfall ids, match distance |
| `2026-10-14/images/<uuid>.jpg` | the photo of each card (kept 30 days by default) |
| `sessions/results_*.csv` | the sorter's own end-of-run summaries (never auto-deleted here) |

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
