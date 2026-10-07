"""
Regenerate snapshot files from fixtures.

Run this after a deliberate extraction change:
    python tests/generate_snapshots.py

DANGER: this overwrites the golden snapshots. Only run when you have
reviewed the new extraction output and confirmed the changes are correct.
"""

import json
import pathlib

import trafilatura

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
SNAPSHOTS_DIR = pathlib.Path(__file__).parent / "snapshots"


def _extract(html: str) -> str:
    return trafilatura.extract(
        html,
        output_format="markdown",
        include_links=False,
        include_images=False,
        include_tables=True,
    )


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


def main():
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    for name in FIXTURE_NAMES:
        html_path = FIXTURES_DIR / f"{name}.html"
        html = html_path.read_text(encoding="utf-8")
        content = _extract(html)

        snapshot = {
            "fixture": name,
            "trafilatura_version": trafilatura.__version__,
            "word_count": len(content.split()) if content else 0,
            "content": content,
        }

        snap_path = SNAPSHOTS_DIR / f"{name}.json"
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)

        print(f"{name}: word_count={snapshot['word_count']}, snapshot saved")


if __name__ == "__main__":
    main()
