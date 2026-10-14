"""Tests for the Scryfall streaming parser and the card database mapping.

No network access: Scryfall payloads are built locally with tests.helpers.
"""
import io
import json

import pytest

from localsorter import carddb, scryfall

from .helpers import scryfall_card

CHUNK_SIZES = [1, 3, 7, 64, 1 << 20]

ITEMS = [
    {
        "id": "a1",
        "name": "Lim-Dûl's Vault",
        "cmc": 3,
        "price": 1.5e-3,
        "neg": -42,
        "big": 12345678901234567890,
        "flag": True,
        "none": None,
        "nested": {"colors": ["W", "U"], "faces": [{"n": 1}, {"n": 2}]},
        "text": 'Tap: ] , [ } { "quoted"',
    },
    {"id": "b2", "name": "魔法使い", "jp": "ドラゴン", "empty": {}, "emptylist": [], "float": 0.1},
    {"name": "Émer", "emoji": "😀", "nums": [1, 2.5, -3e10]},
]


def _compact(items):
    return json.dumps(items).encode("utf-8")


def _scryfall_lines(items):
    # Scryfall's bulk files: "[" newline, one object per line, ",\n" separators
    return ("[\n" + ",\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n]\n").encode("utf-8")


def _pretty(items):
    return json.dumps(items, indent=2).encode("utf-8")


def _non_ascii(items):
    return json.dumps(items, ensure_ascii=False).encode("utf-8")


def _parse(data, chunk_size):
    return list(scryfall.iter_json_array(io.BytesIO(data), chunk_size=chunk_size))


# ---------------------------------------------------------------------------
# scryfall.iter_json_array
# ---------------------------------------------------------------------------

FORMATS = {
    "compact": _compact,
    "scryfall_lines": _scryfall_lines,
    "pretty": _pretty,
    "non_ascii": _non_ascii,
}


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_yields_same_objects_as_json_loads(fmt, chunk_size):
    data = FORMATS[fmt](ITEMS)

    assert _parse(data, chunk_size) == json.loads(data.decode("utf-8"))


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_numbers_inside_objects_at_chunk_boundaries(chunk_size):
    items = [{"n": 12345, "m": -7, "f": 3.25e2}, {"cmc": 0.0, "big": 9007199254740993, "neg": -1.5e-3}]
    data = _scryfall_lines(items)

    assert _parse(data, chunk_size) == items


@pytest.mark.parametrize("chunk_size", [
    pytest.param(1),
    pytest.param(3),
    pytest.param(7),
    pytest.param(64),
    pytest.param(1 << 20),
])
def test_top_level_numbers_at_chunk_boundaries(chunk_size):
    data = b"[12345, -7, 3.25e2, 0, 1.0, 9007199254740993]"

    assert _parse(data, chunk_size) == [12345, -7, 325.0, 0, 1.0, 9007199254740993]


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_nested_objects_at_chunk_boundaries(chunk_size):
    items = [{"a": {"b": {"c": [1, {"d": "e"}]}}}, {"x": [[1, 2], [3, [4]]]}]
    data = _scryfall_lines(items)

    assert _parse(data, chunk_size) == items


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
@pytest.mark.parametrize("data", [b"[]", b"[ ]\n", b" \n\t[\n]  \n"])
def test_empty_array(data, chunk_size):
    assert _parse(data, chunk_size) == []


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_whitespace_before_array(chunk_size):
    data = b"  \r\n\t [\n {\"id\": 1},\n {\"id\": 2}\n]"

    assert _parse(data, chunk_size) == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7])
def test_non_ascii_text_split_across_chunks(chunk_size):
    items = [{"name": "Lim-Dûl"}, {"name": "魔法使い"}, {"name": "Émer 😀"}]
    data = json.dumps(items, ensure_ascii=False).encode("utf-8")

    assert _parse(data, chunk_size) == items


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
@pytest.mark.parametrize("data", [
    b'[{"a": 1}',
    b'[{"a": 1}, {"b"',
    b'[1, 2',
    b'[{"name": "Lim-D',
    b'[',
])
def test_truncated_input_raises(data, chunk_size):
    with pytest.raises(ValueError):
        _parse(data, chunk_size)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_malformed_element_raises_json_error(chunk_size):
    with pytest.raises(json.JSONDecodeError):
        _parse(b'[{"a": }]', chunk_size)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
@pytest.mark.parametrize("data", [b'{"a": 1}', b'"text"', b"5", b"null", b"true"])
def test_non_array_top_level_raises(data, chunk_size):
    with pytest.raises(ValueError):
        _parse(data, chunk_size)


def test_empty_stream_raises():
    with pytest.raises(ValueError):
        _parse(b"", 64)


# ---------------------------------------------------------------------------
# carddb.card_row
# ---------------------------------------------------------------------------

def test_card_row_normal_card():
    card = scryfall_card(1, set_code="abc", usd="1.50", colors=("R", "G"))
    row = carddb.card_row(card)

    assert row["id"] == card["id"]
    assert row["set_code"] == "ABC"
    assert row["usd"] == 1.5
    assert isinstance(row["usd"], float)
    assert row["usd_foil"] == 4.0
    assert row["eur"] is None
    assert row["eur_foil"] is None
    assert row["usd_etched"] is None
    assert json.loads(row["colors"]) == ["R", "G"]
    assert json.loads(row["image_front"]) == {"small": card["image_uris"]["small"]}
    assert row["image_back"] is None


@pytest.mark.parametrize("prices", [
    {},
    {"usd": None, "usd_foil": None, "eur": None},
    {"usd": "", "usd_foil": ""},
    {"usd": "not-a-number"},
])
def test_card_row_null_or_missing_prices_are_none(prices):
    row = carddb.card_row(scryfall_card(2, prices=prices))

    assert row["usd"] is None
    assert row["usd_foil"] is None


def test_card_row_missing_prices_key():
    card = scryfall_card(3)
    card.pop("prices")

    assert carddb.card_row(card)["usd"] is None


@pytest.mark.parametrize("obj", ["set", "token", "ad_card", None])
def test_card_row_non_card_object_returns_none(obj):
    card = scryfall_card(4)
    if obj is None:
        card.pop("object")
    else:
        card["object"] = obj

    assert carddb.card_row(card) is None


def test_card_row_without_any_image_returns_none():
    card = scryfall_card(5)
    card.pop("image_uris")

    assert carddb.card_row(card) is None


def test_card_row_double_faced_card():
    card = scryfall_card(6, name="Front Face // Back Face", type_line="Creature — Elf // Creature — Wizard",
                         layout="transform")
    card.pop("image_uris")
    card.pop("colors")
    card["card_faces"] = [
        {
            "name": "Front Face",
            "colors": ["G"],
            "type_line": "Creature — Elf",
            "image_uris": {"small": "https://example.invalid/front-s.jpg",
                           "normal": "https://example.invalid/front.jpg"},
        },
        {
            "name": "Back Face",
            "colors": ["U"],
            "type_line": "Creature — Wizard",
            "image_uris": {"small": "https://example.invalid/back-s.jpg",
                           "normal": "https://example.invalid/back.jpg"},
        },
    ]

    row = carddb.card_row(card)

    assert json.loads(row["image_front"]) == {"small": "https://example.invalid/front-s.jpg",
                                              "normal": "https://example.invalid/front.jpg"}
    assert json.loads(row["image_back"]) == {"small": "https://example.invalid/back-s.jpg",
                                             "normal": "https://example.invalid/back.jpg"}
    assert json.loads(row["colors"]) == ["G"]


def test_card_row_double_faced_top_level_colors_win():
    card = scryfall_card(7, colors=("W", "B"), layout="modal_dfc")
    card.pop("image_uris")
    card["card_faces"] = [
        {"colors": ["G"], "image_uris": {"small": "https://example.invalid/f.jpg"}},
        {"colors": ["U"], "image_uris": {"small": "https://example.invalid/b.jpg"}},
    ]

    assert json.loads(carddb.card_row(card)["colors"]) == ["W", "B"]


# ---------------------------------------------------------------------------
# carddb.import_cards / import_sets
# ---------------------------------------------------------------------------

def test_import_cards_upserts_by_id():
    conn = carddb.connect(":memory:")
    card = scryfall_card(10, usd="1.00")

    assert carddb.import_cards(conn, [card]) == 1
    card["prices"]["usd"] = "2.25"
    assert carddb.import_cards(conn, [card]) == 1

    count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    usd = conn.execute("SELECT usd FROM cards WHERE id = ?", (card["id"],)).fetchone()["usd"]

    assert count == 1
    assert usd == 2.25


def test_import_cards_skips_non_cards_and_uppercases_set():
    conn = carddb.connect(":memory:")
    cards = [scryfall_card(11, set_code="tst"), {"object": "set", "id": "x"}, scryfall_card(12, set_code="tst")]

    assert carddb.import_cards(conn, cards) == 2

    codes = {r["set_code"] for r in conn.execute("SELECT set_code FROM cards")}
    assert codes == {"TST"}


def test_import_sets_uppercases_codes():
    conn = carddb.connect(":memory:")
    sets = [{"code": "neo", "name": "Kamigawa", "released_at": "2020-02-07", "set_type": "expansion",
             "card_count": 3, "digital": False}]

    assert carddb.import_sets(conn, sets) == 1

    row = conn.execute("SELECT * FROM sets").fetchone()
    assert row["code"] == "NEO"
    assert row["name"] == "Kamigawa"
    assert row["card_count"] == 3


# ---------------------------------------------------------------------------
# carddb.main_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("type_line, expected", [
    ("Legendary Creature — Elf", "creature"),
    ("Basic Land — Forest", "land"),
    ("Instant // Sorcery", "instant"),
    ("Creature — Goblin // Instant", "creature"),
    ("", ""),
    (None, ""),
    ("Legendary Planeswalker — Jace", "planeswalker"),
    ("Artifact — Equipment", "artifact"),
    ("Plane — Ravnica", "plane"),  # not in MAIN_TYPES: falls back to the last word
])
def test_main_type(type_line, expected):
    assert carddb.main_type(type_line) == expected


def test_main_type_artifact_creature_is_creature():
    # Behaviour as implemented: MAIN_TYPES lists "creature" before "artifact", so the
    # first match wins. Worth a look if artifact creatures should sort as artifacts.
    assert carddb.main_type("Artifact Creature — Golem") == "creature"


# ---------------------------------------------------------------------------
# carddb.price_for / carddb.to_api_card
# ---------------------------------------------------------------------------

def _db_row(prices=None, **extra):
    conn = carddb.connect(":memory:")
    card = scryfall_card(20, prices=prices if prices is not None else {}, **extra)
    carddb.import_cards(conn, [card])

    return conn.execute("SELECT * FROM cards WHERE id = ?", (card["id"],)).fetchone()


@pytest.mark.parametrize("prices, expected", [
    ({"usd_foil": "4.00", "usd": "1.00"}, 4.0),
    ({"usd_etched": "2.50", "usd": "1.00"}, 2.5),
    ({"usd": "1.00"}, 1.0),
    ({"usd_foil": "4.00", "usd_etched": "2.50", "usd": "1.00"}, 4.0),
    ({}, None),
])
def test_price_for_foil(prices, expected):
    assert carddb.price_for(_db_row(prices), foil=1) == expected


@pytest.mark.parametrize("prices, expected", [
    ({"usd": "1.00", "usd_foil": "4.00"}, 1.0),
    ({"usd_foil": "4.00"}, 4.0),
    ({"usd_etched": "2.50"}, 2.5),
    ({}, None),
])
def test_price_for_non_foil(prices, expected):
    assert carddb.price_for(_db_row(prices), foil=0) == expected


@pytest.mark.parametrize("prices, foil, expected", [
    ({"eur_foil": "3.00", "eur": "1.00"}, 1, 3.0),
    ({"eur": "1.00"}, 1, 1.0),
    ({"eur": "1.00", "eur_foil": "3.00"}, 0, 1.0),
    ({"eur_foil": "3.00"}, 0, 3.0),
])
def test_price_for_eur(prices, foil, expected):
    assert carddb.price_for(_db_row(prices), foil=foil, currency="eur") == expected


def test_to_api_card_keys_and_values():
    row = _db_row({"usd": "1.50", "usd_foil": "4.00"}, name="Lim-Dûl the Necromancer",
                  printed_name="Lim-Dûl der Nekromant", colors=("U", "B"),
                  type_line="Legendary Creature — Elf Wizard", cmc=3.0, rarity="rare",
                  collector_number="42", tcgplayer_id=555, set_code="mh3")

    api = carddb.to_api_card(row)

    assert set(api) == {"title", "local_title", "set", "set_name", "num", "rarity", "lang", "foil",
                        "condition", "price", "price_trend", "ext_id", "ecommerce_id", "detect_set",
                        "extra"}
    assert api["title"] == "Lim-Dûl the Necromancer"
    assert api["local_title"] == "Lim-Dûl der Nekromant"
    assert api["set"] == "MH3"
    assert api["num"] == "42"
    assert api["rarity"] == "rare"
    assert api["lang"] == "en"
    assert api["foil"] == 0
    assert api["condition"] == "NM"
    assert api["price"] == 1.5
    assert api["price_trend"] == 1.5
    assert api["ext_id"] == row["id"]
    assert api["ecommerce_id"] == 555
    assert api["detect_set"] == 1


def test_to_api_card_extra_is_json_string():
    row = _db_row({"usd": "1.50"}, colors=("U", "B"), type_line="Legendary Creature — Elf Wizard",
                  cmc=3.0, rarity="rare")

    api = carddb.to_api_card(row)

    assert isinstance(api["extra"], str)
    extra = json.loads(api["extra"])
    assert extra["colors"] == ["blue", "black"]
    assert extra["rarity"] == "rare"
    assert extra["type"] == "creature"
    assert extra["cmc"] == 3
    assert isinstance(extra["cmc"], int)


def test_to_api_card_single_color_and_colorless():
    red = json.loads(carddb.to_api_card(_db_row({"usd": "1"}, colors=("R",)))["extra"])
    colorless = json.loads(carddb.to_api_card(_db_row({"usd": "1"}, colors=()))["extra"])

    assert red["colors"] == ["red"]
    assert colorless["colors"] == []


def test_to_api_card_cmc_none():
    row = _db_row({"usd": "1"}, cmc=None)

    assert json.loads(carddb.to_api_card(row)["extra"])["cmc"] is None


def test_to_api_card_foil_flag_and_price():
    row = _db_row({"usd": "1.50", "usd_foil": "4.00"})

    api = carddb.to_api_card(row, foil=1)

    assert api["foil"] == 1
    assert api["price"] == 4.0
    assert api["price_trend"] == 4.0


def test_to_api_card_eur_currency():
    row = _db_row({"usd": "1.50", "eur": "0.90", "eur_foil": "1.20"})

    api = carddb.to_api_card(row, foil=0, currency="eur")

    assert api["price"] == 0.9


def test_to_api_card_price_zero_when_no_price():
    api = carddb.to_api_card(_db_row({}))

    assert api["price"] == 0.0
    assert api["price_trend"] == 0.0
