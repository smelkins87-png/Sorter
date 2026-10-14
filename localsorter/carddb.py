"""SQLite card database built from Scryfall bulk data."""
import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id               TEXT PRIMARY KEY,   -- Scryfall card id
    oracle_id        TEXT,
    name             TEXT NOT NULL,
    printed_name     TEXT,
    lang             TEXT,
    set_code         TEXT NOT NULL,      -- upper case, as the sorter expects
    set_name         TEXT,
    collector_number TEXT,
    rarity           TEXT,
    colors           TEXT,               -- JSON list of Scryfall letters
    type_line        TEXT,
    cmc              REAL,
    released_at      TEXT,
    layout           TEXT,
    illustration_id  TEXT,
    digital          INTEGER DEFAULT 0,
    tcgplayer_id     INTEGER,
    usd              REAL,
    usd_foil         REAL,
    usd_etched       REAL,
    eur              REAL,
    eur_foil         REAL,
    edhrec_rank      INTEGER,
    legalities       TEXT,               -- JSON object
    image_front      TEXT,               -- JSON {size: url}
    image_back       TEXT,               -- JSON {size: url}, double faced only
    frame            TEXT,               -- 1993, 1997, 2003, 2015, future
    finishes         TEXT,               -- JSON list: nonfoil, foil, etched
    border_color     TEXT,
    promo            INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cards_set ON cards (set_code);
CREATE INDEX IF NOT EXISTS cards_oracle ON cards (oracle_id);
CREATE INDEX IF NOT EXISTS cards_set_number ON cards (set_code, collector_number);

CREATE TABLE IF NOT EXISTS sets (
    code        TEXT PRIMARY KEY,        -- upper case
    name        TEXT,
    released_at TEXT,
    set_type    TEXT,
    card_count  INTEGER,
    digital     INTEGER DEFAULT 0
);

-- One row per card face image. Hashes are packed bit arrays (see imagehash)
CREATE TABLE IF NOT EXISTS hashes (
    card_id    TEXT NOT NULL,
    face       INTEGER NOT NULL,
    source_url TEXT,
    full_hash  BLOB NOT NULL,
    art_hash   BLOB NOT NULL,
    PRIMARY KEY (card_id, face)
);

-- Close-ups used to tell apart printings that share the same artwork:
-- set symbol (colour PNG) and bottom info strip (greyscale PNG), cut from
-- Scryfall's image in REGION_CARD_W-wide card space (see printing.py)
CREATE TABLE IF NOT EXISTS regions (
    card_id    TEXT NOT NULL,
    face       INTEGER NOT NULL,
    source_url TEXT,
    symbol     BLOB NOT NULL,
    bottom     BLOB NOT NULL,
    PRIMARY KEY (card_id, face)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

COLOR_WORDS = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}
MAIN_TYPES = ["land", "creature", "planeswalker", "battle", "instant", "sorcery",
              "artifact", "enchantment", "kindred", "tribal"]


def connect(path):
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets the nightly price sync write while the server keeps reading
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    conn.executescript(SCHEMA)

    return conn


# Columns added after the first release: (name, type)
_ADDED_COLUMNS = [("frame", "TEXT"), ("finishes", "TEXT"), ("border_color", "TEXT"),
                  ("promo", "INTEGER DEFAULT 0")]


def _migrate(conn):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}

    if not existing:
        return

    for name, kind in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute("ALTER TABLE cards ADD COLUMN %s %s" % (name, kind))


def _price(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _images(uris):
    if not uris:
        return None

    return json.dumps({k: uris[k] for k in ("small", "normal", "large") if k in uris})


def card_row(card):
    """Maps a Scryfall card object to a `cards` row, or None to skip it."""
    if card.get("object") != "card":
        return None

    faces = card.get("card_faces") or []
    front = faces[0] if faces else {}

    image_front = _images(card.get("image_uris") or front.get("image_uris"))
    image_back = None

    # Transform/MDFC cards keep a separate image per face
    if len(faces) > 1 and not card.get("image_uris"):
        image_back = _images(faces[1].get("image_uris"))

    if not image_front:
        return None

    colors = card.get("colors")
    if colors is None:
        colors = front.get("colors", [])

    prices = card.get("prices") or {}

    return {
        "id": card["id"],
        "oracle_id": card.get("oracle_id") or front.get("oracle_id"),
        "name": card["name"],
        "printed_name": card.get("printed_name") or front.get("printed_name"),
        "lang": card.get("lang", "en"),
        "set_code": card["set"].upper(),
        "set_name": card.get("set_name"),
        "collector_number": card.get("collector_number"),
        "rarity": card.get("rarity"),
        "colors": json.dumps(colors),
        "type_line": card.get("type_line") or front.get("type_line") or "",
        "cmc": card.get("cmc"),
        "released_at": card.get("released_at"),
        "layout": card.get("layout"),
        "illustration_id": card.get("illustration_id") or front.get("illustration_id"),
        "digital": 1 if card.get("digital") else 0,
        "tcgplayer_id": card.get("tcgplayer_id"),
        "usd": _price(prices.get("usd")),
        "usd_foil": _price(prices.get("usd_foil")),
        "usd_etched": _price(prices.get("usd_etched")),
        "eur": _price(prices.get("eur")),
        "eur_foil": _price(prices.get("eur_foil")),
        "edhrec_rank": card.get("edhrec_rank"),
        "legalities": json.dumps(card.get("legalities") or {}),
        "image_front": image_front,
        "image_back": image_back,
        "frame": card.get("frame"),
        "finishes": json.dumps(card.get("finishes") or []),
        "border_color": card.get("border_color"),
        "promo": 1 if card.get("promo") else 0,
    }


CARD_COLUMNS = ["id", "oracle_id", "name", "printed_name", "lang", "set_code", "set_name",
                "collector_number", "rarity", "colors", "type_line", "cmc", "released_at", "layout",
                "illustration_id", "digital", "tcgplayer_id", "usd", "usd_foil", "usd_etched", "eur",
                "eur_foil", "edhrec_rank", "legalities", "image_front", "image_back", "frame", "finishes",
                "border_color", "promo"]

_UPSERT = "INSERT INTO cards (%s) VALUES (%s) ON CONFLICT(id) DO UPDATE SET %s" % (
    ", ".join(CARD_COLUMNS),
    ", ".join(":" + c for c in CARD_COLUMNS),
    ", ".join("%s=excluded.%s" % (c, c) for c in CARD_COLUMNS if c != "id"),
)


def import_cards(conn, cards, batch_size=2000, progress=None):
    """Upserts an iterable of Scryfall card objects. Returns the count stored."""
    batch = []
    total = 0

    for card in cards:
        row = card_row(card)

        if row is None:
            continue

        batch.append(row)

        if len(batch) >= batch_size:
            with conn:
                conn.executemany(_UPSERT, batch)
            total += len(batch)
            batch = []

            if progress:
                progress(total)

    if batch:
        with conn:
            conn.executemany(_UPSERT, batch)
        total += len(batch)

    return total


def import_sets(conn, sets):
    rows = [{
        "code": s["code"].upper(),
        "name": s.get("name"),
        "released_at": s.get("released_at"),
        "set_type": s.get("set_type"),
        "card_count": s.get("card_count"),
        "digital": 1 if s.get("digital") else 0,
    } for s in sets]

    with conn:
        conn.executemany(
            "INSERT INTO sets (code, name, released_at, set_type, card_count, digital) "
            "VALUES (:code, :name, :released_at, :set_type, :card_count, :digital) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, released_at=excluded.released_at, "
            "set_type=excluded.set_type, card_count=excluded.card_count, digital=excluded.digital",
            rows)

    return len(rows)


def set_meta(conn, key, value):
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()

    return row["value"] if row else default


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main_type(type_line):
    """Lower-case primary type, e.g. 'land' or 'creature' (front face only)."""
    front = (type_line or "").split("//")[0].lower()
    types = front.split("—")[0]

    for name in MAIN_TYPES:
        if name in types.split():
            return name

    return types.strip().split()[-1] if types.strip() else ""


def price_for(row, foil, currency="usd"):
    if currency == "eur":
        order = ("eur_foil", "eur") if foil else ("eur", "eur_foil")
        return next((row[c] for c in order if row[c] is not None), None)

    if foil:
        for column in ("usd_foil", "usd_etched", "usd"):
            if row[column] is not None:
                return row[column]
        return None

    for column in ("usd", "usd_foil", "usd_etched"):
        if row[column] is not None:
            return row[column]

    return None


def to_api_card(row, foil=0, condition="NM", lang="en", currency="usd"):
    """Builds the card dict the Magic Sorter firmware expects from its API.

    Field names follow lib/matcher/card_attributes.py, lib/organizer/camera.py
    and lib/plate/plate.py in the vendor code: `extra` is a JSON *string*
    holding colors (as words), rarity, type and cmc.
    """
    colors = [COLOR_WORDS[c] for c in json.loads(row["colors"] or "[]") if c in COLOR_WORDS]
    price = price_for(row, foil, currency)
    cmc = row["cmc"]

    extra = {
        "colors": colors,
        "rarity": row["rarity"] or "",
        "type": main_type(row["type_line"]),
        "cmc": int(cmc) if cmc is not None else None,
        "type_line": row["type_line"],
        "border_color": row["border_color"],
    }

    return {
        "title": row["name"],
        "local_title": row["printed_name"] or "",
        "set": row["set_code"],
        "set_name": row["set_name"] or "",
        "num": row["collector_number"] or "",
        "rarity": row["rarity"] or "",
        "lang": row["lang"] or lang,
        "foil": int(foil or 0),
        "condition": condition,
        "price": round(price, 2) if price is not None else 0.0,
        "price_trend": round(price, 2) if price is not None else 0.0,
        "ext_id": row["id"],
        "ecommerce_id": row["tcgplayer_id"] or 0,
        "detect_set": 1,
        "extra": json.dumps(extra),
    }


def lookalikes(conn, card_id):
    """Every paper printing that shares the card's artwork (same oracle card
    and illustration), the card itself included. These can only be told apart
    by set symbol and collector info."""
    row = conn.execute("SELECT oracle_id, illustration_id FROM cards WHERE id = ?", (card_id,)).fetchone()

    if row is None or row["oracle_id"] is None:
        return [card_id]

    if row["illustration_id"]:
        rows = conn.execute("SELECT id FROM cards WHERE oracle_id = ? AND digital = 0 AND "
                            "(illustration_id = ? OR illustration_id IS NULL)",
                            (row["oracle_id"], row["illustration_id"])).fetchall()
    else:
        rows = conn.execute("SELECT id FROM cards WHERE oracle_id = ? AND digital = 0",
                            (row["oracle_id"],)).fetchall()

    ids = [r["id"] for r in rows]

    return ids if card_id in ids else ids + [card_id]


def finishes_of(row):
    try:
        return set(json.loads(row["finishes"] or "[]"))
    except (TypeError, ValueError, IndexError, KeyError):
        return set()
