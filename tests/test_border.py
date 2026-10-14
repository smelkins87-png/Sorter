"""Border colour detection and its use in telling printings apart.

Beta (black border) and Unlimited / Revised (white border) share their art
and have no set symbol: the border is the only visible difference."""
import threading

import pytest

from localsorter import carddb, imagehash, printing
from localsorter.matcher import HashIndex
from localsorter.printing import PrintingResolver
from localsorter.server import App
from localsorter.settings import Settings

from . import synthetic
from .helpers import scryfall_card

BIG = dict(card_width=1200, canvas_size=(2150, 1850))
# A mid-tone surface: neither border colour blends into it
MID = (115, 112, 108)
BACKGROUNDS = [(20, 20, 22), (60, 60, 60), MID, (170, 170, 170), (200, 200, 200), (232, 230, 226)]


def _photo(border, seed, background=MID, frame_colour=None, **kw):
    card = synthetic.make_printing(seed, "X", "1", frame="1993", border=border, frame_colour=frame_colour)
    return synthetic.photo(card, seed=seed, background=background, **BIG, **kw)


@pytest.mark.parametrize("border", ["black", "white"])
def test_black_and_white_borders_are_read(border):
    for seed in range(4):
        for kw in ({}, {"upside_down": True}, {"landscape": True}):
            label, info = printing.detect_border(imagehash.locate(_photo(border, seed, **kw)))
            assert label == border, (seed, kw, info)


@pytest.mark.parametrize("border", ["silver", "gold"])
def test_silver_and_gold_are_never_read_as_another_colour(border):
    for background in BACKGROUNDS:
        for seed in range(2):
            label, info = printing.detect_border(imagehash.locate(_photo(border, seed, background)))
            assert label in (None, border), (background, seed, info)


@pytest.mark.parametrize("border", ["black", "white"])
@pytest.mark.parametrize("background", BACKGROUNDS)
def test_border_is_never_read_as_the_wrong_colour(border, background):
    """The expensive mistake: a Revised Dark Ritual read as black-bordered
    becomes a confident Beta. When a border blends into the background the
    outline lands on the frame inside it; the answer must then be "unsure".
    Dark and light frames both, since the frame is what gets measured then."""
    for frame_colour in (None, (25, 25, 28), (230, 228, 220)):
        for seed in range(2):
            label, info = printing.detect_border(imagehash.locate(_photo(border, seed, background, frame_colour)))
            assert label in (None, border), (frame_colour, seed, info)


def test_no_outline_means_no_reading():
    located = imagehash.Located(b"", synthetic.make_card(1))
    assert printing.detect_border(located) == (None, {})


# --- Choosing the printing -------------------------------------------------

PRINTINGS = [("LEB", "black"), ("2ED", "white"), ("3ED", "white")]


@pytest.fixture(scope="module")
def world():
    conn = carddb.connect(":memory:")
    cards, images = [], {}

    for code, border in PRINTINGS:
        card = scryfall_card(1, set_code=code.lower(), oracle="O1", illustration_id="I1", frame="1993",
                             border_color=border, collector_number="1")
        card["id"] = "print-" + code
        cards.append(card)
        images[card["id"]] = synthetic.make_printing(1, code, "1", frame="1993", border=border)

    for i in range(2, 30):
        card = scryfall_card(i)
        cards.append(card)
        images[card["id"]] = synthetic.make_printing(i, "TST", str(i), total="100")

    carddb.import_cards(conn, cards)
    rows = [(cid, 0, "t") + imagehash.reference_hashes(synthetic.reference_bytes(img)) for cid, img in images.items()]

    with conn:
        conn.executemany("INSERT INTO hashes VALUES (?, ?, ?, ?, ?)", rows)

    return conn, HashIndex.from_db(conn), images


def _resolve(world, code, seed=0, border_check=True, **kw):
    conn, index, images = world
    photo = synthetic.photo(images["print-" + code], seed=seed, background=MID, **BIG, **kw)
    match = index.identify(photo, 80, 15, 4)
    assert match.found

    resolver = PrintingResolver(conn, threading.Lock(), None, "off", border_check=border_check)

    return resolver.resolve(match)


@pytest.mark.parametrize("seed", range(3))
def test_black_border_picks_beta(world, seed):
    decision = _resolve(world, "LEB", seed)

    assert decision.card_id == "print-LEB", decision.evidence
    assert decision.set_detected and decision.method == "border"
    assert decision.border == "black"


@pytest.mark.parametrize("seed", range(3))
def test_white_border_rules_out_beta_but_not_unlimited_vs_revised(world, seed):
    decision = _resolve(world, "2ED", seed, upside_down=bool(seed % 2))

    assert decision.card_id in ("print-2ED", "print-3ED"), decision.evidence
    assert decision.border == "white"
    assert decision.evidence["border_kept"] == ["2ED", "3ED"]
    # Two white-bordered printings remain: honest about not knowing which
    assert not decision.set_detected


def test_border_check_can_be_switched_off(world):
    decision = _resolve(world, "LEB", border_check=False)

    assert not decision.set_detected
    assert "border_kept" not in decision.evidence


def test_server_reports_border(world, tmp_path):
    conn, index, images = world
    app = App(Settings(data_dir=str(tmp_path), export_dir=str(tmp_path / "scans"), ocr="off"), conn=conn, index=index)
    photo = synthetic.photo(images["print-LEB"], seed=1, background=MID, **BIG)

    status, payload = app.identify(photo, {"lang": ["en"], "foil": ["0"]})

    assert status == 200
    card = payload["result"]
    assert (card["set"], card["border"], card["detect_set"]) == ("LEB", "black", 1)
    assert '"border_color": "black"' in card["extra"]
