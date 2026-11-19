#!/usr/bin/env python3
"""Compare two Claude models on fixed /extract fixtures.

Commit 7536247 switched EXTRACT_MODEL from claude-sonnet-4-6 to
claude-sonnet-5-5 with no checked-in eval. This script does not change
that setting. It runs the bundled HTML pages through the same markdown
extraction and ``_extract_structured`` call as POST /extract, once per
model, and writes field accuracy.

No paid API call is made unless ANTHROPIC_API_KEY is set. The bundled
set is 4 pages x 2 models = 8 calls, and the script refuses to exceed 20.

    python scripts/compare_extract_models.py
    python scripts/compare_extract_models.py --output comparison.json
    python scripts/compare_extract_models.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

MAX_CALLS = 20
# Model id in use before commit 7536247. The live default is read from main.
PREVIOUS_MODEL = "claude-sonnet-4-6"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "extract_models"


def prepare_import_env() -> None:
    """Let the script import the app without a production environment."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ.setdefault(
        "ADMIN_SECRET",
        "model-compare-admin-secret-32-characters",
    )
    os.environ.setdefault(
        "DATABASE_URL",
        "sqlite:////tmp/terradeed-model-compare.db",
    )


def _normalise(value):
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def score_fields(expected: dict, actual) -> dict:
    """Score exact field matches after stripping and case-folding strings."""
    found = actual if isinstance(actual, dict) else {}
    fields = {
        key: _normalise(found.get(key)) == _normalise(expected_value)
        for key, expected_value in expected.items()
    }
    total = len(fields)
    correct = sum(1 for ok in fields.values() if ok)
    return {
        "correct": correct,
        "total": total,
        "accuracy": (correct / total) if total else 0.0,
        "fields": fields,
    }


def load_fixtures() -> list[dict]:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    fixtures = []
    for item in manifest:
        html = (FIXTURE_DIR / item["file"]).read_text(encoding="utf-8")
        fixtures.append({**item, "html": html})
    return fixtures


def model_ids(app) -> list[str]:
    chosen = [app.EXTRACT_MODEL]
    if PREVIOUS_MODEL != app.EXTRACT_MODEL:
        chosen.append(PREVIOUS_MODEL)
    return chosen


async def compare_one(app, fixture: dict, model_id: str) -> dict:
    content, _title = app._extract_content(fixture["html"])
    if not content or not str(content).strip():
        return {
            "called": False,
            "error": "fixture produced no markdown",
            "accuracy": 0.0,
            "correct": 0,
            "total": len(fixture["expected"]),
            "fields": {key: False for key in fixture["expected"]},
        }
    try:
        result = await app._extract_structured(
            content,
            fixture["url"],
            list(fixture["expected"]),
            False,
            model=model_id,
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return {
            "called": True,
            "error": str(detail),
            "accuracy": 0.0,
            "correct": 0,
            "total": len(fixture["expected"]),
            "fields": {key: False for key in fixture["expected"]},
        }
    scored = score_fields(fixture["expected"], result.get("data") or {})
    scored["called"] = True
    scored["data"] = result.get("data")
    scored["model"] = result.get("model")
    return scored


async def run_comparison(app, models: list[str]) -> dict:
    fixtures = load_fixtures()
    planned = len(fixtures) * len(models)
    if planned > MAX_CALLS:
        raise RuntimeError(f"Refusing to make {planned} calls; cap is {MAX_CALLS}")
    report = {
        "status": "compared",
        "models": models,
        "calls": 0,
        "max_calls": MAX_CALLS,
        "fixtures": [],
        "summary": {},
    }
    totals = {model_id: [] for model_id in models}
    for fixture in fixtures:
        entry = {"name": fixture["name"], "url": fixture["url"], "models": {}}
        for model_id in models:
            scored = await compare_one(app, fixture, model_id)
            if scored.get("called"):
                report["calls"] += 1
            entry["models"][model_id] = scored
            if scored.get("called") and "error" not in scored:
                totals[model_id].append(scored["accuracy"])
        report["fixtures"].append(entry)
    for model_id, scores in totals.items():
        report["summary"][model_id] = (sum(scores) / len(scores)) if scores else 0.0
    if report["calls"] > MAX_CALLS:
        raise RuntimeError(f"Made {report['calls']} calls; cap is {MAX_CALLS}")
    return report


def skipped_report(models: list[str], *, dry_run: bool) -> dict:
    fixtures = load_fixtures()
    if dry_run:
        reason = "Dry run. No model calls were made."
        status = "dry_run"
    else:
        reason = "ANTHROPIC_API_KEY is not set. No paid model calls were made."
        status = "not_run"
    return {
        "status": status,
        "reason": reason,
        "models": models,
        "fixtures": [item["name"] for item in fixtures],
        "calls": 0,
        "planned_calls": len(fixtures) * len(models),
        "max_calls": MAX_CALLS,
    }


def emit(report: dict, path: str | None) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Write the JSON report to this path. Default: stdout.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and make no model calls.",
    )
    args = parser.parse_args(argv)

    prepare_import_env()
    import main as app

    models = model_ids(app)
    planned = len(load_fixtures()) * len(models)
    if planned > MAX_CALLS:
        print(f"Refusing to make {planned} calls; cap is {MAX_CALLS}", file=sys.stderr)
        return 2

    if args.dry_run or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        emit(skipped_report(models, dry_run=args.dry_run), args.output)
        return 0

    report = asyncio.run(run_comparison(app, models))
    emit(report, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
