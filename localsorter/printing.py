"""Second stage: which *printing* of the recognised card is this?

The perceptual hash finds the card and its artwork, but reprints that reuse
the same art and frame look identical at that scale. They differ only in:

* the set symbol (shape) and its colour (rarity): black, silver, gold,
  orange-red;
* the collector info at the bottom: set code, collector number, rarity
  letter, language, and a star on foils (2015 frame onwards), or the
  collector number (2003 frame).

For every look-alike printing we keep close-ups of those two areas, cut from
Scryfall's image, and compare them with the same areas of the scan (cut
sharply from the full-resolution photo). The bottom text is also read with
Tesseract and checked against what each printing should say. Evidence is
combined; when it doesn't single out one printing, the set is reported as
not detected rather than guessed.
"""
import io
import logging
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import carddb, imagehash, ocr

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

log = logging.getLogger("localsorter")

# Card space for the close-ups: Scryfall's "normal" image width
REGION_CARD_W = 488
# Right end of the type line, where the set symbol sits on every frame since
# 1997 (fractions of the card)
SYMBOL_BOX = (0.80, 0.535, 0.965, 0.635)
BOTTOM_BOX = ocr.BOTTOM_BOX
# The scan's window is a bit larger than the reference, to absorb small
# errors in the card outline
SEARCH_PAD = 0.025
# Other printings of the same card this close in the hash ranking (bits) are
# considered too, even with a different illustration id
HASH_SLACK = 12


def _pad(box, pad):
    return (max(0.0, box[0] - pad), max(0.0, box[1] - pad), min(1.0, box[2] + pad), min(1.0, box[3] + pad))


def _encode(image, quality=92):
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)

    return buf.getvalue()


def _decode(blob, mode):
    return np.asarray(Image.open(io.BytesIO(blob)).convert(mode))


def reference_regions(image_bytes):
    """Close-ups of a Scryfall card image: (symbol JPEG, bottom JPEG)."""
    image = imagehash.load_image(image_bytes)
    symbol = imagehash.reference_region(image, SYMBOL_BOX, REGION_CARD_W)
    bottom = imagehash.reference_region(image, BOTTOM_BOX, REGION_CARD_W).convert("L")

    return _encode(symbol), _encode(bottom, 90)


def _ncc(window, template):
    """Best normalised cross-correlation of template anywhere in window,
    and where. Both greyscale uint8."""
    if window.shape[0] < template.shape[0] or window.shape[1] < template.shape[1]:
        return -1.0, (0, 0)

    result = cv2.matchTemplate(window.astype(np.float32), template.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    _, best, _, location = cv2.minMaxLoc(result)

    return float(best), location


@dataclass
class ScanRegions:
    symbol: np.ndarray   # RGB windows, slightly larger than the references
    bottom: np.ndarray


def scan_regions(located):
    symbol = located.region(_pad(SYMBOL_BOX, SEARCH_PAD), REGION_CARD_W)
    bottom = located.region(_pad(BOTTOM_BOX, SEARCH_PAD), REGION_CARD_W)

    if symbol is None or bottom is None:
        return None

    return ScanRegions(np.asarray(symbol), np.asarray(bottom.convert("RGB")))


def _lab(rgb):
    return cv2.cvtColor(np.ascontiguousarray(rgb, dtype=np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)


# Pixels whose colour varies this much (Lab units) across the look-alike
# printings are the ones that tell them apart
DIFFERENCE_THRESHOLD = 12.0
MIN_DIFFERENCE_PIXELS = 12


def _compare_area(window_rgb, references):
    """Distance from the scan to each reference, measured only where the
    references differ from each other.

    references: {card_id: RGB array}, all the same size. Returns
    ({card_id: mean Lab distance}, number of differing pixels), or (None, 0)
    when the references are identical there."""
    ids = list(references)
    stack = np.stack([_lab(references[i]) for i in ids])            # N x h x w x 3
    mean = stack.mean(axis=0)
    spread = np.abs(stack - mean).max(axis=0).max(axis=-1)           # h x w
    mask = spread > DIFFERENCE_THRESHOLD
    mask = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)

    if mask.sum() < MIN_DIFFERENCE_PIXELS:
        return None, 0

    # Line the scan up using what all printings share (frame, type bar...)
    h, w = mean.shape[:2]
    _, (x, y) = _ncc(cv2.cvtColor(np.ascontiguousarray(window_rgb, dtype=np.uint8), cv2.COLOR_RGB2GRAY),
                     mean[..., 0])
    scan = _lab(window_rgb)[y:y + h, x:x + w]

    if scan.shape[:2] != (h, w):
        return None, 0

    # Undo exposure/white balance differences, fitted on the shared pixels:
    # lightness gets gain + offset, colour channels an offset
    shared = ~mask
    if shared.sum() > 50:
        ref_l, scan_l = mean[..., 0][shared], scan[..., 0][shared]
        gain = np.std(ref_l) / max(np.std(scan_l), 1e-3)
        gain = float(np.clip(gain, 0.5, 2.0))
        scan = scan.copy()
        scan[..., 0] = (scan[..., 0] - scan_l.mean()) * gain + ref_l.mean()
        for channel in (1, 2):
            scan[..., channel] += mean[..., channel][shared].mean() - scan[..., channel][shared].mean()

    distances = {i: float(np.linalg.norm(scan[mask] - stack[k][mask], axis=-1).mean()) for k, i in enumerate(ids)}

    return distances, int(mask.sum())


def compare_regions(scan, references):
    """references: {card_id: (symbol_blob, bottom_blob)}. Returns
    {card_id: {"symbol": d, "bottom": d, "total": d}}: mean colour distance
    (lower is closer) over the pixels where the printings differ."""
    ids = list(references)
    result = {i: {"symbol": None, "bottom": None, "total": 0.0} for i in ids}

    # The bottom strip is cut at the same modest resolution as the symbol;
    # its tiny text is better judged by OCR, so it counts half
    for key, window, index, weight in (("symbol", scan.symbol, 0, 1.0), ("bottom", scan.bottom, 1, 0.5)):
        refs = {i: _decode(references[i][index], "RGB") for i in ids}
        distances, pixels = _compare_area(window, refs)

        if distances is None:
            continue

        for i, distance in distances.items():
            result[i][key] = round(distance, 2)
            result[i]["total"] = round(result[i]["total"] + weight * distance, 2)

    return result


@dataclass
class Decision:
    card_id: str
    set_detected: bool
    foil: int = None          # None: unknown
    lang: str = None          # None: not read
    method: str = "hash"
    evidence: dict = field(default_factory=dict)


class PrintingResolver:
    def __init__(self, conn, db_lock, tesseract=None, ocr_mode="auto", region_margin=6.0):
        self.conn = conn
        self.db_lock = db_lock
        self.tesseract = tesseract
        self.ocr_mode = ocr_mode
        self.region_margin = region_margin

    def _rows(self, ids):
        ids = list(ids)

        with self.db_lock:
            return {r["id"]: r for r in self.conn.execute(
                "SELECT * FROM cards WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}

    def _regions(self, ids, face):
        ids = list(ids)

        with self.db_lock:
            return {r["card_id"]: (r["symbol"], r["bottom"]) for r in self.conn.execute(
                "SELECT card_id, symbol, bottom FROM regions WHERE face = ? AND card_id IN (%s)"
                % ",".join("?" * len(ids)), [face] + ids)}

    def _printings_named_in(self, text, oracle_id):
        """Printings of the card whose set code appears in the OCR text (finds
        printings the look-alike grouping missed, e.g. missing illustration
        ids on Scryfall)."""
        tokens = {ocr.canon(t) for t in ocr._TOKEN.findall(text)}

        with self.db_lock:
            rows = self.conn.execute("SELECT id, set_code FROM cards WHERE oracle_id = ? AND digital = 0",
                                     (oracle_id,)).fetchall()

        return [r["id"] for r in rows if ocr.canon(r["set_code"]) in tokens]

    def resolve(self, match, foil_mode=0, lang_hint="en", allowed_sets=None):
        best_id = match.card_id

        with self.db_lock:
            pool = set(carddb.lookalikes(self.conn, best_id))

        # Same card a hair away in the hash ranking also qualifies
        near = {c["id"] for c in match.candidates[:10] if c["distance"] - match.distance <= HASH_SLACK}
        rows = self._rows(pool | near)
        best_row = rows[best_id]
        pool |= {cid for cid in near if rows[cid]["oracle_id"] == best_row["oracle_id"]}

        if allowed_sets:
            allowed = {s.upper() for s in allowed_sets}
            pool = {cid for cid in pool if rows[cid]["set_code"] in allowed} or {best_id}

        decision = Decision(card_id=best_id, set_detected=True)
        evidence = decision.evidence
        distinct = {(rows[c]["set_code"], rows[c]["collector_number"]) for c in pool}

        frames = {rows[c]["frame"] for c in pool}
        sides = {ocr.SIDES[f] for f in frames if f in ocr.SIDES}
        foil_choice = len(carddb.finishes_of(best_row) & {"foil", "nonfoil"}) == 2
        want_ocr = self.tesseract is not None and self.tesseract.available and sides and (
            self.ocr_mode == "always" or (self.ocr_mode == "auto" and (
                len(distinct) > 1 or lang_hint == "auto" or (foil_mode == 2 and foil_choice))))

        text = ""
        if want_ocr and match.located is not None:
            text = ocr.read_bottom(match.located, self.tesseract, sides)
            evidence["ocr_text"] = " ".join(text.split())[:200]

            extra = set(self._printings_named_in(text, best_row["oracle_id"])) - pool
            if extra and not allowed_sets:
                rows.update(self._rows(extra))
                pool |= extra
                distinct = {(rows[c]["set_code"], rows[c]["collector_number"]) for c in pool}

        if len(distinct) > 1:
            self._choose(decision, match, pool, rows, text)

        chosen = rows[decision.card_id]
        parsed = ocr.parse(text) if text else {}
        decision.lang = parsed.get("lang")
        decision.foil = self._foil(chosen, foil_mode, parsed)

        return decision

    def _choose(self, decision, match, pool, rows, text):
        evidence = decision.evidence
        candidates = sorted(pool)
        label = lambda c: rows[c]["set_code"] + "#" + rows[c]["collector_number"]

        # 1. What the card says about itself
        ocr_scores = ocr.score_candidates(text, [rows[c] for c in candidates]) if text else {}
        if ocr_scores:
            evidence["ocr"] = {label(c): s for c, (s, _) in ocr_scores.items() if s}

        top = max((s for s, _ in ocr_scores.values()), default=0)
        ocr_pick = None

        if top >= 3:
            leaders = [c for c in candidates if ocr_scores[c][0] == top]
            runner_up = max((s for c, (s, _) in ocr_scores.items() if c not in leaders), default=0)

            if len(leaders) == 1:
                ocr_pick = leaders[0]

                # Set code + number agree, or a clear lead over the rest
                if top >= 6 or top - runner_up >= 3:
                    decision.card_id = ocr_pick
                    decision.method = "ocr"
                    return
            else:
                # e.g. the number matched several printings: symbol decides
                candidates = leaders

        # 2. Set symbol and bottom strip close-ups
        scan = scan_regions(match.located) if match.located is not None else None
        refs = self._regions(candidates, match.face) if scan is not None else {}

        if scan is not None and len(refs) >= 2:
            scored = compare_regions(scan, {c: refs[c] for c in candidates if c in refs})
            evidence["regions"] = {label(c): v for c, v in scored.items()}
            ranking = sorted(scored, key=lambda c: scored[c]["total"])
            winner = ranking[0]
            best, second = scored[winner]["total"], scored[ranking[1]]["total"]
            margin = second - best
            evidence["region_margin"] = round(margin, 2)

            # Every candidate needs a close-up before it can be ruled out, and
            # the winner must be clearly closer than the runner-up
            complete = len(scored) == len(candidates)
            clear = second > 0 and margin >= self.region_margin and margin / second >= 0.2
            decision.card_id = winner
            decision.method = "regions"

            if ocr_pick is not None:
                # Weak OCR lead: trust it only when the close-ups agree
                decision.method = "ocr+regions"
                decision.set_detected = complete and winner == ocr_pick
            else:
                decision.set_detected = complete and clear
            return

        # 3. Nothing to go on: keep the best guess, but say the set is a guess
        decision.card_id = ocr_pick or (match.card_id if match.card_id in candidates else candidates[0])
        decision.set_detected = False

    @staticmethod
    def _foil(row, foil_mode, parsed):
        finishes = carddb.finishes_of(row)

        if finishes and "nonfoil" not in finishes:
            return 1          # only ever printed foil (or etched)
        if finishes == {"nonfoil"}:
            return 0
        if foil_mode == 1:
            return 1          # "all my cards are foil"
        if foil_mode == 2 and row["frame"] in ("2015", "future") and parsed.get("foil") is not None:
            return 1 if parsed["foil"] else 0

        return None if foil_mode == 2 else 0
