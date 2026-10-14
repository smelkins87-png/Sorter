"""Runtime settings.

Defaults suit the Raspberry Pi install (see deploy/install.sh). Every value can
be overridden by a JSON file passed with --config, or by the LOCALSORTER_CONFIG
environment variable pointing at one.
"""
import json
import os
from dataclasses import asdict, dataclass, fields


@dataclass
class Settings:
    # Where the SQLite card database (cards, sets, prices, image hashes) lives
    data_dir: str = "/var/lib/localsorter"

    # HTTP server the sorter talks to. Keep 127.0.0.1 when everything runs on
    # the Pi; use 0.0.0.0 when the server runs on a PC and the Pi connects to it
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765

    # Where every scan is logged (CSV per day) and photos are saved. The Pi
    # image already shares /home/pi/public over Samba as \\<sorter>\Public
    export_dir: str = "/home/pi/public/scans"
    save_images: bool = True
    # Delete saved photos older than this many days (0 = keep forever)
    image_retention_days: int = 30

    # Scryfall image size used to build the recognition index: "small"
    # (146x204, ~10 KB each) is plenty for perceptual hashing
    index_image_size: str = "small"
    download_workers: int = 4
    # Delay between image downloads per worker (seconds), to be polite
    download_delay: float = 0.1

    # Matching thresholds, in bits of Hamming distance (0 = identical).
    # Tune with `python -m localsorter eval` against your own scans
    max_distance: int = 80
    # Best match must beat the best *different card* by at least this much
    min_margin: int = 15
    # Printings of the same card closer than this are treated as ambiguous
    # (set is then reported as "not detected")
    set_margin: int = 4

    # Telling apart printings that share artwork (see printing.py).
    # Close-ups of set symbol + collector info are cut from this Scryfall
    # image size for every such printing ("normal" is 488x680)
    region_image_size: str = "normal"
    # Read the collector info with Tesseract: "auto" (only when several
    # printings look alike, or to detect foil/language), "always", or "off"
    ocr: str = "auto"
    tesseract_cmd: str = "tesseract"
    # How much closer (mean Lab colour distance over the pixels where the
    # printings differ) the winning printing's close-ups must be
    region_margin: float = 6.0
    # Use the border colour (black/white/silver/gold/borderless) to tell
    # printings apart, e.g. Beta (black) from Unlimited (white)
    border_check: bool = True

    # Second photo after each drop (the "verifydrop" plugin, switched on in the
    # touchscreen's System settings > Available plugins). When the second photo
    # disagrees with the first: "manual_bin" (move the card to the bin for
    # unrecognised cards), "pause" the sorter, or just "warn" in the log
    verify_mismatch_action: str = "manual_bin"

    # Price returned to the sorter: "usd" (TCGplayer market via Scryfall) or "eur"
    price_currency: str = "usd"

    # Include Scryfall's "digital" (Arena/MTGO-only) printings? Never physical
    include_digital: bool = False

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "cards.sqlite")

    @classmethod
    def load(cls, path=None):
        path = path or os.environ.get("LOCALSORTER_CONFIG")
        settings = cls()

        if path and os.path.exists(path):
            with open(path, "r") as handle:
                data = json.load(handle)

            known = {f.name for f in fields(cls)}
            unknown = set(data) - known

            if unknown:
                raise ValueError("Unknown settings in %s: %s" % (path, ", ".join(sorted(unknown))))

            for key, value in data.items():
                setattr(settings, key, value)

        return settings

    def to_dict(self):
        return asdict(self)
