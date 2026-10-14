"""Writes every scan to a daily CSV (opens in Excel/LibreOffice/Sheets) and,
optionally, keeps the photo next to it.

Layout under export_dir (default /home/pi/public/scans, i.e. the sorter's
"Public" network share):

    2026-10-14/scans.csv
    2026-10-14/images/<uuid>.jpg
    sessions/results_*.csv     <- end-of-run summaries the sorter uploads

The vendor software deletes every results*.csv in ~/public once there are 15,
so we keep our own files in a separate folder it never touches.
"""
import csv
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger("localsorter")

COLUMNS = ["timestamp", "uuid", "status", "title", "set", "set_name", "collector_number", "rarity",
           "lang", "foil", "condition", "price", "currency", "tcgplayer_id", "scryfall_id",
           "distance", "margin", "set_detected", "printing_method", "ocr_text", "image"]


class Exporter:
    def __init__(self, export_dir, save_images=True, retention_days=0, currency="usd"):
        self.export_dir = export_dir
        self.save_images = save_images
        self.retention_days = retention_days
        self.currency = currency
        self._lock = threading.Lock()
        self._last_cleanup = 0

    def _day_dir(self, now):
        path = os.path.join(self.export_dir, now.strftime("%Y-%m-%d"))
        os.makedirs(path, exist_ok=True)

        return path

    def record(self, card_uuid, image_bytes, card=None, match=None, condition="NM", foil=0):
        """Appends one scan. `card` is the dict returned to the sorter (None
        when the card was not recognised)."""
        now = datetime.now()

        with self._lock:
            day_dir = self._day_dir(now)
            image_rel = ""

            if self.save_images and image_bytes:
                image_dir = os.path.join(day_dir, "images")
                os.makedirs(image_dir, exist_ok=True)
                image_rel = os.path.join("images", card_uuid + ".jpg")

                with open(os.path.join(day_dir, image_rel), "wb") as handle:
                    handle.write(image_bytes)

            card = card or {}
            row = {
                "timestamp": now.isoformat(timespec="seconds"),
                "uuid": card_uuid,
                "status": "found" if card else "not_found",
                "title": card.get("title", ""),
                "set": card.get("set", ""),
                "set_name": card.get("set_name", ""),
                "collector_number": card.get("num", ""),
                "rarity": card.get("rarity", ""),
                "lang": card.get("lang", ""),
                "foil": card.get("foil", foil),
                "condition": card.get("condition", condition),
                "price": card.get("price", ""),
                "currency": self.currency.upper() if card else "",
                "tcgplayer_id": card.get("ecommerce_id", "") or "",
                "scryfall_id": card.get("ext_id", ""),
                "distance": getattr(match, "distance", ""),
                "margin": getattr(match, "margin", ""),
                "set_detected": int(match.set_detected) if (match and card) else "",
                "printing_method": (getattr(match, "printing", None) or {}).get("method", "") if card else "",
                "ocr_text": (getattr(match, "printing", None) or {}).get("ocr_text", ""),
                "image": image_rel,
            }

            path = os.path.join(day_dir, "scans.csv")
            new_file = not os.path.exists(path)

            # utf-8-sig so Excel shows accented names correctly
            with open(path, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=COLUMNS)

                if new_file:
                    writer.writeheader()

                writer.writerow(row)

        self._maybe_cleanup()

    def store_session(self, name, data):
        """Keeps a copy of an end-of-run results file sent by the sorter."""
        session_dir = os.path.join(self.export_dir, "sessions")
        os.makedirs(session_dir, exist_ok=True)
        safe = os.path.basename(name)

        with open(os.path.join(session_dir, safe), "wb") as handle:
            handle.write(data)

        return os.path.join(session_dir, safe)

    def _maybe_cleanup(self):
        if not self.retention_days or time.time() - self._last_cleanup < 3600:
            return

        self._last_cleanup = time.time()
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")

        try:
            entries = os.listdir(self.export_dir)
        except OSError:
            return

        for entry in entries:
            image_dir = os.path.join(self.export_dir, entry, "images")

            # Only photos expire; the CSV logs are tiny and kept forever
            if len(entry) == 10 and entry < cutoff and os.path.isdir(image_dir):
                log.info("Removing photos older than %d days: %s", self.retention_days, image_dir)
                shutil.rmtree(image_dir, ignore_errors=True)
