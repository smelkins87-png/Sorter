"""The stop card: the card at the bottom of each feeder pile. When the sorter
photographs it, the pile is done: the feeder is switched off and the arm
moves on (lib/matcher/matcher.py checks for the title "stoptoken").

The vendor's servers recognised the stop card; nothing on the Pi did. Here
you show it once: sort it like any card, then press "This is my stop card"
on the review page. Its fingerprint (the same hashes as card recognition,
upright and upside down) is kept in the card database, and any photo that
close to it is answered as the stop card. Several can be kept.
"""
import logging
import threading
from datetime import datetime

import numpy as np

from . import imagehash

log = logging.getLogger("localsorter")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stop_cards (
    uuid     TEXT PRIMARY KEY,     -- the scan it was learnt from
    day      TEXT NOT NULL,
    hashes   BLOB NOT NULL,        -- full + art hash, upright then rotated
    added_at TEXT NOT NULL
);
"""

# Same physical card photographed twice: full + 2 x art bits stay well under
# the ~84 where different cards start (as for re-sorting, resort.py)
SAME_CARD = 50


def fingerprint(located):
    """Packed (full, art) hashes, upright and rotated 180 degrees."""
    card = located.card
    return [imagehash.card_hashes(card), imagehash.card_hashes(card.rotate(180))]


def _pack(pairs):
    return b"".join(full + art for full, art in pairs)


def _unpack(blob):
    pair = len(blob) // 2
    full_size = pair - ART_BYTES
    return [(blob[i:i + full_size], blob[i + full_size:i + pair]) for i in (0, pair)]


# The art hash is 64 bits (imagehash.card_hashes); the full hash is the rest
ART_BYTES = 8


def distance(a, b):
    """Smallest full + 2 x art distance between two fingerprints."""
    best = None

    for full_a, art_a in a:
        for full_b, art_b in b:
            d = (int(imagehash.hamming(full_a, np.frombuffer(full_b, dtype=np.uint8).reshape(1, -1))[0]) +
                 2 * int(imagehash.hamming(art_a, np.frombuffer(art_b, dtype=np.uint8).reshape(1, -1))[0]))
            best = d if best is None else min(best, d)

    return best


class StopCards:
    def __init__(self, conn, db_lock):
        self.conn = conn
        self.db_lock = db_lock
        self._lock = threading.Lock()
        self._cache_key = None
        self._cards = []

        with self.db_lock, self.conn:
            self.conn.executescript(SCHEMA)

    def _known(self):
        """Fingerprints of the stop cards, reloaded when the table changes
        (the review page writes it from another thread)."""
        with self.db_lock:
            key = tuple(self.conn.execute("SELECT COUNT(*), COALESCE(MAX(added_at), '') FROM stop_cards").fetchone())

            if key != self._cache_key:
                rows = self.conn.execute("SELECT uuid, hashes FROM stop_cards").fetchall()
                self._cards = [(r[0], _unpack(bytes(r[1]))) for r in rows]
                self._cache_key = key

        return self._cards

    def __bool__(self):
        return bool(self._known())

    def match(self, located):
        """The stop card this photo shows, or None."""
        known = self._known()

        if not known:
            return None

        query = fingerprint(located)
        best = min((distance(query, hashes), uuid) for uuid, hashes in known)

        return best[1] if best[0] <= SAME_CARD else None

    def add(self, uuid, day, image_bytes):
        located = imagehash.locate(image_bytes)

        with self.db_lock, self.conn:
            self.conn.execute(
                "INSERT INTO stop_cards (uuid, day, hashes, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(uuid) DO UPDATE SET hashes=excluded.hashes, added_at=excluded.added_at",
                (uuid, day, _pack(fingerprint(located)), datetime.now().isoformat(timespec="microseconds")))

        log.info("Stop card learnt from scan %s", uuid)

    def remove(self, uuid):
        with self.db_lock, self.conn:
            self.conn.execute("DELETE FROM stop_cards WHERE uuid = ?", (uuid,))

    def uuids(self):
        return {uuid for uuid, _ in self._known()}
