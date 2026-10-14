# Card data for localsorter

Prebuilt card data, so the sorter skips its first download (several hours
of card images). Built on 14 October 2026 from Scryfall's data, with that
day's prices. The code lives on the `claude/magic-sorter-local-processing-0prj28`
branch; this branch only holds data.

| Folder | What | Size | Where it goes |
|---|---|---|---|
| `card-data/core` | 118,578 cards, 1,059 sets, prices, 113,355 recognition fingerprints | 41 MB | on the SD card, inside the `localsorter` folder |
| `card-data/closeups` | set-symbol and bottom-text close-ups of 79,861 look-alike printings | 1.3 GB in 14 pieces | the sorter's network folder, after first boot |

## How to use it

1. **Download this branch:** on GitHub, switch to the `card-data` branch, then
   Code → Download ZIP. Extract it.
2. **Making the SD card:** copy `card-data/core` into the `localsorter` folder
   on the card's **bootfs** drive, so you have `localsorter\card-data\core`.
3. **After first boot,** before installing: open `\\magic-sorter-INSER\Public`
   in File Explorer and copy the whole `card-data` folder there (it only needs
   `closeups`, but the whole folder is fine).
4. **Install as usual** (`sudo bash deploy/install.sh`). It finds both, checks
   every piece, and loads them. The first sync then only fetches newer prices
   and cards: minutes instead of hours.

Afterwards you can delete `card-data` from the Public folder to free space.
If a piece is missing or damaged, the installer names it; copy it again and
re-run the installer.

The pieces are made by `python3 -m localsorter pack` and loaded by
`python3 -m localsorter seed` (see `localsorter/seed.py` on the code branch).
