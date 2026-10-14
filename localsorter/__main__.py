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
import time

from . import carddb, indexer, scryfall
from .matcher import HashIndex
from .settings import Settings

log = logging.getLogger("localsorter")


def cmd_prices(settings, args):
    conn = carddb.connect(settings.db_path)

    log.info("Fetching set list")
    count = carddb.import_sets(conn, scryfall.fetch_sets())
    log.info("Stored %d sets", count)

    info, cards = scryfall.iter_bulk_cards("default_cards")
    log.info("Streaming %s (%s, %.0f MB)", info.get("name"), info.get("updated_at"),
             (info.get("size") or 0) / 1e6)

    started = time.monotonic()
    total = carddb.import_cards(conn, cards, progress=lambda n: log.info("  %d cards", n) if n % 20000 == 0 else None)
    log.info("Stored %d cards in %.0f s", total, time.monotonic() - started)

    carddb.set_meta(conn, "bulk_updated_at", info.get("updated_at"))
    carddb.set_meta(conn, "prices_updated", carddb.now_iso())
    carddb.set_meta(conn, "data_version", carddb.now_iso())

    return conn


def cmd_index(settings, args, conn=None):
    conn = conn or carddb.connect(settings.db_path)
    started = time.monotonic()

    def progress(done, failed, total):
        rate = done / max(time.monotonic() - started, 1)
        log.info("  hashed %d/%d (%d failed, %.1f/s)", done, total, failed, rate)

    done, failed = indexer.build(conn, size=settings.index_image_size, workers=settings.download_workers,
                                 delay=settings.download_delay, include_digital=settings.include_digital,
                                 limit=getattr(args, "limit", None), progress=progress)

    if done:
        carddb.set_meta(conn, "data_version", carddb.now_iso())

    log.info("Hashed %d new images (%d failed; rerun to retry)", done, failed)

    return 1 if failed and not done else 0


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

    server = make_server(app, settings.listen_host, settings.listen_port)
    log.info("Listening on http://%s:%d (%d reference images)", settings.listen_host,
             settings.listen_port, len(app.index))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    return 0


def _identify_file(app_index, conn, settings, path):
    with open(path, "rb") as handle:
        data = handle.read()

    started = time.monotonic()
    match = app_index.identify(data, settings.max_distance, settings.min_margin, settings.set_margin)
    elapsed = (time.monotonic() - started) * 1000
    row = conn.execute("SELECT name, set_code, collector_number FROM cards WHERE id = ?",
                       (match.card_id,)).fetchone() if match.card_id else None

    return match, row, elapsed


def cmd_identify(settings, args):
    conn = carddb.connect(settings.db_path)
    index = HashIndex.from_db(conn, settings.include_digital)

    for path in args.images:
        match, row, elapsed = _identify_file(index, conn, settings, path)
        label = "%s (%s) #%s" % (row["name"], row["set_code"], row["collector_number"]) if row else "-"
        verdict = "MATCH" if match.found else "NO MATCH (closest)"

        print("%s: %s %s  distance=%s margin=%s set_detected=%s  %.0f ms"
              % (path, verdict, label, match.distance, match.margin, match.set_detected, elapsed))

    return 0


def cmd_eval(settings, args):
    """Labelled scans: a CSV with columns image + set + collector_number (or
    image + scryfall_id). A day's scans.csv works once you fix wrong rows."""
    conn = carddb.connect(settings.db_path)
    index = HashIndex.from_db(conn, settings.include_digital)
    base = os.path.dirname(os.path.abspath(args.labels))
    stats = {"total": 0, "correct": 0, "correct_card_wrong_set": 0, "wrong": 0, "not_found": 0}
    distances = {"correct": [], "wrong": []}

    with open(args.labels, newline="", encoding="utf-8-sig") as handle:
        for label in csv.DictReader(handle):
            path = os.path.join(base, label["image"])

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

            if not expected:
                continue

            match, row, _ = _identify_file(index, conn, settings, path)
            stats["total"] += 1

            if match.card_id == expected:
                distances["correct"].append(match.distance)
            else:
                distances["wrong"].append(match.distance)

            if not match.found:
                stats["not_found"] += 1
            elif match.card_id == expected:
                stats["correct"] += 1
            else:
                same = conn.execute("SELECT a.oracle_id = b.oracle_id FROM cards a, cards b WHERE a.id = ? AND b.id = ?",
                                    (match.card_id, expected)).fetchone()
                stats["correct_card_wrong_set" if same and same[0] else "wrong"] += 1

    print(json.dumps(stats, indent=2))

    for key, values in distances.items():
        if values:
            values.sort()
            print("%s distances: min=%d median=%d max=%d" % (key, values[0], values[len(values) // 2], values[-1]))

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
