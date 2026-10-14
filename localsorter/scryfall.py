"""Minimal Scryfall client (stdlib only).

Only public, anonymous endpoints are used. Nothing about the scanned cards is
ever sent: the bulk file and the image downloads are identical for everyone.
See https://scryfall.com/docs/api for the usage guidelines this follows.
"""
import codecs
import glob
import gzip
import json
import os
import re
import urllib.parse
import time
import urllib.request
import zlib

from . import __version__

API = "https://api.scryfall.com"
USER_AGENT = "localsorter/%s (self-hosted card sorter)" % __version__


def _request(url, accept="application/json"):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": "gzip",
    })

    return urllib.request.urlopen(req, timeout=60)


def _decoded_stream(response):
    """Wraps an HTTP response so reads return decompressed bytes."""
    if response.headers.get("Content-Encoding", "").lower() == "gzip":
        return gzip.GzipFile(fileobj=response)

    return response


def get_json(url):
    with _request(url) as response:
        return json.load(_decoded_stream(response))


def bulk_info(kind="default_cards"):
    """Returns the bulk-data descriptor (download_uri, updated_at, size...)."""
    data = get_json(API + "/bulk-data/" + kind)

    if data.get("object") != "bulk_data":
        raise RuntimeError("Unexpected bulk-data response: %r" % data.get("object"))

    return data


def fetch_sets():
    """All Scryfall sets (follows pagination)."""
    url = API + "/sets"
    sets = []

    while url:
        page = get_json(url)
        sets.extend(page.get("data", []))
        url = page.get("next_page") if page.get("has_more") else None

        if url:
            time.sleep(0.1)

    return sets


def iter_json_array(stream, chunk_size=1 << 20):
    """Yields the elements of a top-level JSON array without loading it whole.

    Scryfall's default_cards file is ~500 MB, too big to json.load() on a Pi.
    Works for any formatting; only one element (plus a read chunk) is held in
    memory at a time.
    """
    decoder = json.JSONDecoder()
    raw = _text_reader(stream)
    buf = ""
    pos = 0
    started = False
    eof = False

    while True:
        # Skip whitespace and separators
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1

            if pos < len(buf) or eof:
                break

            buf, pos, eof = _refill(raw, buf, pos, chunk_size)

        if pos >= len(buf):
            # An empty download must not look like "zero cards"
            raise ValueError("Truncated JSON array" if started else "Empty JSON stream")

        if not started:
            if buf[pos] != "[":
                raise ValueError("Expected a JSON array")

            started = True
            pos += 1
            continue

        if buf[pos] == "]":
            return

        try:
            obj, end = decoder.raw_decode(buf, pos)
        except json.JSONDecodeError:
            if eof:
                raise
            buf, pos, eof = _refill(raw, buf, pos, chunk_size)
            continue

        # raw_decode can succeed on a prefix of a number ("3" of "3.25") at
        # the buffer edge: only trust it once a delimiter follows
        if not eof and (end == len(buf) or buf[end] not in " \t\r\n,]"):
            buf, pos, eof = _refill(raw, buf, pos, chunk_size)
            continue

        yield obj
        pos = end


def _text_reader(stream):
    """Incremental UTF-8 decoding reader over a binary stream."""
    return codecs.getreader("utf-8")(stream)


def _refill(raw, buf, pos, chunk_size):
    chunk = raw.read(chunk_size)
    buf = buf[pos:] + (chunk or "")

    return buf, 0, not chunk


def iter_json_lines(stream):
    """Yields one JSON object per line (JSON Lines), skipping blank lines."""
    for line in _text_reader(stream):
        line = line.strip()

        if line:
            yield json.loads(line)


def iter_bulk_cards(kind="default_cards"):
    """Streams every card of a bulk file straight from Scryfall.

    Returns (info, iterator). Nothing is written to disk, which spares the
    Pi's SD card a large write every day. Scryfall serves the bulk files as
    gzipped JSON Lines (jsonl_download_uri, ~80 MB); older responses had a
    plain JSON array (download_uri), still understood.
    """
    info = bulk_info(kind)
    url = info.get("jsonl_download_uri") or info.get("download_uri")

    if not url:
        raise RuntimeError("Scryfall bulk-data response has no download link: %s" % sorted(info))

    response = _request(url, accept="*/*")

    def _iter():
        with response:
            stream = _decoded_stream(response)

            # The file itself may be gzipped (.gz), on top of any transfer encoding
            if url.split("?")[0].endswith(".gz"):
                stream = gzip.GzipFile(fileobj=stream)

            parse = iter_json_lines if "jsonl" in url.split("?")[0] else iter_json_array

            for card in parse(stream):
                yield card

    return info, _iter()


class ImageCache(object):
    """Keeps every downloaded card image on disk, so the reference data can
    be rebuilt offline (after tuning, or when the close-up boxes change).

    Files mirror Scryfall's URL layout, with the image's version (the URL's
    "?1712345678" suffix) in the name: when Scryfall replaces an image the
    URL changes, the new one is downloaded and the old version removed."""
    _SAFE = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(self, folder, fetch=None):
        self.folder = folder
        self.fetch = fetch or download_bytes

    def path_for(self, url):
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]

        if not parts or not all(self._SAFE.match(p) and p not in (".", "..") for p in parts):
            return None

        stem, ext = os.path.splitext(parts[-1])
        version = re.sub(r"[^0-9A-Za-z]", "", parsed.query)[:32]
        name = "%s.%s%s" % (stem, version, ext) if version else parts[-1]

        return os.path.join(self.folder, parsed.netloc.replace(":", "_"), *(parts[:-1] + [name]))

    def __call__(self, url):
        path = self.path_for(url)

        if path is None:
            return self.fetch(url)

        if os.path.exists(path):
            with open(path, "rb") as handle:
                return handle.read()

        data = self.fetch(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())

        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

        # Older versions of the same image are now stale
        stem = os.path.basename(self.path_for(url.split("?")[0]) or "")
        prefix, ext = os.path.splitext(stem)
        for old in glob.glob(os.path.join(os.path.dirname(path), glob.escape(prefix) + ".*" + ext)):
            if old != path:
                try:
                    os.remove(old)
                except OSError:
                    pass

        return data


def download_bytes(url, retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            with _request(url, accept="image/*") as response:
                return _decoded_stream(response).read()
        except (OSError, zlib.error) as error:
            last_error = error
            time.sleep(1 + attempt * 2)

    raise last_error
