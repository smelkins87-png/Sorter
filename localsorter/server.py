"""Drop-in replacement for the Magic Sorter cloud API.

Point `endpoint` in /home/pi/.magicsorter/config.json at this server (for
example http://127.0.0.1:8765) and the vendor software, touchscreen and
control panel use it instead of api2..api5.magic-sorter.com. The endpoints
and response shapes mirror what the vendor code reads:

    POST /image/<game>        card recognition   (lib/matcher/connector.py)
    GET  /sets/<game>         set list           (connector.setlist, touch UI, CP)
    GET  /popular/<format>    popular cards      (lib/matcher/rules/custom.py)
    POST /mail                end-of-run results (lib/organizer/results.py)
    GET|POST /device/config   config backup/restore
    POST /user/preferences    user preferences
    GET  /status              local health info (not used by the sorter)
"""
import csv
import io
import json
import logging
import threading
import time
import uuid
import zipfile
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, carddb
from .exporter import Exporter
from .matcher import HashIndex
from .ocr import Tesseract
from .printing import PrintingResolver

log = logging.getLogger("localsorter")

# Scryfall set types considered "regular" when the sorter asks for set_type=1
REGULAR_SET_TYPES = {"core", "expansion", "masters", "draft_innovation", "commander", "starter"}
POPULAR_FORMATS = {"standard": "standard", "modern": "modern", "pioneer": "pioneer",
                   "pauper": "pauper", "edh": "commander", "cedh": "commander"}
POPULAR_LIMIT = 1000
RELOAD_CHECK_SECONDS = 300
MAX_UPLOAD = 25 * 1024 * 1024


def parse_multipart(content_type, body):
    """Returns {field_name: bytes} for a multipart/form-data body."""
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    files = {}

    if not message.is_multipart():
        return files

    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")

        if name:
            files[name] = part.get_payload(decode=True) or b""

    return files


class App:
    """Holds the state shared by all request handler threads."""

    def __init__(self, settings, conn=None, index=None, exporter=None, resolver=None):
        self.settings = settings
        self.conn = conn if conn is not None else carddb.connect(settings.db_path)
        self.index = index if index is not None else HashIndex.from_db(self.conn, settings.include_digital)
        self.exporter = exporter if exporter is not None else Exporter(settings.export_dir, settings.save_images,
                                             settings.image_retention_days, settings.price_currency)
        self.db_lock = threading.Lock()
        tesseract = Tesseract(settings.tesseract_cmd) if settings.ocr != "off" else None
        self.resolver = resolver if resolver is not None else PrintingResolver(
            self.conn, self.db_lock, tesseract, settings.ocr, settings.region_margin, settings.border_check)
        self._data_version = carddb.get_meta(self.conn, "data_version")
        self._last_check = time.monotonic()

    def maybe_reload(self):
        """Picks up new cards/hashes written by the nightly sync process."""
        if time.monotonic() - self._last_check < RELOAD_CHECK_SECONDS:
            return

        self._last_check = time.monotonic()

        with self.db_lock:
            version = carddb.get_meta(self.conn, "data_version")

            if version == self._data_version:
                return

            self._data_version = version
            log.info("Card data changed (%s), reloading index", version)
            self.index.reload(self.conn, self.settings.include_digital)

    def card_row(self, card_id):
        with self.db_lock:
            return self.conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()

    def regular_sets(self):
        with self.db_lock:
            rows = self.conn.execute("SELECT code, set_type FROM sets").fetchall()

        return [r["code"] for r in rows if r["set_type"] in REGULAR_SET_TYPES]

    # --- endpoints -------------------------------------------------------

    def identify(self, image, params):
        """Returns (status, payload) for POST /image/<game>."""
        condition = params.get("condition", ["NM"])[0]
        lang = params.get("lang", ["en"])[0]
        # Sorter setting: 0 = no foils, 1 = all foil, 2 = detect
        foil_mode = int(params.get("foil", ["0"])[0] or 0)
        foil = 1 if foil_mode == 1 else 0

        allowed = [s.strip() for s in params.get("set[]", []) if s.strip()]

        if not allowed and params.get("set_type", ["2"])[0] == "1":
            allowed = self.regular_sets()

        self.maybe_reload()

        card_uuid = str(uuid.uuid4())
        s = self.settings
        match = self.index.identify(image, s.max_distance, s.min_margin, s.set_margin, allowed or None)
        card = None

        if match.found:
            card_id, set_detected = match.card_id, match.set_detected

            # Which printing? (set symbol, collector info)
            try:
                decision = self.resolver.resolve(match, foil_mode, lang, allowed or None)
                card_id, set_detected = decision.card_id, decision.set_detected
                foil = decision.foil if decision.foil is not None else foil
                match.printing = dict(decision.evidence, method=decision.method)
                detected_lang = decision.lang
                detected_border = decision.border
            except Exception:
                log.exception("Printing check failed; keeping the hash match")
                detected_lang = detected_border = None

            match.card_id, match.set_detected = card_id, set_detected
            row = self.card_row(card_id)

            if row is not None:
                card = carddb.to_api_card(row, foil=foil, condition=condition,
                                          lang=lang if lang != "auto" else "en", currency=s.price_currency)
                # What the card itself says beats the sorter's language setting
                if detected_lang:
                    card["lang"] = detected_lang
                # Border as seen on the scan (None: unsure); the printing's own
                # border colour is in "extra"
                card["border"] = detected_border
                card["uuid"] = card_uuid
                card["detect_set"] = 1 if set_detected else 0
                card["confidence"] = match.confidence()

        try:
            self.exporter.record(card_uuid, image, card, match, condition=condition, foil=foil)
        except OSError as error:
            log.warning("Could not write scan export: %s", error)

        if card is None:
            log.info("Not found (distance=%s margin=%s)", match.distance, match.margin)
            return 404, {"error": "Card not found", "uuid": card_uuid}

        log.info("Matched %s (%s) #%s distance=%s margin=%s via %s%s", card["title"], card["set"], card["num"],
                 match.distance, match.margin, (match.printing or {}).get("method", "hash"),
                 "" if match.set_detected else " set?")

        return 200, {"result": card}

    # Fields of the sorter's own record that a confirmed printing may update
    REFINE_FIELDS = ("set", "num", "ext_id", "rarity", "price", "price_trend", "ecommerce_id", "extra",
                     "confidence")

    def verify(self, image, expected):
        """POST /verify/<game>: the top card of the destination pile, photographed
        after the drop, against the card the sorter meant to put there.

        Results:
          match       same card (and printing, as far as both photos can tell)
          refined     same card; the first photo couldn't confirm the printing,
                      this one does: `card` holds the corrected fields
          printing_differs  same card, both photos confident about different
                      printings (logged, never pauses)
          mismatch    a different card is on top: missed drop, wrong pile...
          unreadable  nothing recognised on the pile
          skipped     the dropped card itself wasn't recognised
        """
        s = self.settings
        payload = {"action": s.verify_mismatch_action}

        if not expected.get("title"):
            payload["result"] = "skipped"
            return 200, payload

        self.maybe_reload()
        match = self.index.identify(image, s.max_distance, s.min_margin, s.set_margin)
        card = None

        if match.found:
            try:
                decision = self.resolver.resolve(match)
                match.card_id, match.set_detected = decision.card_id, decision.set_detected
            except Exception:
                log.exception("Printing check failed during verification")

            row = self.card_row(match.card_id)
            if row is not None:
                card = carddb.to_api_card(row, foil=expected.get("foil", 0), condition=expected.get("condition", "NM"),
                                          currency=s.price_currency)
                card["detect_set"] = 1 if match.set_detected else 0
                card["confidence"] = match.confidence()

        expected_sure = bool((expected.get("confidence") or {}).get("set_detected", True))

        if card is None:
            result = "unreadable"
        elif card["title"] != expected["title"]:
            result = "mismatch"
        elif (card["set"], card["num"]) == (expected.get("set"), expected.get("num")):
            result = "match"
        elif card["detect_set"] and not expected_sure:
            result = "refined"
            payload["card"] = {k: card[k] for k in self.REFINE_FIELDS if k in card}
        elif card["detect_set"] and expected_sure:
            result = "printing_differs"
        else:
            result = "match"

        payload["result"] = result
        if card is not None:
            payload["seen"] = {k: card[k] for k in ("title", "set", "num", "detect_set")}

        try:
            self.exporter.record_verification(expected, result, card, match, image)
        except OSError as error:
            log.warning("Could not write verification log: %s", error)

        level = logging.WARNING if result == "mismatch" else logging.INFO
        log.log(level, "Drop check %s: expected %s (%s #%s), saw %s", result, expected.get("title"),
                expected.get("set"), expected.get("num"),
                "%s (%s #%s)" % (card["title"], card["set"], card["num"]) if card else "nothing")

        return 200, payload

    def sets(self, params):
        orderby = params.get("orderby", ["released_at"])[0]
        orderdir = params.get("orderdir", ["desc"])[0].lower()
        column = {"name": "name", "code": "code"}.get(orderby, "released_at")
        direction = "ASC" if orderdir == "asc" else "DESC"
        where = "" if self.settings.include_digital else " WHERE digital = 0"

        # Only sets we actually have cards for
        with self.db_lock:
            rows = self.conn.execute(
                "SELECT code, name, released_at, set_type, card_count FROM sets%s%s ORDER BY %s %s, code"
                % (where, (" AND" if where else " WHERE") + " code IN (SELECT DISTINCT set_code FROM cards)",
                   column, direction)).fetchall()

        return 200, [dict(r) for r in rows]

    def popular(self, fmt):
        legality = POPULAR_FORMATS.get(fmt)

        if not legality:
            return 404, {"error": "Unknown format"}

        # Approximation of the vendor's list: the most played cards by EDHREC
        # rank that are legal in the format, every printing included
        with self.db_lock:
            rows = self.conn.execute(
                "SELECT oracle_id, MIN(edhrec_rank) AS rank FROM cards "
                "WHERE edhrec_rank IS NOT NULL AND json_extract(legalities, ?) = 'legal' "
                "GROUP BY oracle_id ORDER BY rank LIMIT ?",
                ("$." + legality, POPULAR_LIMIT)).fetchall()
            oracle_ids = [r["oracle_id"] for r in rows]
            data = []

            for start in range(0, len(oracle_ids), 500):
                chunk = oracle_ids[start:start + 500]
                data.extend(dict(r) for r in self.conn.execute(
                    "SELECT set_code AS code, name AS title, collector_number AS num FROM cards "
                    "WHERE oracle_id IN (%s)" % ",".join("?" * len(chunk)), chunk))

        return 200, {"data": data}

    def mail(self, upload):
        """End-of-run results. The sorter normally has them emailed; we keep
        them in the export folder and send back the summary CSV, which the
        sorter writes to ~/public/results_processed_*.csv."""
        summary = b""

        try:
            with zipfile.ZipFile(io.BytesIO(upload)) as archive:
                for name in archive.namelist():
                    data = archive.read(name)
                    self.exporter.store_session(name, data)

                    if not name.startswith("results_details") and name.endswith(".csv"):
                        summary = data
        except zipfile.BadZipFile:
            return 400, {"error": "Invalid results archive"}

        return 200, summary.decode("utf-8", "replace")

    def get_kv(self, key):
        with self.db_lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()

        return json.loads(row["value"]) if row else None

    def set_kv(self, key, value):
        with self.db_lock, self.conn:
            self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def status(self):
        with self.db_lock:
            cards = self.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            prices_updated = carddb.get_meta(self.conn, "prices_updated")
            bulk_updated_at = carddb.get_meta(self.conn, "bulk_updated_at")

        return 200, {
            "version": __version__,
            "cards": cards,
            "indexed_images": len(self.index),
            "prices_updated": prices_updated,
            "bulk_updated_at": bulk_updated_at,
        }


class Handler(BaseHTTPRequestHandler):
    app = None  # set by make_server
    server_version = "localsorter/" + __version__

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status, payload):
        if isinstance(payload, str):
            body = payload.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        else:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        # The touchscreen and control panel fetch /sets and /device/config
        # from their renderer with an X-Auth header, which needs CORS
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Auth, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)

        if length > MAX_UPLOAD:
            raise ValueError("Upload too large")

        return self.rfile.read(length) if length else b""

    def _route(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        params = parse_qs(url.query)

        return parts, params

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parts, params = self._route()

        try:
            if len(parts) == 2 and parts[0] == "sets":
                if parts[1] != "mtg":
                    return self._send(200, [])
                return self._send(*self.app.sets(params))

            if len(parts) == 2 and parts[0] == "popular":
                return self._send(*self.app.popular(parts[1]))

            if parts == ["device", "config"]:
                config = self.app.get_kv("device_config")

                if config is None:
                    return self._send(404, {"error": "No configuration stored"})
                return self._send(200, {"result": config})

            if parts == ["status"] or not parts:
                return self._send(*self.app.status())
        except Exception as error:
            log.exception("Request failed")
            return self._send(500, {"error": str(error)})

        self._send(404, {"error": "Not found"})

    def do_POST(self):
        parts, params = self._route()

        try:
            body = self._body()

            if len(parts) == 2 and parts[0] == "image":
                if parts[1] != "mtg":
                    return self._send(404, {"error": "Only Magic: The Gathering is supported"})

                files = parse_multipart(self.headers.get("Content-Type", ""), body)

                if not files.get("image"):
                    return self._send(400, {"error": "Missing image"})

                return self._send(*self.app.identify(files["image"], params))

            if len(parts) == 2 and parts[0] == "verify":
                if parts[1] != "mtg":
                    return self._send(200, {"result": "skipped"})

                files = parse_multipart(self.headers.get("Content-Type", ""), body)

                if not files.get("image"):
                    return self._send(400, {"error": "Missing image"})

                expected = json.loads(files.get("expected") or b"{}")
                if not isinstance(expected, dict):
                    return self._send(400, {"error": "expected must be an object"})

                return self._send(*self.app.verify(files["image"], expected))

            if parts == ["mail"]:
                files = parse_multipart(self.headers.get("Content-Type", ""), body)
                return self._send(*self.app.mail(files.get("results", b"")))

            if parts == ["device", "config"]:
                config = json.loads(body or b"{}")
                config = config.get("config") if isinstance(config, dict) else None

                if not isinstance(config, dict):
                    return self._send(400, {"error": "Expected {\"config\": {...}}"})

                # Never keep the vendor credentials around
                for key in ("secret", "device_id", "download_id"):
                    config.pop(key, None)

                self.app.set_kv("device_config", config)
                return self._send(200, {"result": "ok"})

            if parts == ["user", "preferences"]:
                self.app.set_kv("user_preferences", json.loads(body or b"{}"))
                return self._send(200, {"result": "ok"})
        except ValueError as error:
            return self._send(400, {"error": str(error)})
        except Exception as error:
            log.exception("Request failed")
            return self._send(500, {"error": str(error)})

        self._send(404, {"error": "Not found"})


def make_server(app, host, port):
    handler = type("BoundHandler", (Handler,), {"app": app})

    return ThreadingHTTPServer((host, port), handler)
