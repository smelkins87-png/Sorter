"""Tests for localsorter.ocr.

Pure parts (folding, parsing, candidate scoring) run without Tesseract. The
real-OCR tests run Tesseract on synthetic sorter photos and are skipped when
the binary is not installed.
"""
import shutil

import pytest
from PIL import Image

from localsorter import imagehash, ocr
from tests import synthetic

# Applied to the real-OCR tests only
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None,
                                     reason="tesseract binary not installed")


def row(card_id, set_code, number, rarity="common", frame="2015"):
    """A candidate printing as score_candidates() sees it (sqlite row stand-in)."""
    return {"id": card_id, "set_code": set_code, "collector_number": number, "rarity": rarity,
            "frame": frame}


DOM_DECOYS = [row("m21", "M21", "7", "uncommon"), row("woe", "WOE", "311", "mythic"),
              row("m19", "M19", "45")]


# ---------------------------------------------------------------- A. pure functions

@pytest.mark.parametrize("char, folded", [
    ("O", "0"), ("Q", "0"), ("D", "0"),
    ("I", "1"), ("L", "1"), ("|", "1"), ("!", "1"),
    ("S", "5"), ("B", "8"), ("Z", "2"), ("G", "6"), ("T", "7"),
])
def test_canon_folds_each_confusable(char, folded):
    assert ocr.canon(char) == folded
    assert ocr.canon(char.lower()) == folded


def test_canon_folds_words_together():
    assert ocr.canon("DOM") == ocr.canon("D0M") == "00M"
    assert ocr.canon("M21") == ocr.canon("MZI") == "M21"


def test_canon_uppercases_and_keeps_other_characters():
    assert ocr.canon("dom 123/264 ★") == "00M 123/264 ★"


def test_parse_nonfoil_en_with_collector_pair():
    info = ocr.parse("123/264 R\nDOM • EN Jane Doe")
    assert info["lang"] == "en"
    assert info["foil"] is False
    assert info["pairs"] == [("123", "264")]


def test_parse_foil_star_after_zero_padded_number():
    info = ocr.parse("0045 M\nWOE ★ EN")
    assert info["foil"] is True
    assert info["lang"] == "en"
    assert info["pairs"] == []


def test_parse_foil_asterisk():
    info = ocr.parse("WOE * EN")
    assert info["foil"] is True
    assert info["lang"] == "en"


def test_parse_german_with_cent_sign_as_dot():
    info = ocr.parse("M21 ¢ DE Jane")
    assert info["lang"] == "de"
    assert info["foil"] is False


@pytest.mark.parametrize("code, lang", [("JP", "ja"), ("CS", "zhs"), ("SP", "es"), ("CT", "zht")])
def test_parse_maps_printed_language_codes(code, lang):
    assert ocr.parse("DOM • %s" % code)["lang"] == lang


def test_parse_without_code_or_language_gives_none():
    info = ocr.parse("Jane Doe")
    assert info["lang"] is None
    assert info["foil"] is None
    assert info["pairs"] == []


def test_parse_strips_leading_zeros_from_pair():
    assert ocr.parse("007/274")["pairs"] == [("7", "274")]
    assert ocr.parse("000/264")["pairs"] == [("0", "264")]


def test_score_set_number_and_rarity_letter():
    scores = ocr.score_candidates("123/264 R\nDOM • EN",
                                  [row("dom", "DOM", "123", "rare"), row("m19", "M19", "45")])
    assert scores["dom"] == (7.0, ["set code", "number", "rarity letter"])
    assert scores["m19"][0] == 0


def test_score_right_set_wrong_number_gets_set_points_only():
    scores = ocr.score_candidates("123/264 R\nDOM • EN", [row("d", "DOM", "999", "rare")])
    assert scores["d"][0] == 3
    assert scores["d"][1] == ["set code"]


def test_score_promo_set_matches_parent_printed_code():
    scores = ocr.score_candidates("123/264 R\nDOM • EN", [row("p", "PDOM", "123", "rare")])
    assert scores["p"][0] == 7


def test_score_stray_i_and_l_do_not_match_number_7():
    scores = ocr.score_candidates("l I | DOM • EN", [row("m21", "M21", "7", "uncommon")])
    assert scores["m21"][0] == 0
    assert "number" not in scores["m21"][1]


def test_score_number_7_matches_pair_7_slash_274():
    scores = ocr.score_candidates("7/274 U", [row("m21", "M21", "7", "uncommon")])
    assert "number" in scores["m21"][1]
    assert scores["m21"][0] == 3 + 1


def test_score_zero_padded_numbers_match_both_ways():
    padded = ocr.score_candidates("0311 R\nWOE • EN", [row("w", "WOE", "311", "mythic")])
    assert "number" in padded["w"][1]

    plain = ocr.score_candidates("311 M\nWOE • EN", [row("w", "WOE", "0311", "mythic")])
    assert "number" in plain["w"][1]


def test_score_confusable_set_code_still_matches():
    scores = ocr.score_candidates("D0M 123 R", [row("d", "DOM", "123", "rare")])
    assert "set code" in scores["d"][1]
    assert scores["d"][0] == 7


def test_score_glued_rarity_letter_keeps_number():
    scores = ocr.score_candidates("123R\nDOM • EN", [row("d", "DOM", "123", "rare")])
    assert scores["d"][0] == 7


def test_score_number_12_not_granted_by_text_125():
    scores = ocr.score_candidates("125 R\nDOM • EN",
                                  [row("true", "DOM", "125", "rare"), row("dom12", "DOM", "12", "rare")])
    assert "number" not in scores["dom12"][1]
    assert scores["true"][0] > scores["dom12"][0]


@pytest.mark.parametrize("glued, number, rarity", [("125R", "125", "rare"), ("0045M", "0045", "mythic")])
def test_score_number_ending_in_five_glued_to_rarity(glued, number, rarity):
    scores = ocr.score_candidates("%s\nDOM • EN" % glued, [row("d", "DOM", number, rarity)])
    assert scores["d"][0] == 7


def test_parse_foil_and_lang_when_previous_line_is_a_code():
    info = ocr.parse("0311M\nWOE * EN Jane Doe")
    assert info["foil"] is True
    assert info["lang"] == "en"


# ------------------------------------------------- B. real Tesseract on photos

def read_photo(card, seed, sides, upside_down=False):
    """Synthetic sorter photo of `card`, located and read by Tesseract."""
    photo = synthetic.photo(card, seed=seed, card_width=1200, canvas_size=(2150, 1850),
                            upside_down=upside_down)
    located = imagehash.locate(photo)

    if upside_down:
        # locate() cannot tell which end is up; the caller must decide
        located = located.rotated()

    text = ocr.read_bottom(located, ocr.Tesseract(), sides=sides)
    assert text.strip(), "Tesseract read nothing (no card outline, or the call failed)"

    return text


def assert_ranked_first(scores, true_id):
    true_score = scores[true_id][0]
    rivals = {cid: score for cid, (score, _) in scores.items() if cid != true_id}

    assert true_score > max(rivals.values()), "true printing not strictly first: %r" % (scores,)


@needs_tesseract
def test_real_ocr_dom_2015_frame_ranks_true_printing_first():
    card = synthetic.make_printing(3, "DOM", "123", rarity="rare", total="264", frame="2015")
    text = read_photo(card, seed=3, sides=("left",))
    candidates = [row("true", "DOM", "123", "rare")] + DOM_DECOYS

    assert ocr.parse(text)["pairs"] == [("123", "264")], repr(text)
    assert_ranked_first(ocr.score_candidates(text, candidates), "true")


@needs_tesseract
def test_real_ocr_woe_foil_0311_ranks_true_printing_first():
    card = synthetic.make_printing(3, "WOE", "0311", rarity="mythic", foil=True, frame="2015")
    text = read_photo(card, seed=3, sides=("left",))
    candidates = [row("true", "WOE", "0311", "mythic")] + [
        row("dom", "DOM", "123", "rare"), row("m21", "M21", "7", "uncommon"), row("m19", "M19", "45")]

    assert_ranked_first(ocr.score_candidates(text, candidates), "true")


@needs_tesseract
def test_real_ocr_woe_foil_star_is_detected():
    card = synthetic.make_printing(3, "WOE", "0311", rarity="mythic", foil=True, frame="2015")
    text = read_photo(card, seed=3, sides=("left",))

    assert ocr.parse(text)["foil"] is True, repr(text)


@needs_tesseract
def test_real_ocr_m21_german_ranks_true_printing_first_and_reads_lang():
    card = synthetic.make_printing(3, "M21", "7", rarity="uncommon", total="274", lang="DE", frame="2015")
    text = read_photo(card, seed=3, sides=("left",))
    candidates = [row("true", "M21", "7", "uncommon")] + [
        row("dom", "DOM", "123", "rare"), row("woe", "WOE", "311", "mythic"), row("m19", "M19", "45")]

    assert ocr.parse(text)["lang"] == "de", repr(text)
    assert_ranked_first(ocr.score_candidates(text, candidates), "true")


@needs_tesseract
def test_real_ocr_2003_frame_matches_on_number_alone():
    card = synthetic.make_printing(3, "10E", "88", rarity="common", total="383", frame="2003")
    text = read_photo(card, seed=3, sides=("right",))
    candidates = [row("true", "10E", "88", "common", frame="2003"),
                  row("dom", "DOM", "123", "rare", frame="2003"),
                  row("m21", "M21", "7", "uncommon", frame="2003"),
                  row("woe", "WOE", "311", "mythic", frame="2003")]

    assert ocr.score_candidates(text, candidates)["true"][0] == 3, repr(text)
    assert_ranked_first(ocr.score_candidates(text, candidates), "true")


@needs_tesseract
def test_real_ocr_upside_down_photo_after_rotated():
    card = synthetic.make_printing(3, "DOM", "123", rarity="rare", total="264", frame="2015")
    text = read_photo(card, seed=3, sides=("left",), upside_down=True)
    candidates = [row("true", "DOM", "123", "rare")] + DOM_DECOYS

    assert ocr.parse(text)["pairs"] == [("123", "264")], repr(text)
    assert ocr.parse(text)["lang"] == "en", repr(text)
    assert_ranked_first(ocr.score_candidates(text, candidates), "true")


@needs_tesseract
def test_robustness_dom_printing_over_eight_seeds():
    card = synthetic.make_printing(3, "DOM", "123", rarity="rare", total="264", frame="2015")
    candidates = [row("true", "DOM", "123", "rare")] + DOM_DECOYS
    passed, failed = [], []

    for seed in range(1, 9):
        text = read_photo(card, seed=seed, sides=("left",))
        scores = ocr.score_candidates(text, candidates)
        rivals = max(score for cid, (score, _) in scores.items() if cid != "true")
        ok = scores["true"][0] >= 6 and scores["true"][0] > rivals

        (passed if ok else failed).append(seed)

        if not ok:
            print("seed %d: raw %r scores %r" % (seed, text, scores))

    print("\nrobustness: DOM#123 ranked first with score >= 6 in %d/8 seeds (failed seeds: %s)"
          % (len(passed), failed))
    assert len(passed) >= 7, "failed seeds: %s" % failed


def test_missing_tesseract_binary_disables_ocr():
    tesseract = ocr.Tesseract("no-such-binary-xyz")
    assert tesseract.available is False
    assert tesseract.command is None

    located = imagehash.locate(synthetic.photo(synthetic.make_card(1), seed=1))

    assert ocr.read_bottom(located, tesseract) == ""
    assert tesseract.read(Image.new("L", (40, 20), 255)) == ""


def test_words_never_count_as_collector_numbers():
    # "IS" folds to "15" and "SO" to "50"; only tokens with a real digit can be numbers
    scores = ocr.score_candidates("THIS IS SO GOOD", [row("a", "XYZ", "15"), row("b", "XYZ", "50")])
    assert scores["a"][0] == 0 and scores["b"][0] == 0
