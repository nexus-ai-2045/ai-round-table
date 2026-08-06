"""v0.2 relay contract: interface, Tier3 default, Codex Tier1 fallback."""
from roundtable.relay import FallbackRelay, RelayError, get_relay, seats_path
from roundtable.relay_tier3 import Tier3Relay


def test_default_relay_is_tier3():
    r = get_relay("codex", tier=3)
    assert isinstance(r, Tier3Relay)
    assert r.tier == 3


def test_tier1_failure_falls_back_to_tier3():
    """Codex Tier1 が送れない時は Tier3 に縮退し、Tier2 へは昇格しない。"""
    calls = []

    class Boom:
        tier = 1

        def send(self, seat, text):
            raise RelayError("app-server down")

        def poll(self, seat):
            return None

    class CaptureTier3:
        tier = 3

        def send(self, seat, text):
            calls.append(("tier3", seat, text[:20]))
            return "delivered"

        def poll(self, seat):
            return None

    r = FallbackRelay(Boom(), CaptureTier3())
    result = r.send({"participant": "codex"}, "hello packet")
    assert result.startswith("fallback-tier3:")
    assert calls and calls[0][0] == "tier3"
    assert r.tier == 3  # 縮退後は Tier3 として記録


def test_get_relay_tier1_wraps_with_fallback():
    r = get_relay("codex", tier=1, allow_fallback=True)
    assert isinstance(r, FallbackRelay)
    assert r.tier == 1  # 送信前は preferred の tier


def test_never_auto_promotes_to_tier2():
    r = get_relay("codex", tier=2)  # 要求されても Tier3
    assert r.tier == 3
    assert seats_path.__name__ == "seats_path"
