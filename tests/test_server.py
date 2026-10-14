"""HTTP-level tests for localsorter.server against an in-memory card database.

The server runs on a real socket (127.0.0.1, ephemeral port) in a daemon
thread; requests go through urllib so no third-party HTTP client is needed.
"""
import csv
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest

from localsorter import carddb
from localsorter.exporter import COLUMNS
from localsorter.server import App, make_server, parse_multipart
from localsorter.settings import Settings

from tests import synthetic
from tests.helpers import build_db, scryfall_card

CARD_COUNT = 20


# --- helpers ---------------------------------------------------------------

def encode_multipart(fields=(), files=(), boundary=None):
    """Builds a multipart/form-data body the way python-requests does.

    files: (field, filename, bytes, part_content_type_or_None). A None content
    type omits the part's Content-Type header, as requests does for a plain
    file object.
    """
    boundary = boundary or uuid.uuid4().hex
    marker = ("--" + boundary).encode()
    chunks = []

    for name, value in fields:
        chunks.append(marker + b"\r\n"
                      + ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
                      + value + b"\r\n")

    for name, filename, data, part_type in files:
        head = ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename))

        if part_type:
            head += "Content-Type: %s\r\n" % part_type

        chunks.append(marker + b"\r\n" + head.encode() + b"\r\n" + data + b"\r\n")

    chunks.append(marker + b"--\r\n")

    return "multipart/form-data; boundary=" + boundary, b"".join(chunks)


def make_zip(members):
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)

    return buf.getvalue()


def call(port, method, path, body=None, headers=None):
    """Returns (status, lowercased headers dict, body bytes). Never raises on HTTP errors."""
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                     method=method, headers=headers or {})

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def call_json(port, method, path, payload=None):
    body = None
    headers = {}

    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    status, resp_headers, raw = call(port, method, path, body, headers)

    return status, resp_headers, json.loads(raw) if raw else None


def post_image(port, path, image_bytes, part_type=None):
    content_type, body = encode_multipart(files=[("image", "temp.jpeg", image_bytes, part_type)])

    return call(port, "POST", path, body, {"Content-Type": content_type})


def post_mail(port, archive_bytes, query="format=1&game=mtg"):
    content_type, body = encode_multipart(files=[("results", "results.zip", archive_bytes, None)])

    return call(port, "POST", "/mail?" + query, body, {"Content-Type": content_type})


SCAN_QUERY = "/image/mtg?set_type=2&condition=NM&lang=en&_=123"


@pytest.fixture
def env(tmp_path):
    conn = carddb.connect(":memory:")
    cards = [scryfall_card(i) for i in range(1, CARD_COUNT + 1)]
    images = {card["id"]: synthetic.make_card(i) for i, card in enumerate(cards, start=1)}
    build_db(conn, cards, images)

    settings = Settings(data_dir=str(tmp_path), export_dir=str(tmp_path / "scans"))
    app = App(settings, conn=conn)
    srv = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    try:
        yield SimpleNamespace(port=srv.server_address[1], app=app, export_dir=settings.export_dir,
                              tmp_path=tmp_path)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


# --- recognition -------------------------------------------------------------

def test_identify_known_card_returns_api_card(env):
    photo = synthetic.photo(synthetic.make_card(3), seed=3)
    status, headers, raw = post_image(env.port, SCAN_QUERY, photo)
    payload = json.loads(raw)

    assert status == 200
    assert headers["content-type"].startswith("application/json")

    result = payload["result"]
    assert result["title"] == "Card 3"
    assert result["set"] == "TST"
    assert result["num"] == "3"
    assert result["condition"] == "NM"
    assert result["lang"] == "en"
    assert result["foil"] == 0
    assert isinstance(result["uuid"], str) and len(result["uuid"]) == 36
    assert isinstance(result["confidence"], dict)
    assert {"distance", "margin", "set_detected", "candidates"} <= set(result["confidence"])

    extra = result["extra"]
    assert isinstance(extra, str)
    assert json.loads(extra)["rarity"] == "common"
    assert json.loads(extra)["cmc"] == 2
    assert json.loads(extra)["colors"] == ["red"]


def test_identify_unknown_card_returns_404_with_error(env):
    photo = synthetic.photo(synthetic.make_card(9999), seed=9999)
    status, _, raw = post_image(env.port, SCAN_QUERY, photo)
    payload = json.loads(raw)

    assert status == 404
    assert payload["error"] == "Card not found"
    assert payload["uuid"]


def test_identify_non_mtg_game_returns_404(env):
    photo = synthetic.photo(synthetic.make_card(3), seed=3)
    status, _, raw = post_image(env.port, "/image/pokemon", photo)

    assert status == 404
    assert "error" in json.loads(raw)


def test_identify_missing_image_field_returns_400(env):
    content_type, body = encode_multipart(fields=[("other", b"x")])
    status, _, raw = call(env.port, "POST", SCAN_QUERY, body, {"Content-Type": content_type})

    assert status == 400
    assert json.loads(raw)["error"] == "Missing image"


def test_set_filter_without_cards_gives_not_found(env):
    photo = synthetic.photo(synthetic.make_card(3), seed=3)

    # Positive control: the same scan restricted to the one set we do have is found
    status, _, _ = post_image(env.port, SCAN_QUERY + "&" + urllib.parse.urlencode({"set[]": "TST"}), photo)
    assert status == 200

    status, _, raw = post_image(env.port, SCAN_QUERY + "&" + urllib.parse.urlencode({"set[]": "XYZ"}), photo)
    assert status == 404
    assert "error" in json.loads(raw)


def test_identify_accepts_part_with_content_type(env):
    photo = synthetic.photo(synthetic.make_card(5), seed=5)
    status, _, raw = post_image(env.port, SCAN_QUERY, photo, part_type="application/octet-stream")

    assert status == 200
    assert json.loads(raw)["result"]["title"] == "Card 5"


# --- scan export -------------------------------------------------------------

def test_scan_is_logged_to_daily_csv_with_image(env):
    found = json.loads(post_image(env.port, SCAN_QUERY, synthetic.photo(synthetic.make_card(3), seed=3))[2])
    missing = json.loads(post_image(env.port, SCAN_QUERY, synthetic.photo(synthetic.make_card(9999),
                                                                          seed=9999))[2])

    day_dir = env.export_dir + "/" + datetime.now().strftime("%Y-%m-%d")
    csv_path = day_dir + "/scans.csv"

    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    assert rows[0] == COLUMNS
    assert len(rows) == 3, "one header row plus one row per scan"

    records = [dict(zip(COLUMNS, row)) for row in rows[1:]]
    by_uuid = {r["uuid"]: r for r in records}

    assert by_uuid[found["result"]["uuid"]]["status"] == "found"
    assert by_uuid[found["result"]["uuid"]]["title"] == "Card 3"
    assert by_uuid[missing["uuid"]]["status"] == "not_found"

    for record in records:
        assert record["image"], "image column should point at the saved photo"
        image_path = day_dir + "/" + record["image"]
        with open(image_path, "rb") as handle:
            assert handle.read(2) == b"\xff\xd8", "saved image should be a JPEG"


# --- reference data -----------------------------------------------------------

def test_sets_mtg_lists_sets_with_cards(env):
    status, _, sets = call_json(env.port, "GET", "/sets/mtg")

    assert status == 200
    assert isinstance(sets, list) and sets
    assert all({"code", "name"} <= set(s) for s in sets)
    assert [s["code"] for s in sets] == ["TST"]


def test_sets_for_other_game_is_empty(env):
    status, _, sets = call_json(env.port, "GET", "/sets/yugioh")

    assert status == 200
    assert sets == []


def test_popular_edh_lists_legal_cards(env):
    status, _, payload = call_json(env.port, "GET", "/popular/edh")

    assert status == 200
    assert len(payload["data"]) == CARD_COUNT
    for entry in payload["data"]:
        assert {"code", "title", "num"} <= set(entry)
    assert {e["title"] for e in payload["data"]} == {"Card %d" % i for i in range(1, CARD_COUNT + 1)}


def test_popular_format_without_legal_cards_is_empty(env):
    status, _, payload = call_json(env.port, "GET", "/popular/modern")

    assert status == 200
    assert payload == {"data": []}


def test_popular_unknown_format_returns_404(env):
    status, _, payload = call_json(env.port, "GET", "/popular/bogus")

    assert status == 404
    assert "error" in payload


# --- device config and preferences ----------------------------------------------

def test_device_config_roundtrip_strips_credentials(env):
    status, _, payload = call_json(env.port, "GET", "/device/config")
    assert status == 404
    assert "error" in payload

    status, _, payload = call_json(env.port, "POST", "/device/config",
                                   {"config": {"speed": 2, "secret": "s", "device_id": "d"}})
    assert status == 200
    assert payload == {"result": "ok"}

    status, _, payload = call_json(env.port, "GET", "/device/config")
    assert status == 200
    assert payload == {"result": {"speed": 2}}


def test_user_preferences_post_is_accepted_and_stored(env):
    prefs = {"language": "en", "sound": False}
    status, _, payload = call_json(env.port, "POST", "/user/preferences", prefs)

    assert status == 200
    assert payload == {"result": "ok"}
    assert env.app.get_kv("user_preferences") == prefs


# --- end-of-run results --------------------------------------------------------------

def test_mail_returns_summary_and_stores_both_files(env):
    summary = b"title,count\nCard 3,1\n"
    details = b"title,location\nCard 3,A1\n"
    archive = make_zip([("results_details_42.csv", details), ("results_42.csv", summary)])

    status, headers, raw = post_mail(env.port, archive)

    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    assert raw.decode("utf-8") == summary.decode("utf-8")

    sessions = env.tmp_path / "scans" / "sessions"
    assert (sessions / "results_42.csv").read_bytes() == summary
    assert (sessions / "results_details_42.csv").read_bytes() == details


def test_mail_rejects_non_zip_upload(env):
    status, _, raw = post_mail(env.port, b"this is not a zip file")

    assert status == 400
    assert json.loads(raw)["error"] == "Invalid results archive"


def test_mail_keeps_archive_members_inside_sessions_dir(env):
    archive = make_zip([("../../escape.csv", b"a,b\n1,2\n"), ("results_7.csv", b"x\n")])

    status, _, _ = post_mail(env.port, archive)

    assert status == 200
    assert (env.tmp_path / "scans" / "sessions" / "escape.csv").exists()
    assert not (env.tmp_path / "escape.csv").exists()


# --- CORS -----------------------------------------------------------------------------

def test_options_preflight_allows_x_auth(env):
    status, headers, raw = call(env.port, "OPTIONS", "/sets/mtg")

    assert status == 204
    assert raw == b""
    assert headers["access-control-allow-origin"] == "*"
    assert "X-Auth" in headers["access-control-allow-headers"]


def test_get_responses_carry_cors_origin(env):
    for path in ("/sets/mtg", "/status", "/device/config", "/popular/edh"):
        _, headers, _ = call(env.port, "GET", path)
        assert headers.get("access-control-allow-origin") == "*", path


# --- status ---------------------------------------------------------------------------

def test_status_reports_card_and_index_counts(env):
    status, _, payload = call_json(env.port, "GET", "/status")

    assert status == 200
    assert payload["cards"] == CARD_COUNT
    assert payload["indexed_images"] == CARD_COUNT
    assert "version" in payload


# --- multipart parser -------------------------------------------------------------------

def test_parse_multipart_extracts_named_parts_bytes_exactly():
    jpeg = synthetic.jpeg(synthetic.make_card(1))
    content_type, body = encode_multipart(
        fields=[("lang", b"en")],
        files=[("image", "temp.jpeg", jpeg, "application/octet-stream"),
               ("results", "r.zip", b"PK\x03\x04binary\r\n\x00\xff", None)])

    files = parse_multipart(content_type, body)

    assert files["lang"] == b"en"
    assert files["image"] == jpeg
    assert files["results"] == b"PK\x03\x04binary\r\n\x00\xff"


def test_parse_multipart_non_multipart_content_type_returns_empty():
    assert parse_multipart("application/json", b'{"a": 1}') == {}
    assert parse_multipart("", b"") == {}


def test_sendlog_keeps_logs_locally(env):
    """The sorter uploads its logs by itself after a crash: they must land on
    the share, not anywhere else."""
    import urllib.request as _request

    body = (b"--B\r\nContent-Disposition: form-data; name=\"log\"; filename=\"magicsorter.zip\"\r\n\r\n"
            b"PK-fake-zip\r\n--B--\r\n")
    request = _request.Request("http://127.0.0.1:%d/sendlog" % env.port, data=body,
                               headers={"Content-Type": "multipart/form-data; boundary=B"})
    assert json.load(_request.urlopen(request)) == {"result": "ok"}

    saved = list((env.tmp_path / "scans" / "logs").glob("sorter-logs_*.zip"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"PK-fake-zip"


def test_negative_content_length_is_rejected(env):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=5)
    conn.putrequest("POST", "/user/preferences")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    assert conn.getresponse().status == 400
