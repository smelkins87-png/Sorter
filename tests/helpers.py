import json

from localsorter import carddb, imagehash

from . import synthetic


def scryfall_card(i, set_code="tst", name=None, oracle=None, colors=("R",), type_line="Creature — Goblin",
                  usd="1.50", **extra):
    card = {
        "object": "card",
        "id": "card-%04d-%s" % (i, set_code),
        "oracle_id": oracle or "oracle-%04d" % i,
        "name": name or "Card %d" % i,
        "lang": "en",
        "set": set_code,
        "set_name": "Test Set %s" % set_code.upper(),
        "collector_number": str(i),
        "rarity": "common",
        "colors": list(colors),
        "type_line": type_line,
        "cmc": 2.0,
        "released_at": "2024-01-01",
        "layout": "normal",
        "digital": False,
        "tcgplayer_id": 1000 + i,
        "prices": {"usd": usd, "usd_foil": "4.00", "eur": None},
        "legalities": {"commander": "legal", "modern": "not_legal"},
        "edhrec_rank": i,
        "image_uris": {"small": "https://example.invalid/%d-%s.jpg" % (i, set_code)},
    }
    card.update(extra)

    return card


def build_db(conn, cards, images):
    """Imports cards and hashes the given {card_id: PIL image} references."""
    carddb.import_cards(conn, cards)
    carddb.import_sets(conn, [{"code": c["set"], "name": c["set_name"], "released_at": c["released_at"],
                               "set_type": "expansion", "card_count": 1} for c in cards])
    rows = []

    for card_id, img in images.items():
        full, art = imagehash.reference_hashes(synthetic.reference_bytes(img))
        rows.append((card_id, 0, "test", full, art))

    with conn:
        conn.executemany("INSERT INTO hashes VALUES (?, ?, ?, ?, ?)", rows)
