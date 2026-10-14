"""Card-data packages: made from a synced database (pack), loaded on the Pi
(seed) so it skips the first download."""
import json
import os

import pytest

from localsorter import carddb, printing, seed
from localsorter.__main__ import main as cli
from localsorter.stopcard import StopCards

from . import synthetic
from .helpers import build_db, scryfall_card


@pytest.fixture
def synced(tmp_path):
    """A database as the sync leaves it, plus things private to one sorter."""
    path = str(tmp_path / "built" / "cards.sqlite")
    conn = carddb.connect(path)
    cards = [scryfall_card(i) for i in range(1, 7)]
    build_db(conn, cards, {c["id"]: synthetic.make_card(int(c["collector_number"])) for c in cards})

    with conn:
        conn.executemany("INSERT INTO regions VALUES (?, 0, 'url', ?, ?)",
                         [(c["id"], os.urandom(3000), os.urandom(4000)) for c in cards])
        conn.execute("INSERT INTO kv VALUES ('device_config', '{}')")
    carddb.set_meta(conn, "regions_version", printing.REGIONS_VERSION)
    carddb.set_meta(conn, "bulk_updated_at", "2026-10-14T09:05:48+00:00")
    StopCards(conn, __import__("threading").Lock())
    with conn:
        conn.execute("INSERT INTO stop_cards VALUES ('u', '2026-10-14', x'00', 'now')")
    conn.close()

    out = str(tmp_path / "card-data")
    os.makedirs(out)
    seed.pack(path, out, part_size=1000)           # small parts: several of each

    return tmp_path, out


def _counts(path):
    conn = carddb.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] if t in tables else 0
                for t in ("cards", "sets", "hashes", "regions", "kv", "stop_cards")}
    finally:
        conn.close()


def test_packages_are_split_and_described(synced):
    tmp_path, out = synced

    for kind in ("core", "closeups"):
        manifest = json.load(open(os.path.join(out, kind, "manifest.json")))
        assert manifest["kind"] == kind and len(manifest["parts"]) > 1
        assert all(p["size"] <= 1000 for p in manifest["parts"])
        assert manifest["regions_version"] == printing.REGIONS_VERSION

    core = json.load(open(os.path.join(out, "core", "manifest.json")))
    assert core["compression"] == "gzip" and core["rows"]["hashes"] == 6


def test_a_new_sorter_gets_everything_but_private_data(synced):
    tmp_path, out = synced
    target = str(tmp_path / "pi" / "cards.sqlite")

    assert seed.seed(target, [out]) == {"core": 6, "closeups": 6}

    # Reviews, settings backups and stop cards belong to the sorter that made them
    assert _counts(target) == {"cards": 6, "sets": 1, "hashes": 6, "regions": 6, "kv": 0, "stop_cards": 0}

    conn = carddb.connect(target)
    assert carddb.get_meta(conn, "regions_version") == printing.REGIONS_VERSION
    built = carddb.connect(str(tmp_path / "built" / "cards.sqlite"))
    assert (conn.execute("SELECT symbol FROM regions WHERE card_id = 'card-0003-tst'").fetchone()[0] ==
            built.execute("SELECT symbol FROM regions WHERE card_id = 'card-0003-tst'").fetchone()[0])
    # No unpacked leftovers next to the database
    assert sorted(os.listdir(tmp_path / "pi")) == sorted(n for n in os.listdir(tmp_path / "pi") if "seed-" not in n)


def test_found_in_the_usual_kinds_of_places(synced, tmp_path):
    tmp_path, out = synced
    usb = tmp_path / "media" / "pi" / "STICK"
    usb.mkdir(parents=True)
    os.rename(out, usb / "card-data")

    found = seed.find([str(tmp_path / "media" / "*" / "*" / "card-data")])
    assert set(found) == {"core", "closeups"}


def test_a_damaged_part_is_refused(synced):
    tmp_path, out = synced
    part = os.path.join(out, "core", json.load(open(os.path.join(out, "core", "manifest.json")))["parts"][1]["name"])
    with open(part, "r+b") as handle:
        handle.write(b"x")

    with pytest.raises(ValueError, match="damaged"):
        seed.seed(str(tmp_path / "pi" / "cards.sqlite"), [out])


def test_a_missing_part_is_named(synced):
    tmp_path, out = synced
    name = json.load(open(os.path.join(out, "closeups", "manifest.json")))["parts"][-1]["name"]
    os.remove(os.path.join(out, "closeups", name))

    with pytest.raises(ValueError, match="missing"):
        seed.seed(str(tmp_path / "pi" / "cards.sqlite"), [out])


def test_a_synced_sorter_keeps_its_own_data_and_gains_closeups(synced):
    tmp_path, out = synced
    target = str(tmp_path / "pi" / "cards.sqlite")
    conn = carddb.connect(target)
    cards = [scryfall_card(1)]
    build_db(conn, cards, {cards[0]["id"]: synthetic.make_card(1)})
    with conn:
        conn.execute("INSERT INTO regions VALUES ('card-0001-tst', 0, 'mine', x'01', x'02')")
    conn.close()

    assert seed.seed(target, [out]) == {"core": 0, "closeups": 5}

    conn = carddb.connect(target)
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1          # its own data
    assert conn.execute("SELECT source_url FROM regions WHERE card_id = 'card-0001-tst'").fetchone()[0] == "mine"
    assert conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0] == 6


def test_closeups_from_another_version_are_skipped(synced):
    tmp_path, out = synced
    path = os.path.join(out, "closeups", "manifest.json")
    manifest = json.load(open(path))
    manifest["regions_version"] = "1"
    json.dump(manifest, open(path, "w"))

    assert seed.seed(str(tmp_path / "pi" / "cards.sqlite"), [out]) == {"core": 6, "closeups": 0}


def test_nothing_found_is_fine(tmp_path):
    assert seed.seed(str(tmp_path / "pi" / "cards.sqlite"), [str(tmp_path / "nowhere")]) == {"core": 0, "closeups": 0}


def test_command_line(synced):
    tmp_path, out = synced
    data = str(tmp_path / "cli")

    assert cli(["--data-dir", data, "seed", out]) == 0
    assert _counts(os.path.join(data, "cards.sqlite"))["regions"] == 6

    os.remove(os.path.join(out, "core", json.load(open(os.path.join(out, "core", "manifest.json")))["parts"][0]["name"]))
    assert cli(["--data-dir", str(tmp_path / "cli2"), "seed", out]) == 1
