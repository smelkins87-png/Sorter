"""Second photo after each drop: the server's /verify endpoint and the
verifydrop plugin (run against stand-ins for the sorter's arm, camera and
piles, since the vendor software can't run here)."""
import csv
import importlib.util
import io
import json
import os
import sys
import threading
import types
import urllib.request
from datetime import datetime

import pytest
from PIL import Image

from localsorter.server import App, make_server
from localsorter.settings import Settings

from . import synthetic
from .test_border import BIG, MID, world  # noqa: F401  (shared fixture)

PLUGIN = os.path.join(os.path.dirname(__file__), "..", "plugin", "localsorter", "localsorter.py")


# --- /verify ----------------------------------------------------------------

@pytest.fixture
def app(world, tmp_path):
    conn, index, images = world
    settings = Settings(data_dir=str(tmp_path), export_dir=str(tmp_path / "scans"), ocr="off")
    return App(settings, conn=conn, index=index), images


def _photo(images, card_id, seed=0):
    return synthetic.photo(images[card_id], seed=seed, background=MID, **BIG)


def _expected(title, code, num="1", sure=True):
    return {"uuid": "u-%s-%s" % (code, sure), "title": title, "set": code, "num": num,
            "confidence": {"set_detected": sure}}


def test_same_card_matches(app):
    app, images = app
    status, answer = app.verify(_photo(images, "print-LEB"), _expected("Card 1", "LEB"))

    assert status == 200 and answer["result"] == "match"
    assert answer["action"] == "manual_bin"


def test_different_card_is_a_mismatch(app):
    app, images = app
    status, answer = app.verify(_photo(images, "card-0005-tst"), _expected("Card 1", "LEB"))

    assert answer["result"] == "mismatch"
    assert answer["seen"]["title"] == "Card 5"


def test_unsure_first_photo_is_refined_by_a_confident_second(app):
    app, images = app
    # First photo said Unlimited, without being sure; the pile shows Beta (black border)
    status, answer = app.verify(_photo(images, "print-LEB"), _expected("Card 1", "2ED", sure=False))

    assert answer["result"] == "refined"
    assert answer["card"]["set"] == "LEB"


def test_two_confident_printings_disagree(app):
    app, images = app
    status, answer = app.verify(_photo(images, "print-LEB"), _expected("Card 1", "2ED", sure=True))

    assert answer["result"] == "printing_differs"


def test_printing_chosen_on_the_review_page_is_not_overruled(app):
    app, images = app
    expected = _expected("Card 1", "2ED")
    expected["confidence"] = {"printing": {"method": "review", "reviewed_scan": "u0"}}
    status, answer = app.verify(_photo(images, "print-LEB"), expected)

    assert answer["result"] == "match"

    # A different card on the pile still counts
    status, answer = app.verify(_photo(images, "card-0005-tst"), expected)
    assert answer["result"] == "mismatch"


def test_unrecognised_pile_and_unrecognised_card(app):
    app, images = app
    blank = synthetic.photo(synthetic.make_card(9999), seed=1, background=MID, **BIG)

    assert app.verify(blank, _expected("Card 1", "LEB"))[1]["result"] == "unreadable"
    assert app.verify(blank, {"uuid": "x"})[1]["result"] == "skipped"


def test_checks_are_logged(app, tmp_path):
    app, images = app
    app.verify(_photo(images, "card-0005-tst"), _expected("Card 1", "LEB"))

    day = datetime.now().strftime("%Y-%m-%d")
    with open(tmp_path / "scans" / day / "verify.csv", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    assert rows[-1]["result"] == "mismatch"
    assert rows[-1]["expected_set"] == "LEB" and rows[-1]["seen_title"] == "Card 5"
    # The second photo is only for the check: not kept
    assert "image" not in rows[-1]
    assert not list((tmp_path / "scans" / day).glob("images/*_drop.jpg"))
    # A check is not a new scan
    assert not (tmp_path / "scans" / day / "scans.csv").exists()


def test_verify_over_http(app):
    app, images = app
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        boundary = "XyZ"
        body = (b"--XyZ\r\nContent-Disposition: form-data; name=\"image\"; filename=\"drop.jpeg\"\r\n\r\n"
                + _photo(images, "print-LEB") +
                b"\r\n--XyZ\r\nContent-Disposition: form-data; name=\"expected\"\r\n\r\n"
                + json.dumps(_expected("Card 1", "LEB")).encode() + b"\r\n--XyZ--\r\n")
        request = urllib.request.Request("http://127.0.0.1:%d/verify/mtg" % server.server_address[1], data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        answer = json.load(urllib.request.urlopen(request))
    finally:
        server.shutdown()
        server.server_close()

    assert answer["result"] == "match"


# --- the plugin ---------------------------------------------------------------

class FakePosition(object):
    def __init__(self, index, x, y, height=0.0):
        self.index = index
        self.coords = {"x": x, "y": y}
        self.height = height
        self.processed = 0
        self.cards = []

    def add_card(self, card):
        self.cards.append(dict(card))


class FakeFrame(object):
    # Frame4's travel
    max_x = 440
    max_y = 430

    def __init__(self):
        self.moves = []

    def move(self, x, y, z):
        self.moves.append((x, y, z))

    def waitMoveCompletes(self):
        pass


class FakeMovements(object):
    def __init__(self, fail=False):
        self.grabbed = []
        self.released = []
        self.fail = fail

    def grabTheCard(self, feeder):
        if self.fail:
            raise RuntimeError("vacuum lost the card")
        self.grabbed.append((feeder.coords, feeder.height, feeder.processed))

    def releaseTheCard(self, feeder, destination):
        # What the vendor's Movements4.releaseTheCard does to the counters
        feeder.processed += 1
        destination.processed += 1
        destination.height += 0.35
        self.released.append(destination.index)


class PlateFullException(Exception):
    pass


class FakeRobot(object):
    def __init__(self, bin_position, fail=False):
        self._frame = FakeFrame()
        self._movements = FakeMovements(fail)
        self.global_pause = threading.Event()
        self.paused = False
        self._game_details = types.SimpleNamespace(get_card_height=lambda: 0.30)

        def handle(attribs, found):
            if bin_position == "full":
                raise PlateFullException("plate full")
            return bin_position

        self.matcher = types.SimpleNamespace(_handleResult=handle)

    def pause(self):
        self.paused = True


class FakeCamera(object):
    closed = False

    def capture(self, stream, format="jpeg"):
        Image.new("RGB", (40, 30), (90, 90, 90)).save(stream, "JPEG")


@pytest.fixture
def plugin_module(monkeypatch, tmp_path):
    """Loads plugin/verifydrop/verifydrop.py with the sorter's modules stubbed."""
    class PluginBase(object):
        def __init__(self, config):
            self._sorter_config = config
            self._logger = types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                                 debug=lambda *a: None)

    stubs = {
        "lib": types.ModuleType("lib"),
        "lib.utils": types.ModuleType("lib.utils"),
        "lib.utils.pluginbase": types.SimpleNamespace(PluginBase=PluginBase),
        "lib.matcher": types.ModuleType("lib.matcher"),
        "lib.matcher.card_attributes": types.SimpleNamespace(
            CardAttributes=types.SimpleNamespace(
                from_camera_card=lambda card: ({"title": card.get("title", ""), "set": card.get("set", "")},
                                               bool(card.get("title"))))),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("verifydrop_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SWITCH_DIR = str(tmp_path)    # the "verifydrop" switch folder

    return module


def _setup(plugin_module, answer, offsets=None, endpoint="http://127.0.0.1:8765", fail=False, x=100.0,
           bin_position=None, pile_set=None):
    config = {"endpoint": endpoint, "card_offset": offsets or {"x": 4, "y": 2}}
    plugin = plugin_module.Localsorter(config)
    plugin.onCameraPicTaken({"game": "mtg"}, types.SimpleNamespace(
        _camera=FakeCamera(),
        _camera_decorator=types.SimpleNamespace(processCardImage=lambda stream, pos: Image.open(stream))), None)
    asked = []
    plugin._ask = lambda photo, expected: asked.append(expected) or answer

    destination = FakePosition(3, x, 50.0, height=12.0)
    # The pile's sorting rule: by set when pile_set is given, else anything
    destination.match = lambda attribs, allowFixed=True, cardFound=False: (
        pile_set is None or attribs.get("set") == pile_set)
    destination.processed = 5
    record = {"uuid": "u1", "title": "Card 1", "set": "LEB", "num": "1", "confidence": {"set_detected": True}}
    destination.cards = [{"title": "Older"}, record]
    bin_position = bin_position or FakePosition(9, 300.0, 50.0)
    robot = FakeRobot(destination if bin_position == "same" else bin_position, fail)
    if bin_position == "same":
        bin_position = destination

    plugin.onRobotReleaseCard(robot, FakePosition(0, 0, 0), destination)

    return robot, destination, bin_position, record, asked


def test_camera_goes_over_the_destination_with_the_photo_offset(plugin_module):
    robot, *_ = _setup(plugin_module, {"result": "match"}, offsets={"x": 4, "y": 2})

    # 55 mm + card_offset.x sideways, card_offset.y but at least 10 mm
    assert robot._frame.moves == [(100.0 + 55 + 4, 50.0 + 10, None)]


def test_match_leaves_everything_alone(plugin_module):
    robot, destination, bin_position, record, asked = _setup(plugin_module, {"result": "match"})

    assert asked == [record]
    assert robot._movements.grabbed == [] and not robot.global_pause.is_set()
    assert destination.cards[-1] is record and destination.processed == 5


def test_mismatch_moves_the_card_to_the_manual_bin(plugin_module):
    answer = {"result": "mismatch", "action": "manual_bin", "seen": {"title": "Card 5", "set": "TST", "num": "5"}}
    robot, destination, bin_position, record, _ = _setup(plugin_module, answer)

    # Grabbed from the destination pile, starting from its tracked height
    assert robot._movements.grabbed == [({"x": 100.0, "y": 50.0}, 12.0, 0)]
    assert robot._movements.released == [9]

    # The record moved with the card, and the pile's counters were undone
    assert [c["title"] for c in destination.cards] == ["Older"]
    assert destination.processed == 5 - 1
    assert destination.height == pytest.approx(12.0 - 0.35)
    moved = bin_position.cards[-1]
    assert moved["title"] == "Card 1"
    assert moved["confidence"]["manual_check"]["reason"] == "mismatch"
    assert moved["confidence"]["manual_check"]["second_photo"]["title"] == "Card 5"
    assert not robot.global_pause.is_set()


def test_differing_printings_also_go_to_the_manual_bin(plugin_module):
    robot, destination, bin_position, *_ = _setup(plugin_module, {"result": "printing_differs", "action": "manual_bin"})

    assert robot._movements.released == [9]
    assert bin_position.cards[-1]["confidence"]["manual_check"]["reason"] == "printing_differs"


def test_mismatch_can_pause_instead(plugin_module):
    robot, destination, *_ = _setup(plugin_module, {"result": "mismatch", "action": "pause"})

    # Both: the robot's own flag stops it before the next card
    assert robot.global_pause.is_set() and robot.paused
    assert robot._movements.grabbed == [] and len(destination.cards) == 2


def test_mismatch_warn_only(plugin_module):
    robot, destination, *_ = _setup(plugin_module, {"result": "mismatch", "action": "warn"})

    assert not robot.global_pause.is_set() and robot._movements.grabbed == []


def test_failed_regrab_pauses(plugin_module):
    robot, destination, bin_position, record, _ = _setup(
        plugin_module, {"result": "mismatch", "action": "manual_bin"}, fail=True)

    assert robot.global_pause.is_set()
    assert destination.cards[-1] is record and bin_position.cards == []


def test_refined_updates_the_record(plugin_module):
    answer = {"result": "refined", "card": {"set": "2ED", "num": "1", "ext_id": "s2"}}
    robot, destination, bin_position, record, _ = _setup(plugin_module, answer)

    assert record["set"] == "2ED" and record["ext_id"] == "s2"
    assert robot._movements.grabbed == []


def test_refined_to_a_printing_that_belongs_elsewhere_goes_to_the_manual_bin(plugin_module):
    # Sorting by set: the pile is for LEB, the second photo says it's 2ED
    answer = {"result": "refined", "action": "manual_bin", "card": {"set": "2ED", "num": "1"}}
    robot, destination, bin_position, record, _ = _setup(plugin_module, answer, pile_set="LEB")

    assert robot._movements.released == [9]
    moved = bin_position.cards[-1]
    assert moved["set"] == "2ED"
    assert moved["confidence"]["manual_check"]["reason"] == "refined_other_pile"


def test_refined_that_still_fits_the_pile_stays(plugin_module):
    answer = {"result": "refined", "action": "manual_bin", "card": {"set": "LEB", "num": "1"}}
    robot, destination, *_ = _setup(plugin_module, answer, pile_set="LEB")

    assert robot._movements.grabbed == []


def test_full_plate_pauses(plugin_module):
    robot, destination, _, record, _ = _setup(
        plugin_module, {"result": "mismatch", "action": "manual_bin"}, bin_position="full")

    assert robot.paused and robot._movements.grabbed == []
    assert destination.cards[-1] is record


def test_card_already_in_the_manual_pile_stays_without_pausing(plugin_module):
    robot, *_ = _setup(plugin_module, {"result": "mismatch", "action": "manual_bin"}, bin_position="same")

    assert not robot.paused and robot._movements.grabbed == []


def test_unreadable_second_photo_is_not_a_mismatch(plugin_module):
    robot, *_ = _setup(plugin_module, {"result": "unreadable", "action": "manual_bin"})

    assert robot._movements.grabbed == [] and not robot.global_pause.is_set()


def test_switch_folder_missing_means_off(plugin_module, tmp_path):
    plugin_module.SWITCH_DIR = str(tmp_path / "not-installed")
    robot, *_, asked = _setup(plugin_module, {"result": "match"})

    assert robot._frame.moves == [] and asked == []


def test_switched_off_with_disabled_txt(plugin_module, tmp_path):
    (tmp_path / "disabled.txt").write_text("")
    robot, *_, asked = _setup(plugin_module, {"result": "mismatch", "action": "manual_bin"})

    assert robot._frame.moves == [] and asked == []


def test_never_runs_against_the_vendor_cloud(plugin_module):
    robot, *_, asked = _setup(plugin_module, {"result": "match"}, endpoint="https://api2.magic-sorter.com")

    assert robot._frame.moves == [] and asked == []


def test_piles_the_camera_cannot_reach_are_not_checked(plugin_module):
    # 400 + 55 + 4 is past Frame4's 440 mm: the move would be clamped short
    robot, destination, bin_position, record, asked = _setup(
        plugin_module, {"result": "mismatch", "action": "manual_bin"}, x=400.0)

    assert robot._frame.moves == [] and asked == []
    assert robot._movements.grabbed == [] and destination.cards[-1] is record


def test_log_started_by_an_earlier_version_keeps_its_columns(tmp_path):
    from localsorter.exporter import Exporter

    day = datetime.now().strftime("%Y-%m-%d")
    (tmp_path / day).mkdir()
    old = ["timestamp", "uuid", "result", "expected_title", "expected_set", "expected_number",
           "seen_title", "seen_set", "seen_number", "seen_set_detected", "distance", "image"]
    (tmp_path / day / "verify.csv").write_text(",".join(old) + "\r\n", encoding="utf-8-sig")

    Exporter(str(tmp_path)).record_verification({"uuid": "u1", "title": "Card 1"}, "match", {"title": "Card 1"})

    with open(tmp_path / day / "verify.csv", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [dict(rows[0], result="match", image="")] and rows[0]["seen_title"] == "Card 1"
