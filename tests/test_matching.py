"""Identification tests on a synthetic in-memory card database.

These check the pipeline mechanics (card detection, warping, orientation,
hashing, search and decisions). They are not a measure of real-world accuracy.
"""
import time
from types import SimpleNamespace

import numpy as np
import pytest

from localsorter import carddb, imagehash
from localsorter.matcher import HashIndex, Match
from localsorter.settings import Settings

from tests import synthetic
from tests.helpers import build_db, scryfall_card

S = Settings()
N_CARDS = 150
UPRIGHT = list(range(0, N_CARDS, 7))[:20]
INT32_MAX = np.iinfo(np.int32).max


def tst_id(i, set_code="tst"):
    return "card-%04d-%s" % (i, set_code)


@pytest.fixture(scope="module")
def world():
    conn = carddb.connect(":memory:")
    cards, images = [], {}

    def add(card, img):
        cards.append(card)
        images[card["id"]] = img

    # 150 plain cards, one printing each, set TST
    for i in range(N_CARDS):
        add(scryfall_card(i), synthetic.make_card(i))

    # Reprints A/B: same oracle and same illustration, different frame and text
    add(scryfall_card(910, set_code="aaa", oracle="o-x"), synthetic.make_card(910, art_seed=777))
    add(scryfall_card(911, set_code="bbb", oracle="o-x"), synthetic.make_card(911, art_seed=777))

    # Reprints C/D: byte-identical image in two sets
    add(scryfall_card(920, set_code="aaa", oracle="o-y"), synthetic.make_card(920))
    add(scryfall_card(921, set_code="bbb", oracle="o-y"), synthetic.make_card(920))

    # A single card in its own set, for the allowed_sets edge case
    add(scryfall_card(930, set_code="ccc", oracle="o-z"), synthetic.make_card(930))

    build_db(conn, cards, images)
    index = HashIndex.from_db(conn)

    return SimpleNamespace(conn=conn, index=index, cards=cards)


def _identify(world, data, **kw):
    return world.index.identify(data, S.max_distance, S.min_margin, S.set_margin, **kw)


def _photo(i, **kw):
    return synthetic.photo(synthetic.make_card(i), seed=i, **kw)


# 1. Hashing primitives -------------------------------------------------------

def test_phash_shape_and_dtype():
    img = synthetic.make_card(3)

    bits16 = imagehash.phash(img, 16)
    bits8 = imagehash.phash(img, 8)

    assert bits16.dtype == bool and bits16.shape == (256,)
    assert bits8.dtype == bool and bits8.shape == (64,)
    assert len(imagehash.pack(bits16)) == imagehash.FULL_BYTES


def test_identical_images_hash_to_zero_distance():
    a_full, a_art = imagehash.card_hashes(synthetic.make_card(3))
    b_full, b_art = imagehash.card_hashes(synthetic.make_card(3).copy())

    full_matrix = np.frombuffer(b_full, dtype=np.uint8).reshape(1, -1)
    art_matrix = np.frombuffer(b_art, dtype=np.uint8).reshape(1, -1)

    assert imagehash.hamming(a_full, full_matrix).tolist() == [0]
    assert imagehash.hamming(a_art, art_matrix).tolist() == [0]


def test_different_cards_hash_apart():
    a = imagehash.card_hashes(synthetic.make_card(3))[0]
    b = imagehash.card_hashes(synthetic.make_card(4))[0]

    assert imagehash.hamming(a, np.frombuffer(b, dtype=np.uint8).reshape(1, -1))[0] > 0


def test_hamming_per_row_on_handmade_bytes():
    query = bytes(32)
    matrix = np.array([
        [0x00] * 32,              # identical            -> 0
        [0xFF] * 32,              # every bit differs    -> 256
        [0x01] + [0x00] * 31,     # one bit              -> 1
        [0xF0] + [0x00] * 31,     # four bits            -> 4
        [0x80, 0x01] + [0x00] * 30,  # two bits          -> 2
    ], dtype=np.uint8)

    assert imagehash.hamming(query, matrix).tolist() == [0, 256, 1, 4, 2]
    assert imagehash.hamming(bytes([0xFF] * 32), matrix).tolist() == [256, 0, 255, 252, 254]


def test_hamming_art_sized_rows():
    matrix = np.array([[0xFF] * 8, [0x00] * 8, [0x0F] + [0x00] * 7], dtype=np.uint8)

    assert imagehash.hamming(bytes(8), matrix).tolist() == [64, 0, 4]


# 2. Upright photos -----------------------------------------------------------

def test_upright_photos_identified(world):
    failures = []

    for i in UPRIGHT:
        m = _identify(world, _photo(i))

        if not (m.found and m.card_id == tst_id(i)):
            failures.append((i, m.found, m.card_id, m.distance, m.margin))

    assert not failures, "wrong or missing matches (card, found, got, dist, margin): %s" % failures


# 3. Orientation --------------------------------------------------------------

ORIENT_CARDS = [4, 33, 77, 118, 142]


@pytest.mark.parametrize("i", ORIENT_CARDS)
def test_upside_down_photo_identified(world, i):
    m = _identify(world, _photo(i, upside_down=True))

    assert m.found and m.card_id == tst_id(i), m
    assert m.rotated is True


@pytest.mark.parametrize("i", ORIENT_CARDS)
def test_landscape_photo_identified(world, i):
    m = _identify(world, _photo(i, landscape=True))

    assert m.found and m.card_id == tst_id(i), m


# 4. Unknown cards ------------------------------------------------------------

def test_unknown_cards_are_rejected(world):
    rows = []

    for k in range(10):
        seed = 5000 + k
        m = _identify(world, synthetic.photo(synthetic.make_card(seed), seed=seed))
        rows.append((k, m.found, m.card_id, m.distance, m.margin))
        print("unknown k=%d found=%s nearest=%s dist=%s margin=%s" % (k, m.found, m.card_id, m.distance, m.margin))

    accepted = [r for r in rows if r[1]]
    assert not accepted, "unknown cards accepted (k, found, nearest, dist, margin): %s" % accepted


# 5. Reprints -----------------------------------------------------------------

@pytest.mark.parametrize("i, set_code", [(910, "aaa"), (911, "bbb")])
def test_reprint_with_same_art_different_frame_identifies_printing(world, i, set_code):
    m = _identify(world, synthetic.photo(synthetic.make_card(i, art_seed=777), seed=i))

    print("reprint %s: found=%s card=%s dist=%s margin=%s set_detected=%s"
          % (set_code, m.found, m.card_id, m.distance, m.margin, m.set_detected))
    assert m.found and m.card_id == tst_id(i, set_code), m
    assert m.candidates[0]["set"] == set_code.upper()


def test_identical_reprints_found_but_set_not_detected(world):
    m = _identify(world, _photo(920))

    assert m.found is True
    assert m.card_id in {tst_id(920, "aaa"), tst_id(921, "bbb")}
    assert m.set_detected is False


# 6. allowed_sets -------------------------------------------------------------

def test_allowed_sets_restricts_to_listed_set(world):
    m = _identify(world, _photo(5), allowed_sets=["TST"])

    assert m.found and m.card_id == tst_id(5)


def test_allowed_sets_is_case_insensitive(world):
    m = _identify(world, _photo(5), allowed_sets=["tst"])

    assert m.found and m.card_id == tst_id(5)


def test_allowed_sets_unknown_code_finds_nothing(world):
    m = _identify(world, _photo(5), allowed_sets=["NOPE"])

    assert m.found is False


def test_allowed_sets_single_card_has_no_sentinel_leak(world):
    m = _identify(world, _photo(930), allowed_sets=["CCC"])

    assert m.found and m.card_id == tst_id(930, "ccc")
    assert all(c["distance"] < INT32_MAX for c in m.candidates)
    assert m.margin is None or m.margin < INT32_MAX


# 7. Fallback without OpenCV --------------------------------------------------

def test_identify_without_cv2_does_not_crash(world, monkeypatch):
    monkeypatch.setattr(imagehash, "cv2", None)

    # The centred-crop path must still give a 252x352 portrait
    card = imagehash.find_card(imagehash.load_image(_photo(12)))
    assert card.size == (imagehash.CARD_W, imagehash.CARD_H)

    m = _identify(world, _photo(12))
    assert isinstance(m, Match)
    print("no-cv2 upright photo of card 12: found=%s card=%s dist=%s margin=%s"
          % (m.found, m.card_id, m.distance, m.margin))


# 8. Timing -------------------------------------------------------------------

def test_identify_timing(world):
    photos = [_photo(i) for i in UPRIGHT]

    start = time.perf_counter()
    for data in photos:
        _identify(world, data)
    elapsed_ms = (time.perf_counter() - start) * 1000 / len(photos)

    print("\nidentify: %.1f ms average over %d photos" % (elapsed_ms, len(photos)))
    assert elapsed_ms < 2000
