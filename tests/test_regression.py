"""
Offline regression tests for the scrape pipeline.

These tests use saved HTML fixtures (no network calls) to verify that
trafilatura.extract() behaves consistently across version upgrades.

To regenerate snapshots after a deliberate extraction change:
    python generate_snapshots.py

--- Default vs favor_recall=True comparison (trafilatura 2.3.1) ---

Fixture                | Defaults wc | favor_recall wc | Delta   | Notes
-----------------------|-------------|-----------------|---------|-------
quotes_2.3.1 (A)       | 189         | 192             | +1.6%   | defaults: 8 quotes; favor_recall: 10 quotes, no Tags
books_2.3.1 (C)        | 248         | 248             | 0%      | Identical
webscraper_2.3.1 (B)   | 72          | 72              | 0%      | Identical — cookie banner only
example                | 25          | 25              | 0%      | Identical
govuk_tax              | 483         | 483             | 0%      | Identical — tables survive
wiki_manchester        | 20,052      | 20,052          | 0%      | Identical — tables survive
python_about           | 187         | 200             | +7.0%   | Slightly more content
quotes_static          | 271         | 212             | −21.8%  | Defaults keep more
books_catalogue        | 60          | 198             | +230%   | **favor_recall injects nav/boilerplate**

Decision: keep trafilatura defaults (no favor_recall). The +230% boilerplate
injection on books_catalogue is unacceptable. Quote completeness on A is a
known limitation tracked for follow-up work.
"""

import json
import pathlib
import re
from collections import Counter

import pytest
import trafilatura

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
SNAPSHOTS_DIR = pathlib.Path(__file__).parent / "snapshots"


def _extract(html: str) -> str:
    """Mirror the _extract_content logic in main.py exactly."""
    return trafilatura.extract(
        html,
        output_format="markdown",
        include_links=False,
        include_images=False,
        include_tables=True,
    )


# ---------------------------------------------------------------------------
# Snapshot tests — any change to extraction output fails CI
# ---------------------------------------------------------------------------

FIXTURE_NAMES = [
    "quotes_2.3.1",
    "books_2.3.1",
    "webscraper_2.3.1",
    "example",
    "govuk_tax",
    "wiki_manchester",
    "python_about",
    "quotes_static",
    "books_catalogue",
]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_snapshot_unchanged(name):
    """Extracted output must match the stored snapshot byte-for-byte."""
    html = (FIXTURES_DIR / f"{name}.html").read_text(encoding="utf-8")
    content = _extract(html)

    snap_path = SNAPSHOTS_DIR / f"{name}.json"
    with open(snap_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    assert content == snapshot["content"], (
        f"Extraction output for {name} changed. "
        f"If this is intentional, regenerate snapshots with generate_snapshots.py"
    )
    assert len(content.split()) == snapshot["word_count"]


# ---------------------------------------------------------------------------
# Correctness tests — assertions that currently pass
# ---------------------------------------------------------------------------

class TestBooksCorrectness:
    """Fixture: books.toscrape.com single product page (C)."""

    @pytest.fixture(scope="class")
    @classmethod
    def content(cls):
        html = (FIXTURES_DIR / "books_2.3.1.html").read_text(encoding="utf-8")
        return _extract(html)

    def test_upc_present(self, content):
        assert "a897fe39b1053632" in content

    def test_price_present(self, content):
        assert "\u00a351.77" in content

    def test_no_duplicate_lines(self, content):
        lines = [l for l in content.split("\n") if l.strip()]
        dupes = {l: c for l, c in Counter(lines).items() if c > 1}
        assert not dupes, f"Unexpected duplicated lines: {dupes}"


class TestGovUkCorrectness:
    """Fixture: gov.uk income tax rates page — tables must survive."""

    @pytest.fixture(scope="class")
    @classmethod
    def content(cls):
        html = (FIXTURES_DIR / "govuk_tax.html").read_text(encoding="utf-8")
        return _extract(html)

    def test_table_content_present(self, content):
        rows = [l for l in content.split("\n") if l.startswith("|") and "---" not in l]
        assert len(rows) >= 4, f"Expected >= 4 table rows, got {len(rows)}"

    def test_tax_bands_present(self, content):
        assert "Personal Allowance" in content
        assert "Basic rate" in content
        assert "Higher rate" in content


class TestWikipediaCorrectness:
    """Fixture: Wikipedia Manchester page — infobox and tables must survive."""

    @pytest.fixture(scope="class")
    @classmethod
    def content(cls):
        html = (FIXTURES_DIR / "wiki_manchester.html").read_text(encoding="utf-8")
        return _extract(html)

    def test_tables_present(self, content):
        rows = [l for l in content.split("\n") if l.startswith("|") and "---" not in l]
        assert len(rows) >= 50, f"Expected >= 50 table rows, got {len(rows)}"

    def test_infobox_content(self, content):
        assert "Manchester" in content[:2000]


# ---------------------------------------------------------------------------
# Known-limitation tests — marked xfail, tracked for follow-up work
# ---------------------------------------------------------------------------

class TestQuotesJsLimitation:
    """Fixture: quotes.toscrape.com/js/ rendered via Playwright (A).

    Known limitation: with trafilatura 2.3.1 defaults, only 8 of 10
    quote blocks are extracted. The remaining 2 are dropped.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def html(cls):
        return (FIXTURES_DIR / "quotes_2.3.1.html").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    @classmethod
    def content(cls, html):
        return _extract(html)

    @pytest.mark.xfail(reason="trafilatura 2.3.1 defaults drop 2 of 10 quote blocks — fix in follow-up PR")
    def test_all_quotes_extracted(self, html, content):
        div_quotes = len(re.findall(r'<div class="quote">', html))
        blocks = [b for b in content.split("\n\n") if b.strip()]
        quote_blocks = [b for b in blocks if "\u201c" in b or "\u201d" in b]
        assert len(quote_blocks) == div_quotes, (
            f"Expected {div_quotes} quotes, got {len(quote_blocks)}"
        )

    def test_each_quote_has_author(self, content):
        blocks = [b for b in content.split("\n\n") if b.strip()]
        quote_blocks = [b for b in blocks if "\u201c" in b or "\u201d" in b]
        for i, b in enumerate(quote_blocks):
            assert "by " in b, f"Quote {i + 1} missing author: {b[:100]}"


class TestWebscraperLimitation:
    """Fixture: webscraper.io AJAX e-commerce page (B).

    Known limitation: trafilatura scores the dense cookie-banner text
    higher than the sparse product grid, so only the banner is extracted.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def content(cls):
        html = (FIXTURES_DIR / "webscraper_2.3.1.html").read_text(encoding="utf-8")
        return _extract(html)

    @pytest.mark.xfail(reason="consent banner wins extraction — fix in PR 2")
    def test_product_grid_present(self, content):
        assert any(k in content for k in ["Acer", "Lenovo", "IdeaTab", "$436", "$1149", "$88"])


class TestBooksCatalogueLimitation:
    """Fixture: books.toscrape.com catalogue index page.

    Known limitation: default extraction returns ~60 words, mostly
    boilerplate. Listing content is not reliably extracted.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def content(cls):
        html = (FIXTURES_DIR / "books_catalogue.html").read_text(encoding="utf-8")
        return _extract(html)

    @pytest.mark.xfail(reason="catalogue listing content not extracted — fix in PR 2")
    def test_listing_content_present(self, content):
        assert "A Light in the Attic" in content or "The Black Maria" in content
