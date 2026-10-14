"""Reads the collector info printed at the bottom of a card (Tesseract).

What is printed there depends on the frame:

* 2015 frame and later ("M15"), two lines bottom left:
      123/264 R              or, since 2023,   0123 R
      DOM • EN  <artist>                        WOE • EN  <artist>
  set code, collector number, rarity letter, language, and a star instead of
  the dot on foils.
* 2003 frame: "123/249" at the bottom right, no set code.
* Older frames: nothing useful.

Free-form parsing of 6-point text is fragile, so the main entry point is
score_candidates(): it checks which candidate printing's *expected* strings
appear in what Tesseract read, tolerating the usual confusions (0/O, 1/I...).
"""
import io
import logging
import os
import re
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger("localsorter")

# Bottom strip, as fractions of the card (both corners: the 2003 frame puts the
# number on the right)
BOTTOM_BOX = (0.02, 0.900, 0.98, 0.990)
# Card space the strip is cut in: ~20 px per mm, the size Tesseract likes
OCR_CARD_W = 1400

# Language codes printed on cards -> Scryfall codes
PRINTED_LANGS = {"EN": "en", "DE": "de", "FR": "fr", "IT": "it", "ES": "es", "SP": "es", "PT": "pt",
                 "JP": "ja", "JA": "ja", "KO": "ko", "KR": "ko", "RU": "ru", "CS": "zhs", "CT": "zht",
                 "PH": "ph"}
RARITY_LETTERS = {"common": "C", "uncommon": "U", "rare": "R", "mythic": "M", "special": "S", "bonus": "S"}

# Characters Tesseract mixes up, mapped to one representative
_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "!": "1",
                             "S": "5", "B": "8", "Z": "2", "G": "6", "T": "7"})
_STAR = set("*\u2605\u2606+xX%#")
# Tesseract reads the small bullet as all sorts of things
_DOT = set("\u2022\u00b7.,-~:\u00b0\u00ab\u00bb\u00a2eo")


def canon(text):
    """Upper-case with confusable characters folded together."""
    return text.upper().translate(_CONFUSABLE)


class Tesseract:
    def __init__(self, command="tesseract", timeout=10):
        self.command = shutil.which(command)
        self.timeout = timeout

        if not self.command:
            log.warning("Tesseract not found: collector info will not be read "
                        "(sudo apt-get install tesseract-ocr)")

    @property
    def available(self):
        return self.command is not None

    def read(self, image, psm=6):
        """Text in a PIL image, or "" on failure."""
        if not self.available:
            return ""

        buf = io.BytesIO()
        image.save(buf, "PNG")

        try:
            result = subprocess.run(
                # do_invert=0: we already hand it dark-on-light text, and
                # skipping its inverted second pass roughly halves the time
                [self.command, "stdin", "stdout", "--psm", str(psm), "-l", "eng",
                 "-c", "tessedit_do_invert=0"],
                input=buf.getvalue(), capture_output=True, timeout=self.timeout, check=False,
                # Tesseract's OpenMP threads make tiny images *slower*, and on
                # a busy 4-core Pi can stall for many seconds
                env=dict(os.environ, OMP_THREAD_LIMIT="1"))
        except (OSError, subprocess.TimeoutExpired) as error:
            log.warning("Tesseract failed: %s", error)
            return ""

        return result.stdout.decode("utf-8", "replace")


def prepare(strip):
    """Dark-on-light, high contrast greyscale for Tesseract. The info text is
    printed white on the black border, or black on light borders."""
    gray = ImageOps.grayscale(strip)
    pixels = np.asarray(gray, dtype=np.float32)

    # Mostly dark strip = light text on black: invert
    if np.median(pixels) < 110:
        gray = ImageOps.invert(gray)

    gray = ImageOps.autocontrast(gray, cutoff=1)

    # A white margin helps Tesseract find the first characters
    return ImageOps.expand(gray, border=20, fill=255)


# Where the collector info sits for each frame style: two lines bottom left
# since 2015; earlier, inside the copyright line ("... 41/155"), left of
# centre (2003) or centred (1997), so the whole strip is read
SIDES = {"2015": "left", "future": "left", "2003": "full", "1997": "full"}


def read_bottom(located, tesseract, sides=("left", "right")):
    """OCR of the bottom strip of a located card. Returns the raw text.

    `sides` limits reading to the half (or halves) where the frames of the
    candidate printings print their collector info: each Tesseract run costs
    a few hundred ms on a Pi."""
    if not tesseract.available or not sides:
        return ""

    strip = located.region(BOTTOM_BOX, OCR_CARD_W)

    if strip is None:
        return ""

    if "full" in sides:
        return tesseract.read(prepare(strip))

    halves = {"left": strip.crop((0, 0, strip.width // 2, strip.height)),
              "right": strip.crop((strip.width // 2, 0, strip.width, strip.height))}

    # Each half separately: the two text blocks have different baselines and
    # Tesseract's layout analysis merges them badly otherwise
    return "\n".join(tesseract.read(prepare(halves[side])) for side in ("left", "right") if side in sides)


_TOKEN = re.compile(r"[A-Za-z0-9★☆•·*+/#%.,-]+")


def parse(text):
    """Best-effort structured reading (used for logging, language and foil).

    Returns dict with keys: tokens (canonical), lang, foil (True/False/None),
    pairs [(number, total)], raw."""
    info = {"raw": " ".join(text.split()), "tokens": [], "lang": None, "foil": None, "pairs": []}
    words = _TOKEN.findall(text)
    info["tokens"] = [canon(w) for w in words]

    for match in re.finditer(r"(\d{1,4})\s*/\s*(\d{1,4})", text):
        info["pairs"].append((match.group(1).lstrip("0") or "0", match.group(2).lstrip("0") or "0"))

    # "DOM • EN" / "DOM ★ EN": code, separator, language, on one line. The
    # lookahead lets matches overlap, so junk before the code can't hide it
    for line in text.splitlines():
        for match in re.finditer(r"(?=\b([A-Za-z0-9]{2,5})[ \t]*(\S)[ \t]*([A-Za-z]{2})\b)", line):
            sep, lang = match.group(2), match.group(3).upper()

            if lang in PRINTED_LANGS and (sep in _STAR or sep in _DOT):
                info["lang"] = PRINTED_LANGS[lang]
                info["foil"] = sep in _STAR
                return info

    return info


def _number_forms(number):
    """Ways a collector number can be printed: 7 -> 7, 07, 007, 0007.
    "The List" reprints (Scryfall "CMA-100") print the original "100".
    Promo variants (216p, 109s, 265†e) print the plain number: the stamp on
    the art is their only difference, which OCR can't see."""
    number = (number or "").upper().rsplit("-", 1)[-1]
    number = re.sub(r"[^0-9A-Z]", "", number) or number
    plain = re.sub(r"(?<=\d)[A-Z]+$", "", number)
    digits = number.lstrip("0") or number
    forms = {canon(number), canon(digits)}

    if digits.isdigit():
        forms.update(canon(digits.zfill(width)) for width in (3, 4))

    if plain != number:
        forms |= _number_forms(plain)

    return forms


def score_candidates(text, candidates):
    """Evidence from OCR text for each candidate printing.

    `candidates` are sqlite rows with set_code, collector_number, rarity,
    frame. Returns {card_id: (score, reasons)}. Matching a token exactly
    (after folding confusables) is required: substrings would let "M21" match
    inside random noise."""
    raw_tokens = _TOKEN.findall(text)
    tokens = [canon(t) for t in raw_tokens]
    token_set = set(tokens)

    # The bullet or star between set code and language is often read as
    # another character and glued on: "WHOXEN", "CMA+*+EN", "MM3°*EN"
    for raw in raw_tokens:
        for piece in re.split(r"[^A-Za-z0-9]+", raw):
            if len(piece) >= 2:
                token_set.add(canon(piece))

        glued = re.match(r"^([A-Za-z0-9]{2,5}?)(?:[^A-Za-z0-9]|[xX])*(%s)$" % "|".join(PRINTED_LANGS), raw)
        if glued:
            token_set.add(canon(glued.group(1)))
    # Numbers also appear glued to the total ("123/264") or rarity ("123R").
    # A lone one-character token ("1") is too easily a stray I, l or |, so
    # those only count when written as "1/249"
    number_parts = set()

    for raw, token in zip(raw_tokens, tokens):
        # Only tokens with a real digit can be numbers: folding confusables
        # would otherwise turn words like "IS" into "15"
        if not any(ch.isdigit() for ch in raw):
            continue

        parts = token.split("/")

        if len(parts) > 1:
            number_parts.add(parts[0].lstrip("0") or "0")

        for part in parts:
            # "0045M": number glued to its rarity letter. Only letters: canon()
            # turns S into 5, and stripping digits would turn 125 into 12
            for candidate in (part, re.sub(r"(?<=\d)[CURM]$", "", part)):
                if len(candidate) >= 2:
                    number_parts.add(candidate)

    scores = {}

    for row in candidates:
        score = 0.0
        reasons = []
        code = canon(row["set_code"])
        keys = row.keys() if hasattr(row, "keys") else ()

        # Tokens, promos and special printings print their parent set's code
        # (TC16 -> C16, PBFZ -> BFZ); "The List" prints the original set
        # (collector number "CMA-100" -> CMA)
        codes = {code}
        if len(code) == 4 and code.startswith("P"):
            codes.add(code[1:])
        if "parent_code" in keys and row["parent_code"]:
            codes.add(canon(row["parent_code"]))
        if "-" in (row["collector_number"] or ""):
            listed = row["collector_number"].rsplit("-", 1)[0].upper()
            codes.add(canon(listed))
            # ...and a listed token or promo prints its parent (TBLB -> BLB)
            if len(listed) == 4 and listed[0] in "TP":
                codes.add(canon(listed[1:]))

        if codes & token_set:
            score += 3
            reasons.append("set code")

        if _number_forms(row["collector_number"]) & number_parts:
            score += 3
            reasons.append("number")

            letter = RARITY_LETTERS.get(row["rarity"] or "")
            if letter and (canon(letter) in token_set or any(
                    t.endswith(canon(letter)) and t[:-1] in _number_forms(row["collector_number"]) for t in tokens)):
                score += 1
                reasons.append("rarity letter")

        scores[row["id"]] = (score, reasons)

    return scores
