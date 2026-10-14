"""Command line entry point: python3 -m localsorter <command>

    sync       download card data + prices from Scryfall, then hash new images
    prices     card data + prices only (no image downloads)
    index      hash card images that are not indexed yet
    serve      run the API server the sorter talks to
    identify   identify one or more image files (for testing)
    eval       check accuracy on a folder of labelled scans
"""
import argparse
import csv
import json
import logging
import os
import sys
import threading
import time

from . import carddb, indexer, scryfall
from .matcher import HashIndex
from .ocr import Tesseract
from .printing import PrintingResolver
from .settings import Settings

log = logging.getLogger("localsorter")


def cmd_prices(settings, args):
    conn = carddb.connect(settings.db_path)

    log.info("Fetching set list")
    count = carddb.import_sets(conn, scryfall.fetch_sets())
    log.info("Stored %d sets", count)

    info, cards = scryfall.iter_bulk_cards("default_cards")
    log.info("Streaming %s (%s, %.0f MB)", info.get("name"), info.get("updated_at"),
             (info.get("compressed_size") or info.get("size") or 0) / 1e6)

    started = time.monotonic()
    total = carddb.import_cards(conn, cards, progress=lambda n: log.info("  %d cards", n) if n % 20000 == 0 else None)
    log.info("Stored %d cards in %.0f s", total, time.monotonic() - started)

    carddb.set_meta(conn, "bulk_updated_at", info.get("updated_at"))
    carddb.set_meta(conn, "prices_updated", carddb.now_iso())
    carddb.set_meta(conn, "data_version", carddb.now_iso())

    return conn


def cmd_index(settings, args, conn=None):
    conn = conn or carddb.connect(settings.db_path)
    limit = getattr(args, "limit", None)
    common = dict(workers=settings.download_workers, delay=settings.download_delay,
                  include_digital=settings.include_digital, limit=limit)
    results = []

    for label, build, size in (("hashed", indexer.build, settings.index_image_size),
                               ("close-ups", indexer.build_regions, settings.region_image_size)):
        started = time.monotonic()

        def progress(done, failed, total, label=label, started=started):
            rate = done / max(time.monotonic() - started, 1)
            log.info("  %s %d/%d (%d failed, %.1f/s)", label, done, total, failed, rate)

        done, failed = build(conn, size=size, progress=progress, **common)
        log.info("%s: %d new images (%d failed; rerun to retry)", label.capitalize(), done, failed)
        results.append((done, failed))

        if done:
            carddb.set_meta(conn, "data_version", carddb.now_iso())

    return 1 if any(failed and not done for done, failed in results) else 0


def cmd_prices_only(settings, args):
    cmd_prices(settings, args)

    return 0


def cmd_sync(settings, args):
    conn = cmd_prices(settings, args)

    return cmd_index(settings, args, conn)


def cmd_serve(settings, args):
    from .server import App, make_server

    app = App(settings)

    if not len(app.index):
        log.warning("The recognition index is empty: run `python3 -m localsorter sync` first")

    if settings.review_port:
        from .review import start_review_server

        start_review_server(app, settings)

    server = make_server(app, settings.listen_host, settings.listen_port)
    log.info("Listening on http://%s:%d (%d reference images)", settings.listen_host,
             settings.listen_port, len(app.index))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    return 0


class Pipeline:
    """Both recognition stages, as the server runs them."""

    def __init__(self, settings):
        self.settings = settings
        self.conn = carddb.connect(settings.db_path)
        self.lock = threading.Lock()
        self.index = HashIndex.from_db(self.conn, settings.include_digital)
        tesseract = Tesseract(settings.tesseract_cmd) if settings.ocr != "off" else None
        self.resolver = PrintingResolver(self.conn, self.lock, tesseract, settings.ocr, settings.region_margin,
                                         settings.border_check)

    def identify_file(self, path, foil_mode=0, lang="en"):
        with open(path, "rb") as handle:
            data = handle.read()

        s = self.settings
        started = time.monotonic()
        match = self.index.identify(data, s.max_distance, s.min_margin, s.set_margin)
        decision = None

        if match.found:
            decision = self.resolver.resolve(match, foil_mode, lang)
            match.card_id, match.set_detected = decision.card_id, decision.set_detected

        elapsed = (time.monotonic() - started) * 1000
        row = self.conn.execute("SELECT name, set_code, collector_number, oracle_id FROM cards WHERE id = ?",
                                (match.card_id,)).fetchone() if match.card_id else None

        return match, decision, row, elapsed


def cmd_identify(settings, args):
    pipeline = Pipeline(settings)

    for path in args.images:
        match, decision, row, elapsed = pipeline.identify_file(path, args.foil, args.lang)
        label = "%s (%s) #%s" % (row["name"], row["set_code"], row["collector_number"]) if row else "-"
        verdict = "MATCH" if match.found else "NO MATCH (closest)"
        extra = ""

        if decision is not None:
            extra = "  printing=%s%s%s" % (decision.method, "" if decision.set_detected else " (set not detected)",
                                           "" if decision.foil is None else " foil=%d" % decision.foil)
            if decision.lang:
                extra += " lang=" + decision.lang
            if decision.border:
                extra += " border=" + decision.border

        print("%s: %s %s  distance=%s margin=%s%s  %.0f ms"
              % (path, verdict, label, match.distance, match.margin, extra, elapsed))

        if args.verbose and decision is not None and decision.evidence:
            print("    " + json.dumps(decision.evidence))

    return 0


def cmd_eval(settings, args):
    """Labelled scans: a CSV with columns image + set + collector_number (or
    image + scryfall_id). A day's scans.csv works once you fix wrong rows.

    The number that matters most is "confidently wrong": a wrong card, or a
    wrong printing reported with the set as detected. Everything flagged
    "set not detected" can be checked by hand; a confident mistake can't."""
    pipeline = Pipeline(settings)
    conn = pipeline.conn
    base = os.path.dirname(os.path.abspath(args.labels))
    stats = {"total": 0, "not_found": 0, "right_printing": 0, "right_printing_but_set_not_detected": 0,
             "right_card_other_printing_flagged": 0, "CONFIDENTLY_WRONG_printing": 0, "WRONG_card": 0}
    distances = {"right card": [], "wrong card": []}
    mistakes = []

    with open(args.labels, newline="", encoding="utf-8-sig") as handle:
        for label in csv.DictReader(handle):
            path = os.path.join(base, label.get("image") or "")

            if not label.get("image") or not os.path.exists(path):
                continue

            # set + collector number win: in a corrected scans.csv the
            # scryfall_id column still holds the original (wrong) guess
            expected = None
            number = label.get("collector_number") or label.get("num")

            if label.get("set") and number:
                row = conn.execute("SELECT id FROM cards WHERE set_code = ? AND collector_number = ?",
                                   (label["set"].upper(), number)).fetchone()
                expected = row["id"] if row else None

            expected = expected or label.get("scryfall_id")
            truth = conn.execute("SELECT oracle_id FROM cards WHERE id = ?", (expected,)).fetchone() if expected else None

            if truth is None:
                continue

            match, decision, row, _ = pipeline.identify_file(path)
            stats["total"] += 1
            same_card = row is not None and row["oracle_id"] == truth["oracle_id"]
            distances["right card" if same_card else "wrong card"].append(match.distance)

            if not match.found:
                stats["not_found"] += 1
            elif match.card_id == expected:
                stats["right_printing" if match.set_detected else "right_printing_but_set_not_detected"] += 1
            elif same_card:
                key = "CONFIDENTLY_WRONG_printing" if match.set_detected else "right_card_other_printing_flagged"
                stats[key] += 1
                if match.set_detected:
                    mistakes.append((label["image"], row["set_code"], row["collector_number"], decision.evidence))
            else:
                stats["WRONG_card"] += 1
                mistakes.append((label["image"], row["name"], row["set_code"], None))

    print(json.dumps(stats, indent=2))

    for key, values in distances.items():
        if values:
            values.sort()
            print("%s distances: min=%d median=%d max=%d" % (key, values[0], values[len(values) // 2], values[-1]))

    for mistake in mistakes:
        print("mistake:", json.dumps(mistake))

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="localsorter", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="JSON settings file (default: $LOCALSORTER_CONFIG)")
    parser.add_argument("--data-dir", help="override data_dir")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="update card data and prices, then hash new images")
    sub.add_parser("prices", help="update card data and prices only")
    p_index = sub.add_parser("index", help="hash card images not indexed yet")
    p_index.add_argument("--limit", type=int, help="stop after N images (testing)")
    sub.add_parser("serve", help="run the API server")
    p_ident = sub.add_parser("identify", help="identify image files")
    p_ident.add_argument("images", nargs="+")
    p_ident.add_argument("--foil", type=int, default=0, help="sorter foil setting: 0 none, 1 all, 2 detect")
    p_ident.add_argument("--lang", default="en", help="sorter language setting (auto = read from card)")
    p_eval = sub.add_parser("eval", help="measure accuracy on labelled scans")
    p_eval.add_argument("labels", help="CSV with image + scryfall_id (or set + collector_number)")

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    settings = Settings.load(args.config)

    if args.data_dir:
        settings.data_dir = args.data_dir

    command = {"sync": cmd_sync, "prices": cmd_prices_only, "index": cmd_index,
               "serve": cmd_serve, "identify": cmd_identify, "eval": cmd_eval}[args.command]

    return command(settings, args) or 0


if __name__ == "__main__":
    sys.exit(main())
