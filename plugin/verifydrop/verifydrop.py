"""verifydrop: photographs the destination pile after each drop and checks
that the card on top is the one that should be there.

Installed into the sorter's plugins folder by deploy/install.sh, switched off
(disabled.txt) until enabled from the touchscreen: System settings >
Available plugins. The switch takes effect from the next card, no restart.

The camera sits on the head, offset from the suction nozzle. For the feeder
photo the sorter moves the head by 55 mm + card_offset.x and at least 10 mm
in y (lib/organizer/robot.py, _move_for_picture); the same offset is applied
here over the destination pile, so card_offset calibration carries over.

When the two photos disagree (a different card, or both confident about
different printings), what happens is set by "verify_mismatch_action" in
/etc/localsorter.json:

  manual_bin  (default) pick the card back up and put it with the cards that
              weren't recognised, for a manual check
  pause       pause the sorter
  warn        only log it (touchscreen: Logging)

The check has to finish before the next card lands on the pile, so the arm
waits for the move, the photo and the identification.
"""
import io
import json
import os

from lib.utils.pluginbase import PluginBase

HERE = os.path.dirname(os.path.abspath(__file__))

# Same as Robot._move_for_picture
CAMERA_OFFSET_X = 55
MIN_OFFSET_Y = 10
# Results that send the card to the manual check
DISAGREE = ("mismatch", "printing_differs")


class _PileTop(object):
    """Stand-in feeder for grabbing the top card off a destination pile.
    Movements4.grabTheCard goes to height - processed * 0.25 + 5 mm and
    probes down to the card, so height must be at or above the pile top:
    the tracked height of a destination pile only ever overestimates it."""

    def __init__(self, position):
        self.coords = position.coords
        self.height = position.height
        self.processed = 0
        self.index = position.index
        self.cards = []


class Verifydrop(PluginBase):
    def __init__(self, config):
        super(Verifydrop, self).__init__(config)
        self._camera = None
        self._game = "mtg"

    @staticmethod
    def get_version():
        return "1.1.0"

    def _enabled(self):
        return not os.path.exists(os.path.join(HERE, "disabled.txt"))

    def _endpoint(self):
        endpoint = self._sorter_config.get("endpoint", "")

        # Only localsorter knows /verify; never send photos to the vendor cloud
        if not endpoint or "magic-sorter" in endpoint:
            return None

        return endpoint.rstrip("/")

    # --- sorter events -----------------------------------------------------

    def onCameraPicTaken(self, commandData, camera_obj, card_art):
        # Remember the camera; returning None leaves identification alone
        self._camera = camera_obj
        self._game = commandData.get("game", "mtg")

    def onRobotReleaseCard(self, robot, feeder, destination):
        if not self._enabled() or self._camera is None or not self._endpoint():
            return

        # Cards already in the manual-check bin need no second opinion
        if not destination.cards or not destination.cards[-1].get("title"):
            return

        expected = destination.cards[-1]
        photo = self._photograph(robot, destination)

        if photo is None:
            return

        try:
            answer = self._ask(photo, expected)
        except Exception as error:  # never take the sorter down
            self._logger.warning("Drop check failed: %s" % error)
            return

        self._act(answer, expected, robot, destination)

    # --- helpers -----------------------------------------------------------

    def _photograph(self, robot, destination):
        """Moves the camera over the destination pile and takes a picture,
        cropped exactly like the feeder photos."""
        camera = getattr(self._camera, "_camera", None)
        decorator = getattr(self._camera, "_camera_decorator", None)

        if camera is None or decorator is None or getattr(camera, "closed", False):
            return None

        offsets = self._sorter_config.get("card_offset", {})
        x = destination.coords["x"] + CAMERA_OFFSET_X + offsets.get("x", 0)
        y = destination.coords["y"] + max(offsets.get("y", 0), MIN_OFFSET_Y)

        frame = robot._frame
        frame.move(x, y, None)
        frame.waitMoveCompletes()

        stream = io.BytesIO()
        camera.capture(stream, format="jpeg")
        stream.seek(0)

        # The pile's height drives the crop, as the feeder's does
        image = decorator.processCardImage(stream, destination)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90)

        return out.getvalue()

    def _ask(self, photo, expected):
        import requests

        url = "%s/verify/%s" % (self._endpoint(), self._game)
        response = requests.post(url, files={"image": ("drop.jpeg", photo)},
                                 data={"expected": json.dumps(expected)}, timeout=20)

        return response.json()

    def _act(self, answer, expected, robot, destination):
        result = answer.get("result")
        seen = answer.get("seen") or {}
        wanted = "%s (%s #%s)" % (expected.get("title"), expected.get("set", "?"), expected.get("num", "?"))
        found = "%s (%s #%s)" % (seen.get("title"), seen.get("set"), seen.get("num"))

        if result == "refined":
            # The second photo confirmed the printing the first one couldn't:
            # correct the sorter's own record, used for its results file
            expected.update(answer.get("card") or {})
            self._logger.info("Drop check: printing confirmed as %s #%s" % (expected.get("set"), expected.get("num")))
            return

        if result == "unreadable":
            self._logger.info("Drop check: could not read the top of the pile for %s" % wanted)
            return

        if result not in DISAGREE:
            return

        self._logger.warning("Drop check: expected %s on top of the pile, the second photo shows %s" % (wanted, found))
        action = answer.get("action", "manual_bin")

        if action == "pause":
            self._logger.warning("Pausing so the pile can be checked")
            robot.global_pause.set()
        elif action == "manual_bin":
            self._to_manual_bin(robot, destination, expected, result, seen)

    def _to_manual_bin(self, robot, destination, record, reason, seen):
        """Picks the top card back up and puts it where unrecognised cards go,
        moving its record along so the results file stays right."""
        from lib.matcher.card_attributes import CardAttributes

        # Where the sorting rules send cards that weren't recognised
        attribs, _ = CardAttributes.from_camera_card({})
        bin_position = robot.matcher._handleResult(attribs, False)

        if not bin_position or bin_position is destination:
            self._logger.warning("No manual-check bin available: pausing instead")
            robot.global_pause.set()
            return

        try:
            pile_top = _PileTop(destination)
            robot._movements.grabTheCard(pile_top)
            robot._movements.releaseTheCard(pile_top, bin_position)
        except Exception as error:
            self._logger.warning("Could not move the card to the manual-check bin (%s): pausing" % error)
            robot.global_pause.set()
            return

        # The card left this pile: undo what releaseTheCard counted for it
        if destination.cards and destination.cards[-1] is record:
            destination.cards.pop()
        destination.processed = max(0, destination.processed - 1)
        destination.height = max(0.0, destination.height - (robot._game_details.get_card_height() + 0.05))

        confidence = dict(record.get("confidence") or {})
        confidence["manual_check"] = {"reason": reason, "second_photo": seen}
        bin_position.add_card(dict(record, confidence=confidence))

        self._logger.warning("Moved it to the manual-check bin (position %s)" % bin_position.index)
