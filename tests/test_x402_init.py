"""One facilitator outage must not stop the API from starting."""

from types import SimpleNamespace

import main


class _Client:
    def __init__(self, name, ok):
        self.identifier = name
        self.ok = ok

    def get_supported(self):
        if not self.ok:
            raise ValueError("Facilitator get_supported failed (503)")
        return SimpleNamespace(kinds=[])


def test_healthy_facilitators_drops_the_failing_one():
    up = _Client("cdp", True)
    down = _Client("xpay", False)
    assert main._healthy_facilitators([up, down]) == [up]


def test_initialise_reports_false_when_none_reachable(monkeypatch):
    monkeypatch.setattr(main, "ALL_FACILITATOR_CLIENTS", [_Client("xpay", False)])
    server = SimpleNamespace(_initialized=False)
    assert main._initialise_x402(server) is False


def test_ensure_ready_is_rate_limited(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "x402_server", SimpleNamespace(_initialized=False))
    monkeypatch.setattr(main, "_initialise_x402", lambda server: calls.append(server) or False)
    import time
    monkeypatch.setattr(main, "_x402_last_init_attempt", time.monotonic())
    main._ensure_x402_ready()
    assert calls == []
    monkeypatch.setattr(main, "_x402_last_init_attempt", time.monotonic() - 120)
    main._ensure_x402_ready()
    assert len(calls) == 1
