"""Re-sorting the manual-check bin with the review page's verdicts: the
server's matching (resort.py, /resort) and the plugin pulling cards back out
of the bin (against stand-ins for the sorter's robot and plate)."""
import importlib.util
import os
import sys
import threading
import time
import types

import pytest

from localsorter import carddb
from localsorter.exporter import Exporter
from localsorter.review import LABELS_SCHEMA
from localsorter.server import App
from localsorter.settings import Settings

from . import synthetic
from .helpers import build_db, scryfall_card

DAY = "2026-10-14"
PLUGIN = os.path.join(os.path.dirname(__file__), "..", "plugin", "localsorter", "localsorter.py")


# --- server side ---------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=str(tmp_path), export_dir=str(tmp_path / "scans"), ocr="off")
    conn = carddb.connect(settings.db_path)
    cards = [scryfall_card(i) for i in range(1, 8)]
    reprint = scryfall_card(1, set_code="rep", oracle="oracle-0001")
    cards.append(reprint)
    build_db(conn, cards, {c["id"]: synthetic.make_card(int(c["collector_number"])) for c in cards})
    with conn:
        conn.executescript(LABELS_SCHEMA)

    app = App(settings, conn=conn)
    images = os.path.join(settings.export_dir, DAY, "images")
    os.makedirs(images)

    def scan(uuid, card_no, seed, label=None, card_id=None):
        """A scan sitting in the manual-check bin, as the first pass left it."""
        with open(os.path.join(images, uuid + ".jpg"), "wb") as handle:
            handle.write(synthetic.photo(synthetic.make_card(card_no), seed=seed))
        if label:
            with conn:
                conn.execute("INSERT INTO labels VALUES (?, ?, ?, ?, 'now')", (uuid, DAY, card_id, label))

    def pull(card_no, seed):
        """The same physical card photographed again, pulled from the bin."""
        return app.identify(synthetic.photo(synthetic.make_card(card_no), seed=seed + 100, angle=1.5),
                            {"lang": ["en"], "foil": ["0"]})

    return types.SimpleNamespace(app=app, conn=conn, scan=scan, pull=pull, reprint=reprint["id"])


def test_reviewed_cards_come_back_with_their_verdicts(env):
    # Bin, bottom to top: card 3 (unreviewed), card 2 (confirmed), card 1 (corrected to the reprint)
    env.scan("u3", 3, 3)
    env.scan("u2", 2, 2, "confirmed", "card-0002-tst")
    env.scan("u1", 1, 1, "corrected", env.reprint)
    assert env.app.resort_start(["u1", "u2", "u3"])[1]["cards"] == 3

    status, answer = env.pull(1, 1)
    assert status == 200 and answer["result"]["set"] == "REP"
    assert answer["result"]["confidence"]["printing"]["method"] == "review"

    status, answer = env.pull(2, 2)
    assert answer["result"]["title"] == "Card 2" and answer["result"]["detect_set"] == 1

    # Not reviewed: identified the usual way
    status, answer = env.pull(3, 3)
    assert status == 200 and answer["result"]["title"] == "Card 3"
    assert "review" not in str(answer["result"]["confidence"])


def test_disturbed_pile_is_matched_by_photo(env):
    env.scan("u2", 2, 2, "confirmed", "card-0002-tst")
    env.scan("u1", 1, 1, "corrected", env.reprint)
    env.app.resort_start(["u1", "u2"])

    # Card 2 comes out first although card 1 was on top
    assert env.pull(2, 2)[1]["result"]["title"] == "Card 2"
    assert env.pull(1, 1)[1]["result"]["set"] == "REP"


def test_not_a_card_goes_back_to_the_manual_check(env):
    env.scan("u4", 4, 4, "not_a_card", None)
    env.app.resort_start(["u4"])

    status, answer = env.pull(4, 4)
    assert status == 404


def test_identical_copies_with_different_verdicts_are_not_guessed(env):
    # Card 3 should be on top, but one of two look-alike copies of card 1,
    # reviewed as different printings, comes out first: the photo can't
    # tell which copy it is
    env.scan("a", 1, 1, "confirmed", "card-0001-tst")
    env.scan("b", 1, 2, "corrected", env.reprint)
    env.scan("top", 3, 3, "confirmed", "card-0003-tst")
    env.app.resort_start(["top", "a", "b"])

    status, answer = env.pull(1, 5)
    assert status == 404       # back to the manual check, not a coin toss

    # Pile order intact: the copy on top gets its own verdict
    assert env.pull(3, 3)[1]["result"]["title"] == "Card 3"


def test_unknown_photo_and_stop(env):
    env.scan("u1", 1, 1, "corrected", env.reprint)
    env.app.resort_start(["u1"])

    # A card that isn't one of the bin's: normal identification
    status, answer = env.pull(5, 5)
    assert answer["result"]["title"] == "Card 5"

    env.app.resort_stop()
    status, answer = env.pull(1, 1)
    assert answer["result"]["set"] == "TST"          # no verdicts once stopped


def test_sorter_status_for_the_banner(env):
    assert env.app.sorter_status() == {"waiting_review": None, "resorting": None}

    env.app.resort_waiting({"cards": 2, "position": 5})
    waiting = env.app.sorter_status()["waiting_review"]
    assert waiting["cards"] == 2 and waiting["position"] == 5 and waiting["since"]

    env.scan("u1", 1, 1, "corrected", env.reprint)
    env.scan("u2", 2, 2)
    env.app.resort_start(["u1", "u2"])
    assert env.app.sorter_status() == {"waiting_review": None, "resorting": {"left": 2}}

    env.pull(1, 1)
    assert env.app.sorter_status()["resorting"] == {"left": 1}

    env.app.resort_stop()
    assert env.app.sorter_status() == {"waiting_review": None, "resorting": None}

    # Stop pressed while waiting clears the banner
    env.app.resort_waiting({"cards": 3})
    env.app.resort_waiting({"cards": 0})
    assert env.app.sorter_status()["waiting_review"] is None


def test_last_not_found_is_the_servers_scan_id(env):
    status, answer = env.app.identify(synthetic.photo(synthetic.make_card(99), seed=1), {})
    assert status == 404 and env.app.last_not_found == answer["uuid"]


def test_unreadable_photo_is_skipped(env):
    env.scan("u1", 1, 1, "corrected", env.reprint)
    with open(os.path.join(env.app.settings.export_dir, DAY, "images", "bad.jpg"), "wb") as handle:
        handle.write(b"not a jpeg")
    env.app.resort_start(["bad", "u1"])
    assert env.app.resort.queue == ["u1"]


def test_look_alikes_after_the_pile_was_disturbed(env):
    # Card 3 comes out before card 2 (pile disturbed). Later, the copy of
    # card 1 on top can't be told from its look-alike with another verdict
    env.scan("a", 1, 1, "confirmed", "card-0001-tst")
    env.scan("b", 1, 2, "corrected", env.reprint)
    env.scan("two", 2, 2, "confirmed", "card-0002-tst")
    env.scan("three", 3, 3, "confirmed", "card-0003-tst")
    env.app.resort_start(["two", "three", "a", "b"])

    assert env.pull(3, 3)[1]["result"]["title"] == "Card 3"
    assert env.pull(2, 2)[1]["result"]["title"] == "Card 2"
    assert env.pull(1, 1)[0] == 404


def test_scans_without_a_photo_are_skipped(env):
    env.app.resort_start(["never-saved"])
    assert env.app.resort.queue == []


# --- the plugin ------------------------------------------------------------------

class Criteria(dict):
    pass


class Position(object):
    def __init__(self, index, fixed=False, feeder=False, cards=0):
        self.index = index
        self.coords = {"x": 10.0 * index, "y": 50.0}
        self.height = 0.35 * cards
        self.processed = cards
        self.isFeeder = feeder
        self.enabled = True
        self.criteria = Criteria(rule="fixed", value=True) if fixed else None
        self.cards = [{"uuid": "u%d-%d" % (index, n), "title": ""} for n in range(cards)]


class Plate(object):
    def __init__(self, positions):
        self.positions = positions
        self.probed = []

    def get_feeders(self, enabledOnly=True):
        return [p for p in self.positions if p.isFeeder and (p.enabled or not enabledOnly)]

    def get_positions_flat(self):
        return list(self.positions)

    def probeFeedingPositions(self, positions):
        for p in positions:
            self.probed.append(p.index)
            p.processed = 0
            p.height = 4.0


class Robot(object):
    def __init__(self, plate):
        self._plate = plate
        self._pause = False
        self._running = True
        self.global_pause = threading.Event()
        self._game_details = types.SimpleNamespace(get_card_height=lambda: 0.25)

    def pause(self):
        self._pause = True


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    class PluginBase(object):
        def __init__(self, config):
            self._sorter_config = config
            self.logged = []
            self._logger = types.SimpleNamespace(info=self.logged.append, warning=self.logged.append,
                                                 debug=lambda *a: None)

    for name, module in {"lib": types.ModuleType("lib"), "lib.utils": types.ModuleType("lib.utils"),
                         "lib.utils.pluginbase": types.SimpleNamespace(PluginBase=PluginBase)}.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("localsorter_plugin_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "resortbin").mkdir()
    module.RESORT_SWITCH_DIR = str(tmp_path / "resortbin")
    module.SWITCH_DIR = str(tmp_path / "verifydrop-not-installed")

    instance = module.Localsorter({"endpoint": "http://127.0.0.1:8765"})
    instance.posted = []
    instance._post = lambda path, data, timeout=60: instance.posted.append((path, data)) or {"result": "ok"}
    instance.module = module

    return instance


def _resume_after(robot, seconds=0.2, then=None):
    def resume():
        if then:
            then()
        robot._pause = False

    threading.Timer(seconds, resume).start()


def _pull(plugin, robot, bin_, destination, uuid="new", reviewed=None):
    """The robot taking a card off the bin: the matcher files its record
    on the destination, then the card is dropped there."""
    assert plugin.onRobotGetNextFeeder(robot) is bin_
    record = {"uuid": uuid, "title": "x"}
    if reviewed:
        record["confidence"] = {"printing": {"method": "review", "reviewed_scan": reviewed}}
    destination.cards.append(record)
    plugin.onRobotReleaseCard(robot, bin_, destination)


def test_waits_while_feeders_have_cards(plugin):
    feeder = Position(1, feeder=True)
    robot = Robot(Plate([feeder, Position(5, fixed=True, cards=3), Position(6, fixed=True)]))

    assert plugin.onRobotGetNextFeeder(robot) is None and not robot._pause


def test_pauses_for_review_then_pulls_the_bin(plugin):
    bin_ = Position(5, fixed=True, cards=3)
    other = Position(6, fixed=True)
    feeder = Position(1, feeder=True)
    feeder.enabled = False                      # ended on its stop token
    robot = Robot(Plate([feeder, bin_, other]))

    _resume_after(robot)
    assert plugin.onRobotGetNextFeeder(robot) is bin_
    assert robot.global_pause.is_set()          # it paused and waited for Resume
    assert any("Review them at http://" in str(m) for m in plugin.logged)

    # The review page's banner was told, then the top of the pile first;
    # measured like a feeder; disabled, so never a destination meanwhile
    assert plugin.posted == [("/resort/waiting", {"cards": 3, "position": 5}),
                             ("/resort/start", {"uuids": ["u5-2", "u5-1", "u5-0"]})]
    assert robot._plate.probed == [5] and not bin_.enabled and not bin_.isFeeder

    for n in range(3):
        _pull(plugin, robot, bin_, other, uuid="new%d" % n)
        assert len(bin_.cards) == 2 - n          # records leave with their cards

    # Empty: back to a normal bin, server told, sorter takes over again
    assert plugin.onRobotGetNextFeeder(robot) is None
    assert plugin.posted[-1] == ("/resort/stop", {})
    assert bin_.enabled and bin_.cards == [] and bin_.height == 0
    assert [c["uuid"] for c in other.cards] == ["new0", "new1", "new2"]


def test_stop_while_waiting_for_review(plugin):
    bin_ = Position(5, fixed=True, cards=2)
    robot = Robot(Plate([bin_, Position(6, fixed=True)]))
    threading.Timer(0.2, lambda: setattr(robot, "_running", False)).start()

    assert plugin.onRobotGetNextFeeder(robot) is None
    # Only the banner was told (and cleared): no re-sort started
    assert plugin.posted == [("/resort/waiting", {"cards": 2, "position": 5}),
                             ("/resort/waiting", {"cards": 0})]
    assert not bin_.isFeeder


def test_banner_is_best_effort(plugin):
    def post(path, data, timeout=60):
        if path == "/resort/waiting":
            raise IOError("server busy")
        return {"result": "ok"}

    plugin._post = post
    bin_ = Position(5, fixed=True, cards=1)
    robot = Robot(Plate([bin_, Position(6, fixed=True)]))
    _resume_after(robot)

    assert plugin.onRobotGetNextFeeder(robot) is bin_


def test_stop_halfway_keeps_the_cards_still_in_the_bin(plugin):
    bin_ = Position(5, fixed=True, cards=3)
    other = Position(6, fixed=True)
    robot = Robot(Plate([bin_, other]))
    _resume_after(robot)
    _pull(plugin, robot, bin_, other)                      # one pulled

    # Stop: the robot never asks for another feeder. The results file is
    # written now, so the bin must hold the right records already; disabled
    # positions are still exported (feeders aren't)
    robot._running = False
    assert [c["uuid"] for c in bin_.cards] == ["u5-0", "u5-1"]   # the bottom two remain
    assert not bin_.isFeeder

    # The next run starts clean, and so does the server
    plugin.onRobotBeforeRun(Robot(Plate([])))
    assert plugin._resort is None and plugin.posted[-1] == ("/resort/stop", {})


def test_a_disturbed_pile_takes_the_right_record_out(plugin):
    bin_ = Position(5, fixed=True, cards=3)
    other = Position(6, fixed=True)
    robot = Robot(Plate([bin_, other]))
    _resume_after(robot)
    _pull(plugin, robot, bin_, other, reviewed="u5-0")      # the bottom card came out first

    assert [c["uuid"] for c in bin_.cards] == ["u5-1", "u5-2"]


def test_a_refused_card_ends_the_resort(plugin):
    # mess detector / stop token: the sorter disables the "feeder" and
    # releases nothing, so pulling would go on forever
    bin_ = Position(5, fixed=True, cards=3)
    robot = Robot(Plate([bin_, Position(6, fixed=True)]))
    _resume_after(robot)
    assert plugin.onRobotGetNextFeeder(robot) is bin_

    plugin.onRobotStopToken(robot, bin_)
    assert plugin._resort is None and bin_.enabled and len(bin_.cards) == 3
    assert plugin.posted[-1] == ("/resort/stop", {})

    # Those cards were offered once: no new pause for them
    assert plugin.onRobotGetNextFeeder(robot) is None and not robot._pause


def test_resume_with_empty_not_found_while_waiting(plugin):
    bin_ = Position(5, fixed=True, cards=2)
    robot = Robot(Plate([bin_, Position(6, fixed=True)]))

    def empty():                       # Plate.empty_notfound
        bin_.cards, bin_.height, bin_.processed = [], 0, 0

    _resume_after(robot, then=empty)
    assert plugin.onRobotGetNextFeeder(robot) is None
    assert not any(path == "/resort/start" for path, _ in plugin.posted)


def test_empty_not_found_during_the_resort_ends_it(plugin):
    bin_ = Position(5, fixed=True, cards=3)
    other = Position(6, fixed=True)
    robot = Robot(Plate([bin_, other]))
    _resume_after(robot)
    _pull(plugin, robot, bin_, other)

    bin_.cards, bin_.enabled = [], True        # Plate.empty_notfound on a later Resume
    assert plugin.onRobotGetNextFeeder(robot) is None
    assert plugin._resort is None and bin_.cards == []


def test_one_round_per_batch(plugin):
    bin_ = Position(5, fixed=True, cards=2)
    other = Position(6, fixed=True)
    robot = Robot(Plate([bin_, other]))
    _resume_after(robot)
    for n in range(2):
        _pull(plugin, robot, bin_, other, uuid="again%d" % n)   # still unresolved: the other bin
    assert plugin.onRobotGetNextFeeder(robot) is None

    # Unresolved cards stay for a manual check; new cards get their own round
    assert plugin.onRobotGetNextFeeder(robot) is None and not robot._pause
    other.cards.append({"uuid": "fresh", "title": ""})
    _resume_after(robot)
    assert plugin.onRobotGetNextFeeder(robot) is other


def test_layer_sorting_is_left_alone(plugin, monkeypatch):
    matcher = types.ModuleType("lib.matcher.matcher")
    matcher.Matcher = types.SimpleNamespace(layer_ordering=True)
    monkeypatch.setitem(sys.modules, "lib.matcher", types.ModuleType("lib.matcher"))
    monkeypatch.setitem(sys.modules, "lib.matcher.matcher", matcher)
    robot = Robot(Plate([Position(5, fixed=True, cards=2), Position(6, fixed=True)]))

    assert plugin.onRobotGetNextFeeder(robot) is None and not robot._pause


def test_not_found_cards_get_the_servers_scan_id(plugin):
    plugin._get = lambda path: {"uuid": "server-uuid"} if path == "/last-not-found" else {}
    card = {"uuid": "vendor-uuid", "confidence": {}}
    plugin.onCameraCardNotFound({}, card, None)
    assert card["uuid"] == "server-uuid"

    plugin._get = lambda path: (_ for _ in ()).throw(IOError("down"))
    card = {"uuid": "vendor-uuid"}
    plugin.onCameraCardNotFound({}, card, None)
    assert card["uuid"] == "vendor-uuid"


def test_needs_a_second_bin(plugin):
    robot = Robot(Plate([Position(5, fixed=True, cards=2)]))

    assert plugin.onRobotGetNextFeeder(robot) is None and not robot._pause


def test_switched_off(plugin, tmp_path):
    (tmp_path / "resortbin" / "disabled.txt").write_text("")
    robot = Robot(Plate([Position(5, fixed=True, cards=2), Position(6, fixed=True)]))

    assert plugin.onRobotGetNextFeeder(robot) is None and not robot._pause
