"""Re-sorting the manual-check bin with the verdicts from the review page.

The sorter plugin (plugin/localsorter) pulls the cards back out of the bin
one by one and tells the server, before starting, which scans are in the bin
from top to bottom. Each photo that arrives meanwhile is matched to one of
those scans (the same physical card, photographed minutes earlier) and gets
the verdict given on the review page instead of a new guess.

Pile order is the primary key: the card on top was dropped last. The photo
check confirms it, and if the pile was disturbed the photo picks among all
remaining scans. Two look-alike copies with different verdicts can't be
told apart that way: such a card goes back to the manual check.
"""
import glob
import logging
import os
import threading

import numpy as np

from . import imagehash

log = logging.getLogger("localsorter")

# Same card photographed twice: full + 2 x art bits, well under the ~84 where
# different cards start (measured on real cards)
SAME_CARD = 50
# A runner-up this close (with another verdict) makes the match ambiguous
AMBIGUOUS = 15
DAYS_SEARCHED = 14


def _hashes(located):
    card = located.card
    return [imagehash.card_hashes(card), imagehash.card_hashes(card.rotate(180))]


def _distance(a, b):
    """Smallest distance between two cards' (upright, rotated) hash pairs."""
    best = None

    for full_a, art_a in a:
        for full_b, art_b in b:
            d = (int(imagehash.hamming(full_a, np.frombuffer(full_b, dtype=np.uint8).reshape(1, -1))[0]) +
                 2 * int(imagehash.hamming(art_a, np.frombuffer(art_b, dtype=np.uint8).reshape(1, -1))[0]))
            best = d if best is None else min(best, d)

    return best


class ResortSession:
    def __init__(self, export_dir, conn, db_lock, uuids):
        """uuids: the scans in the bin, top of the pile first."""
        self._lock = threading.Lock()
        self.queue = []
        self.candidates = {}

        with db_lock:
            labels = {r["uuid"]: dict(r) for r in conn.execute(
                "SELECT uuid, card_id, status FROM labels WHERE uuid IN (%s)" % ",".join("?" * len(uuids)),
                list(uuids))} if uuids else {}

        for uuid in uuids:
            path = self._find_image(export_dir, uuid)

            if path is None:
                log.info("Re-sort: no photo kept for scan %s, it will be identified afresh", uuid)
                continue

            with open(path, "rb") as handle:
                hashes = _hashes(imagehash.locate(handle.read()))

            self.queue.append(uuid)
            self.candidates[uuid] = {"hashes": hashes, "label": labels.get(uuid)}

        log.info("Re-sort started: %d cards, %d reviewed", len(self.queue),
                 sum(1 for c in self.candidates.values() if c["label"]))

    @staticmethod
    def _find_image(export_dir, uuid):
        if not uuid or "/" in uuid or ".." in uuid:
            return None

        days = sorted(glob.glob(os.path.join(export_dir, "????-??-??")), reverse=True)[:DAYS_SEARCHED]

        for day in days:
            path = os.path.join(day, "images", uuid + ".jpg")
            if os.path.exists(path):
                return path

        return None

    def match(self, located):
        """The reviewed verdict for a pulled card.

        Returns (kind, label): kind is "verdict" (label holds card_id and
        status), "unreviewed" (identify it normally), "ambiguous" or
        "unknown" (photo matches none of the bin's scans)."""
        hashes = _hashes(located)

        with self._lock:
            if not self.queue:
                return "unknown", None

            expected = self.queue[0]
            chosen = None

            # 1. The pile's order says it's the scan on top
            if _distance(hashes, self.candidates[expected]["hashes"]) <= SAME_CARD:
                chosen = expected
            else:
                # 2. Pile disturbed: the closest of the remaining scans
                ranked = sorted((_distance(hashes, self.candidates[u]["hashes"]), u) for u in self.queue)
                best_distance, best = ranked[0]

                if best_distance > SAME_CARD:
                    return "unknown", None

                verdict = self._verdict(best)
                for distance, other in ranked[1:]:
                    if distance - best_distance >= AMBIGUOUS:
                        break
                    if self._verdict(other) != verdict:
                        # Identical-looking copies with different verdicts
                        self.queue.remove(best)
                        return "ambiguous", None

                chosen = best

            self.queue.remove(chosen)
            label = self.candidates[chosen]["label"]

            return ("verdict", dict(label, uuid=chosen)) if label else ("unreviewed", None)

    def _verdict(self, uuid):
        label = self.candidates[uuid]["label"]
        return (label["status"], label["card_id"]) if label else None
