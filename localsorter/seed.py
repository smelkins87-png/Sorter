"""Card data built elsewhere, so the Pi skips its first download (hours of
card images). `localsorter pack` makes the packages from a synced database;
`localsorter seed` (run by deploy/install.sh) loads them on the Pi.

Two packages, each a folder with manifest.json and the parts it lists:

  core       cards, sets, prices and fingerprints, gzip-compressed (~70 MB):
             small enough for the SD card's boot partition
  closeups   set-symbol and collector-line close-ups (~1 GB of PNGs, which
             don't compress): too big for the boot partition, so it goes in
             the network folder or on a USB stick

Parts stay under GitHub's 100 MB file limit. Every part is checked against
its SHA-256 before anything is loaded. Loading only adds what the Pi doesn't
have yet: core only into a database that has no fingerprints, close-ups
without replacing any it has. Reviews and stop cards are never shipped or
touched. Whatever's missing afterwards, the nightly sync fetches.
"""
import glob
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile

from . import carddb, printing

log = logging.getLogger("localsorter")

FORMAT = 1
PART_SIZE = 95 * 1000 * 1000
# Where a package may be: the SD card's boot partition (copied there with
# the localsorter folder, or next to it), the network folder, a USB stick
SEARCH = [
    "/boot/firmware/localsorter/card-data",
    "/boot/firmware/card-data",
    "/home/pi/public/card-data",
    "/media/*/card-data",
    "/media/*/*/card-data",
    "/mnt/*/card-data",
]
CORE_TABLES = ("cards", "sets", "hashes", "meta")
# Local to each sorter: never shipped
PRIVATE_TABLES = ("labels", "kv", "stop_cards")


# --- making packages -------------------------------------------------------

def _sha256(path):
    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return digest.hexdigest()


def _split(source, folder, stem, part_size, compress):
    """Writes source as numbered parts (gzipped first if compress)."""
    parts = []
    index = 0
    out = None
    written = 0

    def next_part():
        nonlocal out, written, index
        if out is not None:
            out.close()
        name = "%s.%03d" % (stem, index)
        index += 1
        written = 0
        out = open(os.path.join(folder, name), "wb")
        parts.append(name)

    class Splitter(object):
        def write(self, data):
            nonlocal written
            view = memoryview(data)
            while view:
                if out is None or written >= part_size:
                    next_part()
                take = min(len(view), part_size - written)
                out.write(view[:take])
                written += take
                view = view[take:]
            return len(data)

        def flush(self):
            pass

    splitter = Splitter()

    with open(source, "rb") as handle:
        if compress:
            with gzip.GzipFile(fileobj=splitter, mode="wb", compresslevel=6, mtime=0) as packed:
                shutil.copyfileobj(handle, packed, 1 << 20)
        else:
            shutil.copyfileobj(handle, splitter, 1 << 20)

    if out is not None:
        out.close()

    return [{"name": name, "size": os.path.getsize(os.path.join(folder, name)),
             "sha256": _sha256(os.path.join(folder, name))} for name in parts]


def _write_package(db_file, folder, kind, compression, info, part_size):
    os.makedirs(folder, exist_ok=True)

    for old in glob.glob(os.path.join(folder, "*")):
        os.remove(old)

    stem = "%s.sqlite%s" % (kind, ".gz" if compression == "gzip" else "")
    parts = _split(db_file, folder, stem, part_size, compression == "gzip")
    manifest = dict(info, format=FORMAT, kind=kind, compression=compression,
                    sha256=_sha256(db_file), size=os.path.getsize(db_file), parts=parts)

    with open(os.path.join(folder, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)

    log.info("%s: %d parts, %.0f MB", kind, len(parts), sum(p["size"] for p in parts) / 1e6)

    return manifest


def pack(db_path, out_dir, part_size=PART_SIZE):
    """Makes out_dir/core and out_dir/closeups from a synced database."""
    src = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    meta = dict(src.execute("SELECT key, value FROM meta").fetchall())
    info = {"bulk_updated_at": meta.get("bulk_updated_at"), "prices_updated": meta.get("prices_updated"),
            "regions_version": meta.get("regions_version")}

    with tempfile.TemporaryDirectory(dir=out_dir if os.path.isdir(out_dir) else None) as tmp:
        # core: everything but the close-ups and what's private to a sorter
        core = os.path.join(tmp, "core.sqlite")
        dst = sqlite3.connect(core)
        src.backup(dst)
        tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("regions",) + PRIVATE_TABLES:
            if table in tables:
                dst.execute("DELETE FROM %s" % table)
        dst.commit()
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("VACUUM")
        counts = {t: dst.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("cards", "sets", "hashes")}
        dst.close()
        core_manifest = _write_package(core, os.path.join(out_dir, "core"), "core", "gzip",
                                       dict(info, rows=counts), part_size)
        os.remove(core)

        # close-ups: the regions table on its own
        closeups = os.path.join(tmp, "closeups.sqlite")
        dst = sqlite3.connect(closeups)
        dst.execute("CREATE TABLE regions (card_id TEXT NOT NULL, face INTEGER NOT NULL, source_url TEXT, "
                    "symbol BLOB NOT NULL, bottom BLOB NOT NULL, PRIMARY KEY (card_id, face))")
        dst.execute("ATTACH DATABASE ? AS src", ("file:%s?mode=ro" % db_path,))
        dst.execute("INSERT INTO regions SELECT card_id, face, source_url, symbol, bottom FROM src.regions")
        dst.commit()
        dst.execute("DETACH DATABASE src")
        count = dst.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
        dst.close()
        closeups_manifest = _write_package(closeups, os.path.join(out_dir, "closeups"), "closeups", "none",
                                           dict(info, rows={"regions": count}), part_size)
        os.remove(closeups)

    src.close()

    return core_manifest, closeups_manifest


# --- loading packages ------------------------------------------------------

def _manifest(folder):
    try:
        with open(os.path.join(folder, "manifest.json")) as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return None

    return manifest if isinstance(manifest, dict) and manifest.get("format") == FORMAT else None


def find(search=SEARCH):
    """{"core": (folder, manifest), "closeups": (...)}: the newest of each kind."""
    found = {}

    for pattern in search:
        for top in sorted(glob.glob(pattern)):
            for folder in (os.path.join(top, "core"), os.path.join(top, "closeups"), top):
                manifest = _manifest(folder)

                if manifest is None or manifest.get("kind") not in ("core", "closeups"):
                    continue

                kind = manifest["kind"]
                if kind not in found or (manifest.get("bulk_updated_at") or "") > \
                        (found[kind][1].get("bulk_updated_at") or ""):
                    found[kind] = (folder, manifest)

    return found


def _assemble(folder, manifest, dest):
    """Checks every part, then writes the database file to dest."""
    for part in manifest["parts"]:
        path = os.path.join(folder, os.path.basename(part["name"]))

        if not os.path.exists(path):
            raise ValueError("%s is missing (copy the whole %s folder)" % (part["name"], manifest["kind"]))
        if os.path.getsize(path) != part["size"] or _sha256(path) != part["sha256"]:
            raise ValueError("%s is damaged or incomplete (copy it again)" % part["name"])

    class Joined(object):
        """The parts read back to back, as one file."""
        def __init__(self):
            self.files = [open(os.path.join(folder, os.path.basename(p["name"])), "rb") for p in manifest["parts"]]

        def read(self, size=-1):
            out = b""
            while self.files and (size < 0 or len(out) < size):
                chunk = self.files[0].read(-1 if size < 0 else size - len(out))
                if chunk:
                    out += chunk
                else:
                    self.files.pop(0).close()
            return out

        def close(self):
            for handle in self.files:
                handle.close()

    joined = Joined()

    try:
        source = gzip.GzipFile(fileobj=joined, mode="rb") if manifest["compression"] == "gzip" else joined
        with open(dest, "wb") as out:
            shutil.copyfileobj(source, out, 1 << 20)
    finally:
        joined.close()

    if _sha256(dest) != manifest["sha256"]:
        os.remove(dest)
        raise ValueError("The %s package doesn't add up after unpacking" % manifest["kind"])


def _columns(conn, schema, table):
    return [r[1] for r in conn.execute("PRAGMA %s.table_info(%s)" % (schema, table))]


def load_core(conn, folder, manifest, workdir):
    """Cards, sets, prices and fingerprints, into a database that has no
    fingerprints yet. Returns the number of fingerprints added."""
    if conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]:
        log.info("Card data: this sorter already has fingerprints, keeping its own (newer) data")
        return 0

    tmp = os.path.join(workdir, "seed-core.sqlite")
    _assemble(folder, manifest, tmp)

    try:
        conn.execute("ATTACH DATABASE ? AS seed", (tmp,))
        with conn:
            for table in CORE_TABLES:
                columns = [c for c in _columns(conn, "seed", table) if c in _columns(conn, "main", table)]
                names = ", ".join(columns)
                conn.execute("INSERT OR REPLACE INTO main.%s (%s) SELECT %s FROM seed.%s"
                             % (table, names, names, table))
        conn.execute("DETACH DATABASE seed")
    finally:
        os.remove(tmp)

    carddb.set_meta(conn, "data_version", carddb.now_iso())
    added = conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
    log.info("Card data loaded: %d cards, %d fingerprints (prices of %s)",
             conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0], added, manifest.get("prices_updated"))

    return added


def load_closeups(conn, folder, manifest, workdir):
    """Adds the close-ups this sorter doesn't have. Returns how many."""
    if manifest.get("regions_version") != printing.REGIONS_VERSION:
        log.warning("Close-ups: made by another version (%s, this one uses %s); the sync will cut new ones",
                    manifest.get("regions_version"), printing.REGIONS_VERSION)
        return 0

    tmp = os.path.join(workdir, "seed-closeups.sqlite")
    _assemble(folder, manifest, tmp)

    try:
        before = conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
        conn.execute("ATTACH DATABASE ? AS seed", (tmp,))
        with conn:
            conn.execute("INSERT OR IGNORE INTO main.regions (card_id, face, source_url, symbol, bottom) "
                         "SELECT card_id, face, source_url, symbol, bottom FROM seed.regions")
        conn.execute("DETACH DATABASE seed")
    finally:
        os.remove(tmp)

    carddb.set_meta(conn, "regions_version", printing.REGIONS_VERSION)
    carddb.set_meta(conn, "data_version", carddb.now_iso())
    added = conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0] - before
    log.info("Close-ups loaded: %d added", added)

    return added


def seed(db_path, search=SEARCH):
    """Loads whatever packages are found. Returns {"core": n, "closeups": n}."""
    found = find(search)
    result = {"core": 0, "closeups": 0}

    if not found:
        log.info("No card-data packages found (looked in %s)", ", ".join(search))
        return result

    conn = carddb.connect(db_path)
    workdir = os.path.dirname(os.path.abspath(db_path))

    try:
        if "core" in found:
            log.info("Card data package: %s", found["core"][0])
            result["core"] = load_core(conn, found["core"][0], found["core"][1], workdir)
        if "closeups" in found:
            log.info("Close-ups package: %s", found["closeups"][0])
            result["closeups"] = load_closeups(conn, found["closeups"][0], found["closeups"][1], workdir)
    finally:
        conn.close()

    return result
