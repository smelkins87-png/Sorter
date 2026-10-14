"""Review page: scans of a day, verdicts, labels.csv for eval, export, auth."""
import base64
import csv
import json
import threading
import urllib.error
import urllib.request

import pytest

from localsorter import carddb
from localsorter.__main__ import main as cli
from localsorter.exporter import Exporter
from localsorter.matcher import Match
from localsorter.review import Review, make_review_server
from localsorter.settings import Settings

from . import synthetic
from .helpers import build_db, scryfall_card

DAY = "2026-10-14"


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(data_dir=str(tmp_path), export_dir=str(tmp_path / "scans"))
    conn = carddb.connect(settings.db_path)
    cards = [scryfall_card(i) for i in range(1, 6)]
    # A second printing of card 1, same art: what a correction picks
    reprint = scryfall_card(1, set_code="rep", oracle="oracle-0001")
    cards.append(reprint)
    images = {c["id"]: synthetic.make_card(int(c["collector_number"])) for c in cards}
    build_db(conn, cards, images)

    # Three scans on DAY: card 1 (set unsure), card 2 (sure), one not found
    exporter = Exporter(settings.export_dir)

    class FixedDate(__import__("datetime").datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 14, 12, 0, 0)

    monkeypatch.setattr("localsorter.exporter.datetime", FixedDate)
    scans = []
    for uuid, card_no, sure in (("aaaa1111", 1, False), ("bbbb2222", 2, True), ("cccc3333", None, False)):
        photo = synthetic.photo(synthetic.make_card(card_no or 99), seed=1)
        card = None
        if card_no:
            row = conn.execute("SELECT * FROM cards WHERE id = ?", ("card-%04d-tst" % card_no,)).fetchone()
            card = carddb.to_api_card(row)
        exporter.record(uuid, photo, card, Match(found=bool(card), distance=12, margin=40, set_detected=sure))
        scans.append(uuid)

    review = Review(settings, conn, threading.Lock())
    server = make_review_server(review, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    yield type("Env", (), dict(review=review, port=server.server_address[1], settings=settings, conn=conn,
                               tmp=tmp_path, reprint=reprint["id"]))

    server.shutdown()
    server.server_close()


def get(env, path, auth=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (env.port, path))
    if auth:
        request.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(request) as response:
        body = response.read()
        return response.status, response.headers, body


def post_label(env, **data):
    request = urllib.request.Request("http://127.0.0.1:%d/api/label" % env.port, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request))


def test_page_and_assets_are_served_locally(env):
    status, headers, body = get(env, "/")
    assert status == 200 and b"Sorter Review" in body
    # No external resources: works offline and leaks nothing
    assert b"http" not in body.replace(b"http-equiv", b"")
    for name in ("app.js", "style.css", "background.jpg"):
        assert get(env, "/static/" + name)[0] == 200


def test_days_and_needs_check(env):
    status, _, body = get(env, "/api/days")
    assert json.loads(body) == [{"day": DAY, "scans": 3, "needs_check": 2}]

    data = json.loads(get(env, "/api/scans?day=" + DAY)[2])
    by_uuid = {s["uuid"]: s for s in data["scans"]}
    assert by_uuid["aaaa1111"]["reasons"] == ["set not confirmed"]
    assert by_uuid["bbbb2222"]["needs_check"] is False
    assert by_uuid["cccc3333"]["reasons"] == ["not found"]
    assert data["stats"]["needs_check"] == 2 and data["stats"]["found"] == 2


def test_drop_check_disagreement_needs_check(env):
    exporter = Exporter(env.settings.export_dir)
    exporter.record_verification({"uuid": "bbbb2222", "title": "Card 2"}, "mismatch",
                                 {"title": "Card 4", "set": "TST", "num": "4"}, None)
    import datetime
    day = datetime.date.today().isoformat()
    # record_verification writes today's folder: point it at DAY for the test
    import shutil
    shutil.move(str(env.tmp / "scans" / day / "verify.csv"), str(env.tmp / "scans" / DAY / "verify.csv"))

    scan = {s["uuid"]: s for s in env.review.scans(DAY)}["bbbb2222"]
    assert scan["reasons"] == ["drop check: mismatch"]
    assert scan["drop_seen"] == "Card 4 (TST #4)"


def test_confirm_and_correct_write_labels_for_eval(env):
    post_label(env, day=DAY, uuid="bbbb2222", status="confirmed", card_id="card-0002-tst")
    post_label(env, day=DAY, uuid="aaaa1111", status="corrected", card_id=env.reprint)
    post_label(env, day=DAY, uuid="cccc3333", status="not_a_card")

    with open(env.tmp / "scans" / DAY / "labels.csv", encoding="utf-8-sig") as handle:
        labels = {r["image"]: r for r in csv.DictReader(handle)}

    assert labels["images/aaaa1111.jpg"]["set"] == "REP"
    assert labels["images/bbbb2222.jpg"]["status"] == "confirmed"
    assert "images/cccc3333.jpg" not in labels       # not a card: nothing to learn from

    stats = env.review.stats(DAY)
    assert (stats["needs_check"], stats["confirmed"], stats["corrected"]) == (0, 1, 2)

    # The file is exactly what `localsorter eval` takes
    assert cli(["--data-dir", str(env.tmp), "eval", str(env.tmp / "scans" / DAY / "labels.csv")]) == 0


def test_undo(env):
    post_label(env, day=DAY, uuid="aaaa1111", status="confirmed", card_id="card-0001-tst")
    post_label(env, day=DAY, uuid="aaaa1111", status="clear")

    assert {s["uuid"]: s for s in env.review.scans(DAY)}["aaaa1111"]["label"] is None


def test_export_applies_corrections(env):
    post_label(env, day=DAY, uuid="aaaa1111", status="corrected", card_id=env.reprint)
    status, headers, body = get(env, "/api/export?day=" + DAY)

    rows = list(csv.DictReader(body.decode("utf-8-sig").splitlines()))
    assert headers["Content-Disposition"] == 'attachment; filename="collection_%s.csv"' % DAY
    assert [(r["title"], r["set"], r["reviewed"]) for r in rows] == [("Card 1", "REP", "corrected"), ("Card 2", "TST", "")]


def test_printings_search_images_and_closeups(env):
    printings = json.loads(get(env, "/api/printings?card_id=card-0001-tst")[2])
    assert {p["set_code"] for p in printings} == {"TST", "REP"}

    results = json.loads(get(env, "/api/search?q=card%203")[2])
    assert results[0]["name"] == "Card 3"

    status, headers, body = get(env, "/api/image/%s/aaaa1111?size=thumb" % DAY)
    assert headers["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"

    with pytest.raises(urllib.error.HTTPError) as error:
        get(env, "/api/closeup/card-0001-tst")
    assert error.value.code == 404                      # no close-ups in this DB


@pytest.mark.parametrize("path", ["/api/image/..%2F..%2Fetc/passwd", "/api/scans?day=../../etc",
                                  "/static/..%2Freview.py", "/api/image/2026-10-14/..%2Fx"])
def test_no_path_tricks(env, path):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(env, path)
    assert error.value.code in (400, 404)


def test_bad_labels_are_rejected(env):
    for data in ({"day": DAY, "uuid": "aaaa1111", "status": "bogus"},
                 {"day": DAY, "uuid": "aaaa1111", "status": "corrected", "card_id": "no-such-card"},
                 {"day": "../x", "uuid": "aaaa1111", "status": "confirmed"}):
        with pytest.raises(urllib.error.HTTPError) as error:
            post_label(env, **data)
        assert error.value.code == 400


def test_password(env):
    server = make_review_server(env.review, "127.0.0.1", 0, password="ring")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = env.port
    env.port = server.server_address[1]

    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(env, "/api/days")
        assert error.value.code == 401
        with pytest.raises(urllib.error.HTTPError):
            get(env, "/api/days", auth="pi:wrong")
        assert get(env, "/api/days", auth="anyone:ring")[0] == 200
    finally:
        env.port = port
        server.shutdown()
        server.server_close()


def test_sorter_status_feeds_the_banner(env):
    status, _, body = get(env, "/api/status")
    assert status == 200 and json.loads(body) == {"waiting_review": None, "resorting": None}

    env.review.status = lambda: {"waiting_review": {"cards": 4, "position": 5, "since": "12:00"}, "resorting": None}
    assert json.loads(get(env, "/api/status")[2])["waiting_review"]["cards"] == 4


def test_resorted_cards_are_not_counted_twice(env):
    post_label(env, day=DAY, uuid="aaaa1111", status="corrected", card_id=env.reprint)

    # The same card pulled back out of the bin and sorted with that verdict
    class Reviewed(object):
        distance = margin = ""
        set_detected = True
        printing = {"method": "review", "reviewed_scan": "aaaa1111"}

    row = env.conn.execute("SELECT * FROM cards WHERE id = ?", (env.reprint,)).fetchone()
    Exporter(env.settings.export_dir).record("dddd4444", synthetic.photo(synthetic.make_card(1), seed=2),
                                             carddb.to_api_card(row), Reviewed())

    scans = json.loads(get(env, "/api/scans?day=" + DAY)[2])["scans"]
    assert [s["resorted"] for s in scans if s["uuid"] == "dddd4444"] == [True]

    rows = list(csv.DictReader(get(env, "/api/export?day=" + DAY)[2].decode("utf-8-sig").splitlines()))
    assert [(r["title"], r["set"], r["scan"]) for r in rows] == [("Card 1", "REP", "aaaa1111"),
                                                                 ("Card 2", "TST", "bbbb2222")]


def _request(env, path, data=None, headers=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (env.port, path), data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_other_sites_cant_use_the_page(env):
    label = json.dumps({"day": DAY, "uuid": "aaaa1111", "status": "confirmed",
                        "card_id": "card-0001-tst"}).encode()

    # DNS rebinding: a name that isn't the sorter's
    assert _request(env, "/api/days", headers={"Host": "evil.example:8766"}) == 403
    assert _request(env, "/api/days", headers={"Host": "localhost:8766"}) == 200

    # Cross-site writes: a form post, or another site's page
    assert _request(env, "/api/label", label, {"Content-Type": "text/plain"}) == 415
    assert _request(env, "/api/label", label, {"Content-Type": "application/json",
                                               "Origin": "http://evil.example"}) == 403
    assert _request(env, "/api/label", label, {"Content-Type": "application/json",
                                               "Origin": "http://127.0.0.1:%d" % env.port}) == 200


def test_day_list_is_cached_until_something_changes(env, monkeypatch):
    env.review.days()
    calls = []
    original = env.review.scans
    monkeypatch.setattr(env.review, "scans", lambda day: calls.append(day) or original(day))

    env.review.days()
    assert calls == []

    post_label(env, day=DAY, uuid="aaaa1111", status="confirmed", card_id="card-0001-tst")
    calls.clear()
    assert env.review.days()[0]["needs_check"] == 1 and calls == [DAY]
