from localsorter import carddb, indexer

from . import synthetic
from .helpers import scryfall_card


def _dfc(i):
    card = scryfall_card(i, set_code="dfc")
    del card["image_uris"]
    card["card_faces"] = [
        {"name": "Front", "colors": ["G"], "image_uris": {"small": "https://example.invalid/front-%d.jpg" % i}},
        {"name": "Back", "colors": ["G"], "image_uris": {"small": "https://example.invalid/back-%d.jpg" % i}},
    ]

    return card


def _fetcher(fail=()):
    calls = []

    def fetch(url):
        calls.append(url)

        if url in fail:
            raise OSError("boom")

        return synthetic.reference_bytes(synthetic.make_card(len(url)))

    fetch.calls = calls

    return fetch


def test_pending_faces_includes_back_faces_and_skips_digital():
    conn = carddb.connect(":memory:")
    carddb.import_cards(conn, [scryfall_card(1), _dfc(2), scryfall_card(3, digital=True)])

    pending = sorted((card_id, face) for card_id, face, _ in indexer.pending_faces(conn, "small"))

    assert pending == [("card-0001-tst", 0), ("card-0002-dfc", 0), ("card-0002-dfc", 1)]


def test_build_hashes_and_resumes_after_failures():
    conn = carddb.connect(":memory:")
    carddb.import_cards(conn, [scryfall_card(i) for i in range(5)] + [_dfc(9)])
    bad = "https://example.invalid/2-tst.jpg"

    fetch = _fetcher(fail={bad})
    done, failed = indexer.build(conn, workers=2, delay=0, fetch=fetch)

    assert (done, failed) == (6, 1)
    assert conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0] == 6

    # Second run only retries what is missing
    fetch = _fetcher()
    done, failed = indexer.build(conn, workers=2, delay=0, fetch=fetch)

    assert (done, failed) == (1, 0)
    assert fetch.calls == [bad]

    # Nothing left
    assert indexer.build(conn, delay=0, fetch=_fetcher()) == (0, 0)


def test_build_limit():
    conn = carddb.connect(":memory:")
    carddb.import_cards(conn, [scryfall_card(i) for i in range(5)])

    assert indexer.build(conn, delay=0, limit=2, fetch=_fetcher()) == (2, 0)


def test_changed_image_is_processed_again():
    conn = carddb.connect(":memory:")
    card = scryfall_card(1)
    carddb.import_cards(conn, [card])
    indexer.build(conn, delay=0, fetch=_fetcher())

    # Scryfall swaps a preview for a proper scan: the URL's version changes
    card["image_uris"] = {"small": card["image_uris"]["small"] + "?1700000000"}
    carddb.import_cards(conn, [card])
    fetch = _fetcher()

    assert indexer.build(conn, delay=0, fetch=fetch) == (1, 0)
    assert fetch.calls == [card["image_uris"]["small"]]
    assert conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0] == 1
