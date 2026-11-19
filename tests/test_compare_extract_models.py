"""Offline checks for the extract model comparison script.

The live Anthropic path is mocked. These tests make no paid API calls.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import main

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import compare_extract_models as compare  # noqa: E402


def test_extract_model_is_unchanged():
    assert main.EXTRACT_MODEL == "claude-sonnet-5-5"
    assert compare.PREVIOUS_MODEL == "claude-sonnet-4-6"
    assert compare.model_ids(main) == ["claude-sonnet-5-5", "claude-sonnet-4-6"]


def test_score_fields_normalises_strings_and_counts_misses():
    perfect = compare.score_fields(
        {"title": "Oak Desk Lamp", "price": "£42.00"},
        {"title": " oak   desk lamp ", "price": "£42.00", "extra": "ignored"},
    )
    assert perfect["accuracy"] == 1.0
    assert perfect["fields"]["title"] is True

    partial = compare.score_fields(
        {"title": "Oak Desk Lamp", "price": "£42.00", "availability": "In stock"},
        {"title": "Oak Desk Lamp", "price": "42"},
    )
    assert partial["correct"] == 1
    assert partial["total"] == 3
    assert partial["fields"]["price"] is False
    assert partial["fields"]["availability"] is False
    assert abs(partial["accuracy"] - (1 / 3)) < 1e-9


def test_fixtures_keep_expected_values_in_markdown():
    fixtures = compare.load_fixtures()
    assert len(fixtures) == 4
    assert len(fixtures) * 2 < compare.MAX_CALLS
    for fixture in fixtures:
        content, _title = main._extract_content(fixture["html"])
        assert content and content.strip()
        for value in fixture["expected"].values():
            assert value in content


def test_skips_paid_calls_when_api_key_is_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []

    class Boom:
        def __init__(self, *args, **kwargs):
            called.append(1)

    monkeypatch.setattr(main.httpx, "AsyncClient", Boom)
    out = tmp_path / "report.json"
    code = compare.main_cli(["--output", str(out)])

    assert code == 0
    assert called == []
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "not_run"
    assert report["calls"] == 0
    assert report["planned_calls"] == 8
    assert report["models"] == ["claude-sonnet-5-5", "claude-sonnet-4-6"]


def test_dry_run_makes_no_calls_even_with_a_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    called = []
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda *args, **kwargs: called.append(1),
    )
    out = tmp_path / "report.json"
    code = compare.main_cli(["--dry-run", "--output", str(out)])

    assert code == 0
    assert called == []
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "dry_run"
    assert report["calls"] == 0


def test_mocked_comparison_uses_both_models_under_the_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            calls.append(json["model"])
            prompt = json["messages"][0]["content"]
            assert "Extract these fields" in prompt
            assert json["model"] in {"claude-sonnet-5-5", "claude-sonnet-4-6"}
            expected = next(
                item["expected"]
                for item in compare.load_fixtures()
                if item["url"] in prompt
            )
            text = json_dumps(expected)

            def body():
                return {"content": [{"text": text}]}

            return SimpleNamespace(status_code=200, text=text, json=body)

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    out = tmp_path / "report.json"
    code = compare.main_cli(["--output", str(out)])

    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "compared"
    assert report["calls"] == 8
    assert report["calls"] <= 20
    assert calls.count("claude-sonnet-5-5") == 4
    assert calls.count("claude-sonnet-4-6") == 4
    assert report["summary"]["claude-sonnet-5-5"] == 1.0
    assert report["summary"]["claude-sonnet-4-6"] == 1.0
    assert main.EXTRACT_MODEL == "claude-sonnet-5-5"


def json_dumps(payload):
    return json.dumps(payload)


def test_refuses_to_exceed_the_call_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(compare, "MAX_CALLS", 1)
    called = []
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda *args, **kwargs: called.append(1),
    )
    out = tmp_path / "report.json"
    code = compare.main_cli(["--output", str(out)])

    assert code == 2
    assert called == []
    assert not out.exists()


def test_model_error_is_recorded_without_further_calls(monkeypatch):
    async def boom(*args, **kwargs):
        raise main.HTTPException(status_code=502, detail="Anthropic error: 500 - down")

    monkeypatch.setattr(main, "_extract_structured", boom)
    fixture = compare.load_fixtures()[0]
    scored = asyncio.run(compare.compare_one(main, fixture, "claude-sonnet-5-5"))

    assert scored["called"] is True
    assert scored["accuracy"] == 0.0
    assert "Anthropic error: 500" in scored["error"]
