"""In-memory hash index and card identification."""
import logging
import threading
from dataclasses import dataclass, field

import numpy as np

from . import imagehash

log = logging.getLogger("localsorter")

# Distance given to cards outside the requested sets
EXCLUDED = np.iinfo(np.int32).max


@dataclass
class Match:
    found: bool
    card_id: str = None
    face: int = 0
    distance: int = None
    margin: int = None
    # False when several printings (different sets) look the same
    set_detected: bool = True
    rotated: bool = False
    candidates: list = field(default_factory=list)

    def confidence(self):
        return {
            "distance": self.distance,
            "margin": self.margin,
            "set_detected": self.set_detected,
            "candidates": self.candidates[:5],
        }


class HashIndex:
    """All reference hashes as packed numpy matrices.

    ~110k card faces x 40 bytes = ~4.5 MB of RAM; a full scan is a couple of
    vectorised XOR/popcount passes, ~10-30 ms on a Raspberry Pi 4.
    """

    def __init__(self):
        self.card_ids = []
        self.faces = np.zeros(0, dtype=np.uint8)
        self.oracle = []
        self.set_codes = []
        self.full = np.zeros((0, imagehash.FULL_BYTES), dtype=np.uint8)
        self.art = np.zeros((0, imagehash.ART_BYTES), dtype=np.uint8)
        self._lock = threading.Lock()

    def __len__(self):
        return len(self.card_ids)

    @classmethod
    def from_db(cls, conn, include_digital=False):
        index = cls()
        index.reload(conn, include_digital)

        return index

    def reload(self, conn, include_digital=False):
        query = ("SELECT h.card_id, h.face, h.full_hash, h.art_hash, c.oracle_id, c.set_code "
                 "FROM hashes h JOIN cards c ON c.id = h.card_id")

        if not include_digital:
            query += " WHERE c.digital = 0"

        rows = conn.execute(query).fetchall()

        full = np.frombuffer(b"".join(r["full_hash"] for r in rows), dtype=np.uint8)
        art = np.frombuffer(b"".join(r["art_hash"] for r in rows), dtype=np.uint8)

        with self._lock:
            self.card_ids = [r["card_id"] for r in rows]
            self.faces = np.array([r["face"] for r in rows], dtype=np.uint8)
            self.oracle = [r["oracle_id"] for r in rows]
            self.set_codes = [r["set_code"] for r in rows]
            self.full = full.reshape(-1, imagehash.FULL_BYTES)
            self.art = art.reshape(-1, imagehash.ART_BYTES)

        log.info("Loaded %d reference images", len(rows))

    def distances(self, full_hash, art_hash):
        # Art box gets weight 2: 64 bits vs 256, but it is the most
        # distinctive part of a card and the least affected by framing
        return imagehash.hamming(full_hash, self.full) + 2 * imagehash.hamming(art_hash, self.art)

    def identify(self, image_bytes, max_distance, min_margin, set_margin, allowed_sets=None):
        queries = imagehash.query_hashes(image_bytes)

        with self._lock:
            if not self.card_ids:
                return Match(found=False)

            mask = None
            if allowed_sets:
                allowed = {s.upper() for s in allowed_sets}
                mask = np.array([code in allowed for code in self.set_codes])

                if not mask.any():
                    return Match(found=False)

            best = None

            for rotated, (full_hash, art_hash) in enumerate(queries):
                dist = self.distances(full_hash, art_hash).astype(np.int32)

                if mask is not None:
                    dist = np.where(mask, dist, EXCLUDED)

                if best is None or dist.min() < best[1].min():
                    best = (bool(rotated), dist)

            rotated, dist = best
            return self._decide(dist, rotated, max_distance, min_margin, set_margin)

    def _decide(self, dist, rotated, max_distance, min_margin, set_margin):
        order = [int(i) for i in np.argsort(dist)[:50] if dist[i] != EXCLUDED]
        top = order[0]
        best_dist = int(dist[top])
        best_oracle = self.oracle[top]

        candidates = [{
            "id": self.card_ids[i], "set": self.set_codes[i], "distance": int(dist[i])
        } for i in order[:10]]

        # Closest *different* card decides whether we trust the match at all
        other = next((int(dist[i]) for i in order if self.oracle[i] != best_oracle), None)
        margin = (other - best_dist) if other is not None else None

        match = Match(found=False, card_id=self.card_ids[top], face=int(self.faces[top]),
                      distance=best_dist, margin=margin, rotated=rotated, candidates=candidates)

        if best_dist > max_distance:
            return match

        if margin is not None and margin < min_margin:
            return match

        match.found = True

        # Same card, another set, nearly as close: we know the card but not the printing
        for i in order[1:]:
            if int(dist[i]) - best_dist > set_margin:
                break

            if self.oracle[i] == best_oracle and self.set_codes[i] != self.set_codes[top]:
                match.set_detected = False
                break

        return match
