"""Printing resolution (stage 2): which printing of a card is this?

Five printings share one card's artwork, so the perceptual hash cannot tell
them apart. These tests drive printing.PrintingResolver with the evidence
switched on and off in turn: set-symbol and bottom-strip close-ups
("regions"), Tesseract reading the collector info ("ocr"), both, or neither.
Synthetic cards only: they check the mechanics, not real-world accuracy.
"""
import csv
import shutil
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from localsorter import carddb, imagehash, ocr, printing
from localsorter.matcher import HashIndex
from localsorter.printing import PrintingResolver
from localsorter.server import App
from localsorter.settings import Settings

from tests import synthetic
from tests.helpers import scryfall_card

# Tests that read the collector line need the tesseract program (the Pi gets
# it from install.sh; a bare test machine may not have it)
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None,
                                     reason="tesseract binary not installed")

S = Settings()
PHOTO = dict(card_width=1200, canvas_size=(2150, 1850))

# (set code, collector number, rarity, total, frame) of the five printings
PRINTINGS = [
    ("DOM", "123", "rare", "264", "2015"),
    ("M19", "45", "uncommon", "280", "2015"),
    ("A25", "7", "common", "249", "2015"),
    ("WOE", "0311", "mythic", None, "2015"),
    ("10E", "88", "common", "383", "2003"),
]
CODES = [p[0] for p in PRINTINGS]
FRAME_2015 = [p[0] for p in PRINTINGS if p[4] == "2015"]

# orientation -> (seed offset, synthetic.photo keyword arguments)
ORIENTATIONS = {
    "upright": (0, {}),
    "upside_down": (10, {"upside_down": True}),
    "landscape": (20, {"landscape": True}),
}
CASES = [(code, orientation) for code in CODES for orientation in ORIENTATIONS]
CASE_IDS = ["%s-%s" % case for case in CASES]
CASES_2015 = [case for case in CASES if case[0] in FRAME_2015]
CASES_2015_IDS = ["%s-%s" % case for case in CASES_2015]


def _card_id(code):
    return "print-" + code


# --- shared database: five printings + 40 decoys ---------------------------

@pytest.fixture(scope="module")
def world():
    conn = carddb.connect(":memory:")
    cards, images = [], {}

    for code, num, rarity, total, frame in PRINTINGS:
        card = scryfall_card(1, set_code=code.lower(), oracle="O1", name="Shared Art", rarity=rarity,
                             collector_number=num, frame=frame, illustration_id="I1",
                             finishes=["nonfoil", "foil"])
        card["id"] = _card_id(code)
        cards.append(card)
        images[card["id"]] = synthetic.make_printing(1, code, num, rarity, total=total, frame=frame)

    for i in range(2, 42):  # 40 unrelated cards
        card = scryfall_card(i)
        cards.append(card)
        images[card["id"]] = synthetic.make_printing(i, "TST", str(i), total="100")

    carddb.import_cards(conn, cards)

    with conn:
        conn.executemany(
            "INSERT INTO hashes VALUES (?, ?, ?, ?, ?)",
            [(cid, 0, "test") + imagehash.reference_hashes(synthetic.reference_bytes(img))
             for cid, img in images.items()])
        conn.executemany(
            "INSERT INTO regions VALUES (?, ?, ?, ?, ?)",
            [(_card_id(code), 0, "test") + printing.reference_regions(synthetic.reference(images[_card_id(code)]))
             for code in CODES])

    # Same database with no close-ups at all
    no_regions = carddb.connect(":memory:")
    conn.backup(no_regions)

    with no_regions:
        no_regions.execute("DELETE FROM regions")

    index = HashIndex.from_db(conn)
    tesseract = ocr.Tesseract()
    lock = threading.Lock()

    photos = {}

    for code, orientation in CASES:
        offset, kwargs = ORIENTATIONS[orientation]
        seed = CODES.index(code) + offset
        data = synthetic.photo(images[_card_id(code)], seed=seed, **PHOTO, **kwargs)
        match = index.identify(data, S.max_distance, S.min_margin, S.set_margin)
        photos[(code, orientation)] = SimpleNamespace(data=data, match=match)

    resolvers = {
        "regions": PrintingResolver(conn, lock, None, "off"),
        "ocr": PrintingResolver(no_regions, lock, tesseract, "auto"),
        "combined": PrintingResolver(conn, lock, tesseract, "auto"),
        "none": PrintingResolver(no_regions, lock, None, "off"),
        # OCR switched on, but the tesseract program is missing
        "ocr_missing": PrintingResolver(no_regions, lock, ocr.Tesseract("no-such-tesseract"), "auto"),
    }

    return SimpleNamespace(conn=conn, index=index, images=images, photos=photos, resolvers=resolvers,
                           decisions={})


def _decide(world, mode, code, orientation):
    """Resolver decision for one photo, computed once per mode."""
    key = (mode, code, orientation)

    if key not in world.decisions:
        world.decisions[key] = world.resolvers[mode].resolve(world.photos[(code, orientation)].match)

    return world.decisions[key]


# --- 1. regions only -------------------------------------------------------

@pytest.mark.parametrize("code,orientation", CASES, ids=CASE_IDS)
def test_regions_only_picks_printing(world, code, orientation):
    assert world.photos[(code, orientation)].match.found

    decision = _decide(world, "regions", code, orientation)

    assert decision.card_id == _card_id(code), decision.evidence
    assert decision.set_detected, decision.evidence


# --- 2. OCR only (no close-ups in the database) ----------------------------

@needs_tesseract
@pytest.mark.parametrize("code,orientation", CASES_2015, ids=CASES_2015_IDS)
def test_ocr_only_2015_frame_picks_printing(world, code, orientation):
    decision = _decide(world, "ocr", code, orientation)

    assert decision.card_id == _card_id(code), decision.evidence
    assert decision.set_detected, decision.evidence
    assert decision.method == "ocr", decision.evidence


@pytest.mark.parametrize("code,orientation", CASES_2015[:2], ids=CASES_2015_IDS[:2])
def test_missing_tesseract_never_guesses_the_printing(world, code, orientation):
    decision = _decide(world, "ocr_missing", code, orientation)

    # Same card, the printing honestly unconfirmed: flagged for the review page
    oracle = lambda card_id: world.conn.execute("SELECT oracle_id FROM cards WHERE id = ?",
                                                (card_id,)).fetchone()[0]
    assert oracle(decision.card_id) == oracle(_card_id(code))
    assert not decision.set_detected, decision.evidence


@pytest.mark.parametrize("orientation", list(ORIENTATIONS))
def test_ocr_only_2003_frame_never_confidently_wrong(world, orientation):
    decision = _decide(world, "ocr", "10E", orientation)
    correct = decision.card_id == _card_id("10E")

    print("\nOCR-only 10E-%s: card_id correct=%s set_detected=%s method=%s ocr=%r"
          % (orientation, correct, decision.set_detected, decision.method,
             decision.evidence.get("ocr_text")))

    assert correct or not decision.set_detected, decision.evidence


# --- 3. combined -----------------------------------------------------------

@pytest.mark.parametrize("code,orientation", CASES, ids=CASE_IDS)
def test_combined_picks_printing_confidently(world, code, orientation):
    decision = _decide(world, "combined", code, orientation)

    assert decision.card_id == _card_id(code), decision.evidence
    assert decision.set_detected, decision.evidence


# --- 4. no evidence at all -------------------------------------------------

@pytest.mark.parametrize("code,orientation", CASES, ids=CASE_IDS)
def test_no_evidence_is_flagged_as_guess(world, code, orientation):
    decision = _decide(world, "none", code, orientation)

    assert decision.card_id in {_card_id(c) for c in CODES}
    assert not decision.set_detected, decision.evidence


# --- 5. never confidently wrong --------------------------------------------

def test_never_confidently_wrong(world):
    wrong, confident = [], 0

    for mode in ("regions", "ocr", "combined"):
        for code, orientation in CASES:
            decision = _decide(world, mode, code, orientation)

            if not decision.set_detected:
                continue

            if decision.card_id == _card_id(code):
                confident += 1
            else:
                wrong.append((mode, code, orientation, decision.card_id, decision.method))

    print("\nconfident and correct: %d of %d; confident and wrong: %s"
          % (confident, 3 * len(CASES), wrong or "none"))

    assert not wrong


# --- 6. allowed_sets -------------------------------------------------------

def test_allowed_sets_restricts_pool_to_listed_set(world):
    # The hash match is the DOM printing; the caller only allows M19
    match = world.photos[("DOM", "upright")].match
    decision = world.resolvers["combined"].resolve(match, allowed_sets=["M19"])

    assert decision.card_id == "print-M19", decision.evidence
    # The photo is really the DOM printing: the swap must not be reported as certain
    assert decision.set_detected is False, decision.evidence


def test_allowed_sets_applied_to_identify_returns_listed_set(world):
    data = world.photos[("DOM", "upright")].data
    match = world.index.identify(data, S.max_distance, S.min_margin, S.set_margin, allowed_sets=["M19"])
    decision = world.resolvers["combined"].resolve(match, allowed_sets=["M19"])

    assert decision.card_id == "print-M19", decision.evidence


# --- 7-9. foil, language, unique printing ----------------------------------

# set, oracle id, finishes, printed language
EXTRA = [
    ("FOI", "F1", ["foil"], "EN"),
    ("NFO", "F2", ["nonfoil"], "EN"),
    ("BTH", "F3", ["nonfoil", "foil"], "EN"),
    ("GER", "F4", ["nonfoil", "foil"], "DE"),
    ("UNQ", "F5", ["nonfoil"], "EN"),
]


def _extra_seed(code):
    return 40 + [e[0] for e in EXTRA].index(code)


def _extra_number(code):
    return str(_extra_seed(code) - 39)


def _extra_print(code, foil=False):
    lang = next(e[3] for e in EXTRA if e[0] == code)

    return synthetic.make_printing(_extra_seed(code), code, _extra_number(code), "rare", total="100",
                                   frame="2015", lang=lang, foil=foil)


def _extra_photo(code, foil=False):
    return synthetic.photo(_extra_print(code, foil=foil), seed=_extra_seed(code), **PHOTO)


@pytest.fixture(scope="module")
def extras():
    conn = carddb.connect(":memory:")
    cards, images = [], {}

    for code, oracle, finishes, _lang in EXTRA:
        card = scryfall_card(_extra_seed(code), set_code=code.lower(), oracle=oracle, name="Extra " + code,
                             rarity="rare", collector_number=_extra_number(code), frame="2015",
                             illustration_id=oracle, finishes=finishes,
                             lang=next(e[3] for e in EXTRA if e[0] == code).lower())
        card["id"] = _card_id(code)
        cards.append(card)
        images[card["id"]] = _extra_print(code)

    for i in range(100, 110):
        card = scryfall_card(i)
        cards.append(card)
        images[card["id"]] = synthetic.make_printing(i, "TST", str(i), total="100")

    carddb.import_cards(conn, cards)

    with conn:
        conn.executemany(
            "INSERT INTO hashes VALUES (?, ?, ?, ?, ?)",
            [(cid, 0, "test") + imagehash.reference_hashes(synthetic.reference_bytes(img))
             for cid, img in images.items()])

    return SimpleNamespace(index=HashIndex.from_db(conn),
                           resolver=PrintingResolver(conn, threading.Lock(), ocr.Tesseract(), "auto"))


def _extra_match(extras, code, foil=False):
    return extras.index.identify(_extra_photo(code, foil=foil), S.max_distance, S.min_margin, S.set_margin)


@pytest.mark.parametrize("foil_mode", [0, 1, 2])
def test_foil_only_printing_is_foil(extras, foil_mode):
    match = _extra_match(extras, "FOI", foil=True)
    decision = extras.resolver.resolve(match, foil_mode=foil_mode)

    assert decision.card_id == _card_id("FOI")
    assert decision.foil == 1


@pytest.mark.parametrize("foil_mode", [0, 1, 2])
def test_nonfoil_only_printing_is_not_foil(extras, foil_mode):
    match = _extra_match(extras, "NFO")
    decision = extras.resolver.resolve(match, foil_mode=foil_mode)

    assert decision.card_id == _card_id("NFO")
    assert decision.foil == 0


@pytest.mark.parametrize("foil_mode,expected", [(0, 0), (1, 1)])
def test_both_finishes_follow_foil_mode(extras, foil_mode, expected):
    match = _extra_match(extras, "BTH")
    decision = extras.resolver.resolve(match, foil_mode=foil_mode)

    assert decision.card_id == _card_id("BTH")
    assert decision.foil == expected


@needs_tesseract
def test_both_finishes_detect_foil_from_collector_info(extras):
    foil = extras.resolver.resolve(_extra_match(extras, "BTH", foil=True), foil_mode=2)
    nonfoil = extras.resolver.resolve(_extra_match(extras, "BTH", foil=False), foil_mode=2)

    assert foil.card_id == nonfoil.card_id == _card_id("BTH")
    assert foil.foil == 1, foil.evidence
    assert nonfoil.foil == 0, nonfoil.evidence


@needs_tesseract
def test_printed_language_read_from_collector_info(extras):
    decision = extras.resolver.resolve(_extra_match(extras, "GER"), lang_hint="auto")

    assert decision.card_id == _card_id("GER"), decision.evidence
    assert decision.lang == "de", decision.evidence


def test_unique_printing_skips_ocr(extras):
    decision = extras.resolver.resolve(_extra_match(extras, "UNQ"))

    assert decision.card_id == _card_id("UNQ")
    assert decision.set_detected
    assert decision.method == "hash"
    assert "ocr_text" not in decision.evidence


# --- 10. server integration ------------------------------------------------

def test_server_identify_end_to_end(world, tmp_path):
    settings = Settings(data_dir=str(tmp_path), export_dir=str(tmp_path / "scans"))
    app = App(settings, conn=world.conn)

    status, payload = app.identify(world.photos[("DOM", "upright")].data, {"lang": ["en"], "foil": ["0"]})

    assert status == 200, payload
    card = payload["result"]
    assert card["set"] == "DOM"
    assert card["num"] == "123"
    assert card["detect_set"] == 1
    assert card["confidence"]["printing"]["method"]

    path = tmp_path / "scans" / datetime.now().strftime("%Y-%m-%d") / "scans.csv"

    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert "printing_method" in rows[0] and "ocr_text" in rows[0]
    row = next(r for r in rows if r["uuid"] == card["uuid"])
    assert row["printing_method"] == card["confidence"]["printing"]["method"]
    assert row["set"] == "DOM" and row["collector_number"] == "123"


# --- 11. timing ------------------------------------------------------------

def test_timing_combined(world):
    times = []

    for code, orientation in CASES:
        data = world.photos[(code, orientation)].data
        start = time.perf_counter()
        match = world.index.identify(data, S.max_distance, S.min_margin, S.set_margin)
        world.resolvers["combined"].resolve(match)
        times.append((time.perf_counter() - start) * 1000)

    print("\ncombined identify+resolve over %d photos: avg %.0f ms, max %.0f ms"
          % (len(times), sum(times) / len(times), max(times)))
