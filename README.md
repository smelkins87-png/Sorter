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
- **Recognition:**
  1. Find the card in the photo with OpenCV and straighten it.
  2. Compute a 256-bit perceptual hash of the whole card and a 64-bit hash of the art box.
  3. Compare against hashes of every paper printing's Scryfall image, about 20–100 ms per card.

  If several printings look identical (same art, same frame), the card is still reported, but marked "set not detected". That's the same flag the vendor uses.
- **Prices:** Scryfall's daily bulk file includes TCGplayer market prices (USD; Cardmarket EUR optional). It is refreshed nightly at about 04:30.

## Install (on the sorter, over SSH)

```bash
git clone <this repo> ~/localsorter && cd ~/localsorter
sudo deploy/install.sh                       # user, files, services (no data yet)
sudo systemctl start localsorter-sync        # first sync: card data + image hashes
journalctl -fu localsorter-sync              #   (takes a few hours, resumable)
curl -s http://127.0.0.1:8765/status         # "indexed_images" should be ~100k
sudo deploy/lockdown.sh                      # switch the sorter over + block vendor hosts
```

`sudo deploy/lockdown.sh --strict` also installs an outbound firewall. Only
the Scryfall sync and OS updates can reach the internet. The local network,
SSH, VNC and Samba keep working.

`sudo deploy/lockdown.sh --undo` puts everything back to the vendor setup.

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
- Foil can't be told apart from one photo. Use the sorter's "all cards are foil" setting for foil batches.
- Same-art reprints in the same frame style can't be told apart by image alone. They're flagged as "set not detected".
- The "popular cards" rule uses EDHREC rank among format-legal cards, an approximation of the vendor's list.
