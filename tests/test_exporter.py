import csv
import os
from datetime import datetime

from localsorter.exporter import COLUMNS, Exporter
from localsorter.matcher import Match

BOM = b"\xef\xbb\xbf"
IMG = b"\xff\xd8\xff\xe0 fake jpeg payload \x00\x01"
TODAY = datetime.now().strftime("%Y-%m-%d")

CARD = {
    "title": "Lightning Bolt",
    "set": "TST",
    "set_name": "Test Set TST",
    "num": "42",
    "rarity": "common",
    "lang": "en",
    "foil": 0,
    "condition": "NM",
    "price": 1.5,
    "currency": "USD",
    "ecommerce_id": 1042,
    "ext_id": "card-0042-tst",
}


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _day(root):
    return root / TODAY


def test_record_found_writes_row_and_image(tmp_path):
    exporter = Exporter(export_dir=tmp_path, save_images=True)
    match = Match(found=True, card_id="card-0042-tst", distance=12, margin=40, set_detected=True)

    exporter.record("uuid-1", IMG, CARD, match)

    day = _day(tmp_path)
    with open(day / "scans.csv", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        row = dict(zip(header, next(reader)))

    assert header == COLUMNS
    assert row["status"] == "found"
    assert row["uuid"] == "uuid-1"
    assert row["title"] == "Lightning Bolt"
    assert row["scryfall_id"] == "card-0042-tst"
    assert row["distance"] == "12"
    assert row["margin"] == "40"
    assert row["set_detected"] == "1"
    assert row["image"] == os.path.join("images", "uuid-1.jpg")
    assert (day / "images" / "uuid-1.jpg").read_bytes() == IMG


def test_record_not_found_writes_not_found_row(tmp_path):
    exporter = Exporter(export_dir=tmp_path, save_images=True)

    exporter.record("uuid-2", IMG, card=None)

    rows = _rows(_day(tmp_path) / "scans.csv")
    assert len(rows) == 1
    assert rows[0]["status"] == "not_found"
    assert rows[0]["uuid"] == "uuid-2"
    assert rows[0]["title"] == ""
    assert rows[0]["currency"] == ""
    assert rows[0]["set_detected"] == ""
    assert (_day(tmp_path) / "images" / "uuid-2.jpg").read_bytes() == IMG


def test_two_records_write_header_once_with_bom(tmp_path):
    exporter = Exporter(export_dir=tmp_path, save_images=False)

    exporter.record("uuid-a", b"", CARD)
    exporter.record("uuid-b", b"", None)

    path = _day(tmp_path) / "scans.csv"
    raw = path.read_bytes()
    rows = _rows(path)

    assert raw.startswith(BOM)
    assert raw.count(BOM) == 1
    assert raw.count(b"timestamp,") == 1
    assert len(rows) == 2
    assert [r["uuid"] for r in rows] == ["uuid-a", "uuid-b"]
    assert [r["status"] for r in rows] == ["found", "not_found"]


def test_save_images_false_writes_no_images(tmp_path):
    exporter = Exporter(export_dir=tmp_path, save_images=False)

    exporter.record("uuid-3", IMG, CARD)

    day = _day(tmp_path)
    assert not (day / "images").exists()
    assert os.listdir(day) == ["scans.csv"]
    assert _rows(day / "scans.csv")[0]["image"] == ""


def test_retention_removes_old_images_only(tmp_path):
    old_day = tmp_path / "2000-01-01"
    old_images = old_day / "images"
    old_images.mkdir(parents=True)
    (old_images / "old.jpg").write_bytes(b"old")
    (old_day / "scans.csv").write_text("kept\n")

    exporter = Exporter(export_dir=tmp_path, save_images=True, retention_days=30)
    exporter.record("uuid-4", IMG, CARD)

    assert not old_images.exists()
    assert old_day.is_dir()
    assert (old_day / "scans.csv").read_text() == "kept\n"
    assert (_day(tmp_path) / "images" / "uuid-4.jpg").exists()


def test_store_session_strips_path_traversal(tmp_path):
    exporter = Exporter(export_dir=tmp_path)

    path = exporter.store_session("../evil.csv", b"x")

    assert path == str(tmp_path / "sessions" / "evil.csv")
    assert (tmp_path / "sessions" / "evil.csv").read_bytes() == b"x"
    assert not (tmp_path / "evil.csv").exists()
