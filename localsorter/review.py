"""Review page: check what the sorter recognised, confirm or correct it.

A small web app on its own port, reachable from the local network (the
sorter's API stays on 127.0.0.1). It reads the daily scan logs written by
exporter.py and stores the verdicts in the card database:

* confirmed / corrected scans become <day>/labels.csv, the input of
  `python3 -m localsorter eval` (tuning on your own cards)
* /api/export gives the day's collection with the corrections applied

Everything is served locally: no web fonts, CDNs or card images from the
internet, so reviewing doesn't tell anyone what's in the collection.
"""
import base64
import csv
import hmac
import io
import json
import logging
import mimetypes
import os
import ipaddress
import re
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

from . import __version__
from .stopcard import StopCards

log = logging.getLogger("localsorter")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_static")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
THUMB_SIZE = 420
# Drop-check results that put a card in the manual-check bin
DISAGREE = ("mismatch", "printing_differs")

LABELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    uuid       TEXT PRIMARY KEY,
    day        TEXT NOT NULL,
    card_id    TEXT,               -- NULL: not a card / unidentifiable
    status     TEXT NOT NULL,      -- confirmed | corrected | not_a_card | stop_card
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS labels_day ON labels (day);
"""


def _stamp(path):
    try:
        info = os.stat(path)
    except OSError:
        return None

    return info.st_mtime_ns, info.st_size


def _host_name(netloc):
    """Host header (or URL netloc) without the port, lower case."""
    netloc = netloc.strip().lower()

    if netloc.startswith("["):
        return netloc[1:].partition("]")[0]

    return netloc.rpartition(":")[0] if netloc.count(":") == 1 else netloc


def _local_name(host):
    """An IP address, localhost or this machine's own name (any domain after it)."""
    if not host:
        return False

    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    me = socket.gethostname().lower()

    return host == "localhost" or host == me or host.startswith(me + ".")


def _read_csv(path):
    if not os.path.exists(path):
        return []

    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


class Review:
    def __init__(self, settings, conn, db_lock, status=None):
        self.settings = settings
        self.conn = conn
        self.db_lock = db_lock
        # Callable giving the sorter's state (paused for review, re-sorting)
        self.status = status or (lambda: {"waiting_review": None, "resorting": None})
        self._csv_lock = threading.Lock()
        self.stop_cards = StopCards(conn, db_lock)
        # Per-day counts for the day list, until the day's files or labels change
        self._days_cache = {}
        self._labels_version = 0

        with self.db_lock, self.conn:
            self.conn.executescript(LABELS_SCHEMA)

    # --- reading ---------------------------------------------------------

    def _day_dir(self, day):
        if not DAY_RE.match(day or ""):
            raise ValueError("Invalid day")

        return os.path.join(self.settings.export_dir, day)

    def days(self):
        try:
            entries = sorted((e for e in os.listdir(self.settings.export_dir) if DAY_RE.match(e)), reverse=True)
        except OSError:
            entries = []

        result = []

        for day in entries:
            key = (self._labels_version,) + tuple(_stamp(os.path.join(self.settings.export_dir, day, name))
                                                  for name in ("scans.csv", "verify.csv"))
            cached = self._days_cache.get(day)

            if cached is None or cached[0] != key:
                scans = self.scans(day)
                cached = (key, {"day": day, "scans": len(scans),
                                "needs_check": sum(1 for s in scans if s["needs_check"] and not s["label"])})
                self._days_cache[day] = cached

            result.append(cached[1])

        return result

    def _labels(self, day):
        with self.db_lock:
            rows = self.conn.execute("SELECT uuid, card_id, status FROM labels WHERE day = ?", (day,)).fetchall()

        return {r["uuid"]: {"card_id": r["card_id"], "status": r["status"]} for r in rows}

    def _cards(self, ids):
        ids = [i for i in set(ids) if i]

        if not ids:
            return {}

        with self.db_lock:
            return {r["id"]: r for r in self.conn.execute(
                "SELECT id, name, set_code, set_name, collector_number, rarity, usd, usd_foil, eur, "
                "frame, border_color, released_at FROM cards WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}

    def scans(self, day):
        day_dir = self._day_dir(day)
        rows = _read_csv(os.path.join(day_dir, "scans.csv"))
        checks = {r["uuid"]: r for r in _read_csv(os.path.join(day_dir, "verify.csv"))}
        labels = self._labels(day)
        cards = self._cards([l["card_id"] for l in labels.values()])
        result = []

        for row in rows:
            uuid = row.get("uuid", "")
            check = checks.get(uuid)
            reasons = []

            if row.get("status") != "found":
                reasons.append("not found")
            elif row.get("set_detected") == "0":
                reasons.append("set not confirmed")

            if check and check.get("result") in DISAGREE:
                reasons.append("drop check: %s" % check["result"].replace("_", " "))

            label = labels.get(uuid)
            if label:
                fixed = cards.get(label["card_id"])
                label = dict(label, title=fixed["name"] if fixed else "",
                             set=fixed["set_code"] if fixed else "", num=fixed["collector_number"] if fixed else "")

            result.append({
                "uuid": uuid,
                "time": (row.get("timestamp") or "")[11:19],
                "status": row.get("status"),
                "title": row.get("title"),
                "set": row.get("set"),
                "set_name": row.get("set_name"),
                "num": row.get("collector_number"),
                "rarity": row.get("rarity"),
                "price": row.get("price"),
                "foil": row.get("foil"),
                "border": row.get("border"),
                "scryfall_id": row.get("scryfall_id"),
                "method": row.get("printing_method"),
                # Pulled back out of the manual-check bin and sorted with a review's verdict
                "resorted": row.get("printing_method") == "review",
                "has_image": bool(row.get("image")),
                "drop_check": check.get("result") if check else None,
                "drop_seen": ("%s (%s #%s)" % (check["seen_title"], check["seen_set"], check["seen_number"])
                              if check and check.get("seen_title") else None),
                "needs_check": bool(reasons),
                "reasons": reasons,
                "label": label,
            })

        return result

    def stats(self, day, scans=None):
        scans = self.scans(day) if scans is None else scans
        found = [s for s in scans if s["status"] == "found"]
        labelled = [s for s in scans if s["label"]]
        value = 0.0

        for s in self._final(scans):
            try:
                value += float(s["price"] or 0)
            except ValueError:
                pass

        return {
            "scans": len(scans),
            "found": len(found),
            "needs_check": sum(1 for s in scans if s["needs_check"] and not s["label"]),
            "confirmed": sum(1 for s in labelled if s["label"]["status"] == "confirmed"),
            "corrected": sum(1 for s in labelled if s["label"]["status"] in ("corrected", "not_a_card")),
            "value": round(value, 2),
            "currency": self.settings.price_currency.upper(),
        }

    def printings(self, card_id):
        """Every paper printing of the same card, newest first."""
        with self.db_lock:
            row = self.conn.execute("SELECT oracle_id FROM cards WHERE id = ?", (card_id,)).fetchone()

            if row is None:
                return []

            rows = self.conn.execute(
                "SELECT c.id, c.name, c.set_code, c.set_name, c.collector_number, c.rarity, c.frame, "
                "c.border_color, c.released_at, c.usd, c.lang, "
                "EXISTS(SELECT 1 FROM regions r WHERE r.card_id = c.id) AS has_closeup "
                "FROM cards c WHERE c.oracle_id = ? AND c.digital = 0 "
                "ORDER BY c.released_at DESC, c.set_code, c.collector_number", (row["oracle_id"],)).fetchall()

        return [dict(r) for r in rows]

    def search(self, query):
        query = (query or "").strip()

        if len(query) < 2:
            return []

        with self.db_lock:
            rows = self.conn.execute(
                "SELECT name, MIN(id) AS id, COUNT(*) AS printings FROM cards WHERE digital = 0 AND name LIKE ? "
                "GROUP BY oracle_id ORDER BY (name LIKE ?) DESC, name LIMIT 20",
                ("%" + query + "%", query + "%")).fetchall()

        return [dict(r) for r in rows]

    def closeup(self, card_id):
        with self.db_lock:
            row = self.conn.execute("SELECT symbol FROM regions WHERE card_id = ? ORDER BY face LIMIT 1",
                                    (card_id,)).fetchone()

        return row["symbol"] if row else None

    def image(self, day, uuid, thumb=False):
        if not UUID_RE.match(uuid or ""):
            raise ValueError("Invalid scan id")

        path = os.path.join(self._day_dir(day), "images", uuid + ".jpg")

        if not os.path.exists(path):
            return None

        if not thumb:
            with open(path, "rb") as handle:
                return handle.read()

        image = Image.open(path)
        image.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))
        image = image.convert("RGB")
        image.thumbnail((THUMB_SIZE, THUMB_SIZE))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=82)

        return out.getvalue()

    # --- writing ---------------------------------------------------------

    def label(self, day, uuid, status, card_id=None):
        self._day_dir(day)

        if not UUID_RE.match(uuid or ""):
            raise ValueError("Invalid scan id")

        if status not in ("confirmed", "corrected", "not_a_card", "stop_card", "clear"):
            raise ValueError("Invalid status")

        if status == "stop_card":
            # Learnt from the scan's photo (outside the label's transaction:
            # it takes the database lock itself)
            photo = self.image(day, uuid)
            if photo is None:
                raise ValueError("No photo kept for this scan")
            self.stop_cards.add(uuid, day, photo)
            card_id = None

        with self.db_lock, self.conn:
            if status == "clear":
                self.conn.execute("DELETE FROM labels WHERE uuid = ?", (uuid,))
            else:
                if card_id is not None and not self.conn.execute(
                        "SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone():
                    raise ValueError("Unknown card")

                self.conn.execute(
                    "INSERT INTO labels (uuid, day, card_id, status, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET card_id=excluded.card_id, status=excluded.status, "
                    "updated_at=excluded.updated_at",
                    (uuid, day, card_id, status, datetime.now().isoformat(timespec="seconds")))

        if status != "stop_card":
            self.stop_cards.remove(uuid)

        self._labels_version += 1
        self._write_labels_csv(day)

    def _write_labels_csv(self, day):
        """<day>/labels.csv: what `localsorter eval` reads."""
        with self._csv_lock:
            self._write_labels_csv_locked(day)

    def _write_labels_csv_locked(self, day):
        scans = {s["uuid"]: s for s in self.scans(day)}
        labels = self._labels(day)
        cards = self._cards([l["card_id"] for l in labels.values()])
        path = os.path.join(self._day_dir(day), "labels.csv")

        with open(path + ".tmp", "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["image", "set", "collector_number", "scryfall_id", "title", "status"])

            for uuid, label in labels.items():
                card = cards.get(label["card_id"])
                scan = scans.get(uuid)

                if card is None or scan is None or not scan["has_image"]:
                    continue

                writer.writerow([os.path.join("images", uuid + ".jpg"), card["set_code"], card["collector_number"],
                                 card["id"], card["name"], label["status"]])

        os.replace(path + ".tmp", path)

    def _final(self, scans):
        """Scans with corrections applied: the day's collection."""
        cards = self._cards([s["label"]["card_id"] for s in scans if s["label"]])

        for s in scans:
            label = s["label"]

            # The card's reviewed scan already counts it
            if s.get("resorted"):
                continue

            if label and label["status"] in ("not_a_card", "stop_card"):
                continue

            if label and label["card_id"] in cards:
                card = cards[label["card_id"]]
                foil = s.get("foil") in ("1", 1, True)
                price = card["usd_foil"] if foil and card["usd_foil"] is not None else card["usd"]
                if self.settings.price_currency == "eur":
                    price = card["eur"]

                yield dict(s, title=card["name"], set=card["set_code"], set_name=card["set_name"],
                           num=card["collector_number"], rarity=card["rarity"], scryfall_id=card["id"],
                           price="" if price is None else "%.2f" % price, reviewed=label["status"])
            elif s["status"] == "found":
                yield dict(s, reviewed="")

    def export(self, day):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["title", "set", "set_name", "collector_number", "rarity", "foil", "price", "currency",
                         "scryfall_id", "reviewed", "scan"])

        for s in self._final(self.scans(day)):
            writer.writerow([s["title"], s["set"], s["set_name"], s["num"], s["rarity"], s["foil"], s["price"],
                             self.settings.price_currency.upper(), s["scryfall_id"], s["reviewed"], s["uuid"]])

        return out.getvalue()


class ReviewHandler(BaseHTTPRequestHandler):
    review = None    # set by make_review_server
    password = ""
    server_version = "localsorter-review/" + __version__

    def log_message(self, fmt, *args):
        log.debug("review %s - %s", self.address_string(), fmt % args)

    def _same_site(self):
        """Refuses requests a web page from elsewhere could make through the
        visitor's browser: a Host that isn't this machine (DNS rebinding)
        and cross-site writes."""
        host = _host_name(self.headers.get("Host", ""))

        if not _local_name(host):
            self._send(403, {"error": "Use the sorter's name or IP address"})
            return False

        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin and _host_name(urlparse(origin).netloc) != host:
                self._send(403, {"error": "Cross-site request"})
                return False

            # A form can't send JSON: browsers have to ask first, and we never agree
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self._send(415, {"error": "Expected application/json"})
                return False

        return True

    def _authorised(self):
        if not self._same_site():
            return False

        if not self.password:
            return True

        header = self.headers.get("Authorization", "")

        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                given = ""

            if hmac.compare_digest(given.encode("utf-8"), self.password.encode("utf-8")):
                return True

        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Sorter review"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, status, body, content_type="application/json", cache=False):
        if not isinstance(body, (bytes, bytearray)):
            body = (json.dumps(body) if content_type == "application/json" else body).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorised():
            return

        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        review = self.review

        try:
            if not parts:
                return self._static("index.html")
            if parts[0] == "static" and len(parts) == 2:
                return self._static(parts[1])
            if parts == ["api", "status"]:
                return self._send(200, review.status())
            if parts == ["api", "days"]:
                return self._send(200, review.days())
            if parts == ["api", "scans"]:
                scans = review.scans(params.get("day"))
                return self._send(200, {"scans": scans, "stats": review.stats(params.get("day"), scans)})
            if parts == ["api", "printings"]:
                return self._send(200, review.printings(params.get("card_id")))
            if parts == ["api", "search"]:
                return self._send(200, review.search(params.get("q")))
            if parts[:2] == ["api", "image"] and len(parts) == 4:
                data = review.image(parts[2], parts[3], thumb=params.get("size") == "thumb")
                return self._send(200, data, "image/jpeg", cache=True) if data else self._send(404, {"error": "No image"})
            if parts[:2] == ["api", "closeup"] and len(parts) == 3:
                data = review.closeup(parts[2])
                return self._send(200, data, "image/jpeg", cache=True) if data else self._send(404, {"error": "No close-up"})
            if parts == ["api", "export"]:
                day = params.get("day")
                body = review.export(day).encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="collection_%s.csv"' % day)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
        except ValueError as error:
            return self._send(400, {"error": str(error)})
        except Exception:
            log.exception("Review request failed")
            return self._send(500, {"error": "Internal error"})

        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authorised():
            return

        try:
            if urlparse(self.path).path != "/api/label":
                return self._send(404, {"error": "Not found"})

            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 10000:
                raise ValueError("Invalid request size")

            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Expected an object")

            self.review.label(data.get("day"), data.get("uuid"), data.get("status"), data.get("card_id"))
            return self._send(200, {"result": "ok"})
        except ValueError as error:
            return self._send(400, {"error": str(error)})
        except Exception:
            log.exception("Review request failed")
            return self._send(500, {"error": "Internal error"})

    def _static(self, name):
        if name != os.path.basename(name) or name.startswith("."):
            return self._send(404, {"error": "Not found"})

        path = os.path.join(STATIC_DIR, name)

        if not os.path.isfile(path):
            return self._send(404, {"error": "Not found"})

        with open(path, "rb") as handle:
            body = handle.read()

        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"

        self._send(200, body, content_type, cache=name.endswith(".jpg"))


def make_review_server(review, host, port, password=""):
    handler = type("BoundReviewHandler", (ReviewHandler,), {"review": review, "password": password or ""})

    return ThreadingHTTPServer((host, port), handler)


def start_review_server(app, settings):
    """Runs the review page next to the sorter API (a daemon thread)."""
    review = Review(settings, app.conn, app.db_lock, status=app.sorter_status)
    server = make_review_server(review, settings.review_host, settings.review_port, settings.review_password)
    thread = threading.Thread(target=server.serve_forever, name="review", daemon=True)
    thread.start()
    log.info("Review page on http://%s:%d%s", settings.review_host, settings.review_port,
             " (password protected)" if settings.review_password else "")

    return server
