"""Downloads Scryfall card images and stores their hashes.

Resumable and incremental: only faces without a hash (new cards, or a
previous run interrupted) are fetched. Images are hashed in memory and
discarded, so the Pi's SD card only stores ~40 bytes per card.
"""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import imagehash, scryfall

log = logging.getLogger("localsorter")


def pending_faces(conn, size, include_digital=False):
    """(card_id, face, url) for every face image not hashed yet."""
    query = ("SELECT c.id, c.image_front, c.image_back, "
             "EXISTS(SELECT 1 FROM hashes h WHERE h.card_id = c.id AND h.face = 0) AS has_front, "
             "EXISTS(SELECT 1 FROM hashes h WHERE h.card_id = c.id AND h.face = 1) AS has_back "
             "FROM cards c")

    if not include_digital:
        query += " WHERE c.digital = 0"

    for row in conn.execute(query):
        for face, column, done in ((0, "image_front", row["has_front"]), (1, "image_back", row["has_back"])):
            if done or not row[column]:
                continue

            urls = json.loads(row[column])
            url = urls.get(size) or urls.get("small") or urls.get("normal")

            if url:
                yield row["id"], face, url


def build(conn, size="small", workers=4, delay=0.1, include_digital=False, limit=None,
          fetch=scryfall.download_bytes, progress=None):
    todo = list(pending_faces(conn, size, include_digital))

    if limit:
        todo = todo[:limit]

    log.info("%d card images to hash", len(todo))

    if not todo:
        return 0, 0

    db_lock = threading.Lock()
    done = 0
    failed = 0
    batch = []

    def work(item):
        card_id, face, url = item
        data = fetch(url)
        full, art = imagehash.reference_hashes(data)

        if delay:
            time.sleep(delay)

        return card_id, face, url, full, art

    def flush():
        with db_lock, conn:
            conn.executemany("INSERT OR REPLACE INTO hashes (card_id, face, source_url, full_hash, art_hash) "
                             "VALUES (?, ?, ?, ?, ?)", batch)
        batch.clear()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, item) for item in todo]

        for future in as_completed(futures):
            try:
                batch.append(future.result())
                done += 1
            except Exception as error:  # keep going; the next run retries it
                failed += 1
                log.warning("Could not hash image: %s", error)

            if len(batch) >= 500:
                flush()

                if progress:
                    progress(done, failed, len(todo))

    if batch:
        flush()

    if progress:
        progress(done, failed, len(todo))

    return done, failed
