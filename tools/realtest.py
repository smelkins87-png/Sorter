"""Accuracy test on real Scryfall card scans turned into simulated sorter photos.

Needs a synced database (python3 -m localsorter --data-dir DIR sync) and
internet access to Scryfall. From the repository root:

    python3 tools/realtest.py prepare DIR/cards.sqlite OUTDIR   # pick cards, download, cut close-ups
    python3 tools/realtest.py run DIR/cards.sqlite OUTDIR [off]  # off = without OCR

The photos are simulated (tilt, blur, exposure) from Scryfall's own scans, so
results are optimistic compared with a real camera: use it to compare
changes, and `python3 -m localsorter eval` on real scans for the truth.
"""
import io
import json
import os
import random
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image  # noqa: E402

from localsorter import carddb, printing, scryfall  # noqa: E402
from localsorter.matcher import HashIndex  # noqa: E402
from localsorter.ocr import Tesseract  # noqa: E402
from localsorter.printing import PrintingResolver  # noqa: E402
from tests import synthetic  # noqa: E402

rng = random.Random(20261014)


def pick(conn):
    q = lambda s, *a: [r["id"] for r in conn.execute(s, a)]
    cases = {}
    # A: random paper printings
    for cid in rng.sample(q("SELECT id FROM cards WHERE digital=0"), 200):
        cases[cid] = "random"
    # B: heavily reprinted art (modern frame)
    groups = conn.execute("SELECT oracle_id, illustration_id FROM cards WHERE digital=0 AND frame='2015' AND "
                          "illustration_id IS NOT NULL GROUP BY oracle_id, illustration_id HAVING COUNT(*) >= 6").fetchall()
    for g in rng.sample(groups, 40):
        ids = q("SELECT id FROM cards WHERE digital=0 AND oracle_id=? AND illustration_id=?", g["oracle_id"], g["illustration_id"])
        cases[rng.choice(ids)] = "reprint-heavy"
    # C: old frames with both border colours in the same art group
    old = conn.execute("SELECT oracle_id, illustration_id FROM cards WHERE digital=0 AND frame='1993' AND "
                       "illustration_id IS NOT NULL GROUP BY oracle_id, illustration_id "
                       "HAVING SUM(border_color='white') > 0 AND SUM(border_color='black') > 0").fetchall()
    for g in rng.sample(old, min(30, len(old))):
        ids = q("SELECT id FROM cards WHERE digital=0 AND oracle_id=? AND illustration_id=?", g["oracle_id"], g["illustration_id"])
        cases[rng.choice(ids)] = "old-border"
    for cid in q("SELECT id FROM cards WHERE name='Lightning Bolt' AND set_code IN ('LEB','2ED','3ED')"):
        cases[cid] = "old-border"
    return cases


def prepare(db, out):
    conn = carddb.connect(db)
    os.makedirs(out + "/cards", exist_ok=True)
    cases = pick(conn)

    # Unknown cards: printings whose whole oracle card we'll hide from the index
    unknown = rng.sample([r["id"] for r in conn.execute(
        "SELECT id FROM cards WHERE digital=0 AND frame='2015' AND id NOT IN (%s)" % ",".join("?" * len(cases)),
        list(cases))], 30)
    for cid in unknown:
        cases[cid] = "unknown"

    rows = {r["id"]: r for r in conn.execute("SELECT * FROM cards WHERE id IN (%s)" % ",".join("?" * len(cases)), list(cases))}

    def fetch_png(cid):
        path = "%s/cards/%s.png" % (out, cid)
        if not os.path.exists(path):
            urls = json.loads(rows[cid]["image_front"])
            url = urls.get("large") or urls.get("normal")
            open(path, "wb").write(scryfall.download_bytes(url))
        return cid

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(fetch_png, cases))

    # Close-ups for every look-alike of every test card
    needed = set()
    for cid, kind in cases.items():
        if kind != "unknown":
            needed |= set(carddb.lookalikes(conn, cid))
    have = {r[0] for r in conn.execute("SELECT card_id FROM regions WHERE face=0")}
    todo = sorted(needed - have)
    print("close-ups to cut:", len(todo))
    urls = {r["id"]: json.loads(r["image_front"]).get("normal") for r in conn.execute(
        "SELECT id, image_front FROM cards WHERE id IN (%s)" % ",".join("?" * len(todo)), todo)} if todo else {}
    lock = threading.Lock()

    def cut(cid):
        sym, bottom = printing.reference_regions(scryfall.download_bytes(urls[cid]))
        with lock, conn:
            conn.execute("INSERT OR REPLACE INTO regions VALUES (?, 0, ?, ?, ?)", (cid, urls[cid], sym, bottom))

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(cut, [c for c in todo if urls.get(c)]))

    json.dump(cases, open(out + "/cases.json", "w"), indent=1)
    print("cases:", Counter(cases.values()))


def run(db, out, ocr_mode="auto"):
    conn = carddb.connect(db)
    cases = json.load(open(out + "/cases.json"))
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM cards WHERE id IN (%s)" % ",".join("?" * len(cases)), list(cases))}

    index = HashIndex.from_db(conn)
    # Hide the "unknown" cards: every printing of their oracle card
    hidden_oracles = {rows[c]["oracle_id"] for c, k in cases.items() if k == "unknown"}
    keep = [i for i, o in enumerate(index.oracle) if o not in hidden_oracles]
    import numpy as np
    keep = np.array(keep)
    index.card_ids = [index.card_ids[i] for i in keep]
    index.oracle = [index.oracle[i] for i in keep]
    index.set_codes = [index.set_codes[i] for i in keep]
    index.faces = index.faces[keep]
    index.full = index.full[keep]
    index.art = index.art[keep]
    print("index size:", len(index))

    resolver = PrintingResolver(conn, threading.Lock(), Tesseract() if ocr_mode != "off" else None, ocr_mode)
    stats = Counter()
    by_kind = {}
    dist = {"right": [], "wrong": [], "unknown": []}
    mistakes = []
    times = []
    margins = []

    for n, (cid, kind) in enumerate(sorted(cases.items())):
        r = random.Random(n)
        card = Image.open("%s/cards/%s.png" % (out, cid))
        card = card.convert("RGBA") if card.mode in ("RGBA", "LA", "P") else card.convert("RGB")
        photo = synthetic.photo(card, seed=n, card_width=1200, canvas_size=(2150, 1850),
                                background=(115, 112, 108), upside_down=r.random() < 0.3, blur=r.uniform(0.6, 1.4))
        t = time.time()
        match = index.identify(photo, 80, 15, 4)
        decision = resolver.resolve(match) if match.found else None
        times.append(time.time() - t)
        if decision:
            match.card_id, match.set_detected = decision.card_id, decision.set_detected
            regions = decision.evidence.get("regions")
            if regions and len(regions) > 1:
                totals = sorted(v["total"] for v in regions.values())
                right = decision.card_id == cid
                margins.append((right, round(totals[1] - totals[0], 1), round((totals[1] - totals[0]) / max(totals[1], 1e-6), 2),
                                decision.method, rows[cid]["set_code"]))

        k = by_kind.setdefault(kind, Counter())
        k["n"] += 1
        truth = rows[cid]
        got = conn.execute("SELECT oracle_id, set_code, collector_number, name FROM cards WHERE id=?", (match.card_id,)).fetchone() if match.card_id else None

        if kind == "unknown":
            dist["unknown"].append(match.distance)
            k["correctly rejected" if not match.found else "FALSE ACCEPT"] += 1
            if match.found:
                mistakes.append(("false accept", truth["name"], got["name"], match.distance, match.margin))
            continue

        same_card = got is not None and got["oracle_id"] == truth["oracle_id"]
        dist["right" if same_card else "wrong"].append(match.distance)
        if not match.found:
            k["not found"] += 1
        elif not same_card:
            k["WRONG CARD"] += 1
            mistakes.append(("wrong card", truth["name"], truth["set_code"], got["name"], match.distance, match.margin))
        elif match.card_id == cid:
            k["right printing, confident" if match.set_detected else "right printing, flagged unsure"] += 1
        elif match.set_detected:
            k["CONFIDENTLY WRONG printing"] += 1
            mistakes.append(("wrong printing", truth["name"], truth["set_code"], truth["collector_number"],
                             got["set_code"], got["collector_number"], decision.method if decision else "",
                             (decision.evidence.get("ocr_text") or "")[:80] if decision else ""))
        else:
            k["other printing, flagged unsure"] += 1

    for kind, k in sorted(by_kind.items()):
        print("%-14s %s" % (kind, dict(k)))
    for key, values in dist.items():
        values = sorted(v for v in values if v is not None)
        if values:
            print("%-8s distance: min %d  p10 %d  median %d  p90 %d  max %d" % (
                key, values[0], values[len(values) // 10], values[len(values) // 2], values[9 * len(values) // 10], values[-1]))
    print("time per card: avg %.0f ms, max %.0f ms" % (1000 * sum(times) / len(times), 1000 * max(times)))
    for m in mistakes:
        print("  ", m)
    print("region decisions (right?, margin, ratio, method, set):")
    for m in sorted(margins, key=lambda m: m[1]):
        print("  ", m)


if __name__ == "__main__":
    {"prepare": prepare, "run": run}[sys.argv[1]](*sys.argv[2:])
