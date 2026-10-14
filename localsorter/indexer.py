"""Downloads Scryfall card images and stores what recognition needs.

* hashes:  every card face (small image), ~40 bytes each
* regions: set symbol + collector info close-ups (normal image), only for
           printings that share their artwork with another printing, ~5 KB each

Resumable and incremental: only faces without data (new cards, or a previous
run interrupted) are fetched. Images are processed in memory and discarded.
"""
import itertools
import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import imagehash, printing, scryfall

log = logging.getLogger("localsorter")

# Printings that share artwork with another paper printing of the same card
_LOOKALIKE = ("EXISTS (SELECT 1 FROM cards o WHERE o.oracle_id = c.oracle_id AND o.id <> c.id "
              "AND o.digital = 0 AND (o.illustration_id = c.illustration_id "
              "OR o.illustration_id IS NULL OR c.illustration_id IS NULL))")


def _pending(conn, table, size, include_digital, only_lookalikes=False):
    """(card_id, face, url) for every face image not processed yet, or whose
    image changed since. Scryfall's image URLs carry a version suffix that
    changes when e.g. a low-res preview is replaced by a proper scan."""
    query = ("SELECT c.id, c.image_front, c.image_back, "
             "(SELECT x.source_url FROM %(t)s x WHERE x.card_id = c.id AND x.face = 0) AS done_front, "
             "(SELECT x.source_url FROM %(t)s x WHERE x.card_id = c.id AND x.face = 1) AS done_back "
             "FROM cards c WHERE 1 = 1" % {"t": table})

    if not include_digital:
        query += " AND c.digital = 0"

    if only_lookalikes:
        query += " AND c.oracle_id IS NOT NULL AND " + _LOOKALIKE

    for row in conn.execute(query):
        for face, column, done_url in ((0, "image_front", row["done_front"]), (1, "image_back", row["done_back"])):
            if not row[column]:
                continue

            urls = json.loads(row[column])
            url = urls.get(size) or urls.get("normal") or urls.get("small")

            if url and url != done_url:
                yield row["id"], face, url


def pending_faces(conn, size, include_digital=False):
    return _pending(conn, "hashes", size, include_digital)


def pending_regions(conn, size, include_digital=False):
    return _pending(conn, "regions", size, include_digital, only_lookalikes=True)


def _run(conn, todo, process, insert_sql, workers, delay, fetch, progress):
    if not todo:
        return 0, 0

    db_lock = threading.Lock()
    done = 0
    failed = 0
    batch = []

    def work(item):
        card_id, face, url = item
        result = process(fetch(url))

        if delay:
            time.sleep(delay)

        return (card_id, face, url) + tuple(result)

    def flush():
        with db_lock, conn:
            conn.executemany(insert_sql, batch)
        batch.clear()

    # Only a few jobs in flight at a time: finished futures hold their
    # results, and 79k close-ups at once would not fit in a Pi's memory
    pending = iter(todo)
    window = max(1, workers) * 4

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        running = {pool.submit(work, item) for item in itertools.islice(pending, window)}

        while running:
            finished, running = wait(running, return_when=FIRST_COMPLETED)

            for future in finished:
                try:
                    batch.append(future.result())
                    done += 1
                except Exception as error:  # keep going; the next run retries it
                    failed += 1
                    log.warning("Could not process image: %s", error)

            running |= {pool.submit(work, item) for item in itertools.islice(pending, len(finished))}

            if len(batch) >= 500:
                flush()

                if progress:
                    progress(done, failed, len(todo))

    if batch:
        flush()

    if progress:
        progress(done, failed, len(todo))

    return done, failed


def build(conn, size="small", workers=4, delay=0.1, include_digital=False, limit=None,
          fetch=scryfall.download_bytes, progress=None):
    """Perceptual hashes for every card face."""
    todo = list(pending_faces(conn, size, include_digital))[:limit or None]
    log.info("%d card images to hash", len(todo))

    return _run(conn, todo, imagehash.reference_hashes,
                "INSERT OR REPLACE INTO hashes (card_id, face, source_url, full_hash, art_hash) "
                "VALUES (?, ?, ?, ?, ?)", workers, delay, fetch, progress)


def build_regions(conn, size="normal", workers=4, delay=0.1, include_digital=False, limit=None,
                  fetch=scryfall.download_bytes, progress=None):
    """Set symbol + collector info close-ups for look-alike printings."""
    todo = list(pending_regions(conn, size, include_digital))[:limit or None]
    log.info("%d look-alike printings to cut close-ups from", len(todo))

    return _run(conn, todo, printing.reference_regions,
                "INSERT OR REPLACE INTO regions (card_id, face, source_url, symbol, bottom) "
                "VALUES (?, ?, ?, ?, ?)", workers, delay, fetch, progress)
