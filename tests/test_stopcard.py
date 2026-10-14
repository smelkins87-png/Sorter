"""The stop card at the bottom of each feeder pile: learnt from a scan on the
review page, then answered as the vendor's servers did ("stoptoken")."""
import json
import os
import threading
import urllib.request

import pytest

from localsorter import carddb
from localsorter.review import Review, make_review_server
from localsorter.server import App, make_server
from localsorter.settings import Settings
from localsorter.stopcard import StopCards

from . import synthetic
from .helpers import build_db, scryfall_card

STOP = 99          # a card that isn't in the database, like the vendor's stop card


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=str(tmp_path), export_dir=str(tmp_path / "scans"), ocr="off")
    conn = carddb.connect(settings.db_path)
    cards = [scryfall_card(i) for i in range(1, 6)]
    build_db(conn, cards, {c["id"]: synthetic.make_card(int(c["collector_number"])) for c in cards})
    app = App(settings, conn=conn)
    review = Review(settings, conn, app.db_lock)

    return app, review


def _scan(app, card_no, seed, **kwargs):
    return app.identify(synthetic.photo(synthetic.make_card(card_no), seed=seed, **kwargs), {})


def _day(app):
    return sorted(d for d in os.listdir(app.settings.export_dir) if d[:2] == "20")[-1]


def test_stop_card_is_learnt_from_a_scan_then_recognised(env):
    app, review = env

    # First time through: just a card nobody knows, kept for the review page
    status, answer = _scan(app, STOP, 1)
    assert status == 404
    day, uuid = _day(app), answer["uuid"]

    review.label(day, uuid, "stop_card")

    # From now on: the stop card, upright or upside down, from another photo
    for kwargs in ({"seed": 2}, {"seed": 3, "upside_down": True}):
        status, answer = _scan(app, STOP, **kwargs)
        assert status == 200 and answer["result"]["title"] == "stoptoken"
        assert json.loads(answer["result"]["extra"]) == {}

    # Real cards are untouched
    assert _scan(app, 3, 4)[1]["result"]["title"] == "Card 3"

    # It isn't a card: not logged as a scan again, not in the collection
    rows = review.scans(day)
    assert [r["uuid"] for r in rows if r["status"] != "found"] == [uuid]
    assert "Stop" not in review.export(day) and review.stats(day)["needs_check"] == 0


def test_undo_forgets_the_stop_card(env):
    app, review = env
    status, answer = _scan(app, STOP, 1)
    review.label(_day(app), answer["uuid"], "stop_card")
    assert app.stop_cards

    review.label(_day(app), answer["uuid"], "clear")
    assert not app.stop_cards
    assert _scan(app, STOP, 2)[0] == 404


def test_relabelling_keeps_it_unless_the_label_is_saved(env):
    app, review = env
    status, answer = _scan(app, STOP, 1)
    review.label(_day(app), answer["uuid"], "stop_card")

    with pytest.raises(ValueError):
        review.label(_day(app), answer["uuid"], "corrected", "no-such-card")
    assert app.stop_cards


def test_a_scan_without_a_photo_cant_be_the_stop_card(env, tmp_path):
    app, review = env
    _scan(app, STOP, 1)

    with pytest.raises(ValueError):
        review.label(_day(app), "0000aaaa-never-saved", "stop_card")


def test_fingerprints_survive_a_restart(env):
    app, review = env
    status, answer = _scan(app, STOP, 1)
    review.label(_day(app), answer["uuid"], "stop_card")

    again = StopCards(app.conn, threading.Lock())
    assert again.uuids() == {answer["uuid"]}


def test_stop_card_over_http_and_last_not_found(env):
    app, review = env
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    try:
        # Nothing unrecognised yet: no id (and no error)
        with urllib.request.urlopen(base + "/last-not-found") as response:
            assert json.load(response) == {"uuid": None}

        status, answer = _scan(app, STOP, 1)
        with urllib.request.urlopen(base + "/last-not-found") as response:
            assert json.load(response) == {"uuid": answer["uuid"]}
    finally:
        server.shutdown()
        server.server_close()


def test_review_page_button(env):
    app, review = env
    status, answer = _scan(app, STOP, 1)
    server = make_review_server(review, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/label" % server.server_address[1],
            data=json.dumps({"day": _day(app), "uuid": answer["uuid"], "status": "stop_card"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            assert response.status == 200

        scan = {s["uuid"]: s for s in review.scans(_day(app))}[answer["uuid"]]
        assert scan["label"]["status"] == "stop_card"
        assert _scan(app, STOP, 2)[1]["result"]["title"] == "stoptoken"
    finally:
        server.shutdown()
        server.server_close()
