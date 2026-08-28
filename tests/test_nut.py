"""NUT client tests — protocol parsing, value mapping and the fail-safe contract.

Everything runs against tests/nutsim.py, a fake upsd on 127.0.0.1; no hardware, no
network, no NUT installation needed. The engine's trigger/policy/fail-safe behaviour is
already covered by test_basic.py, which injects UpsState directly — what has to be proven
here is that a NUT source produces the *right* UpsState, and never raises.
"""

import asyncio
import socket

import pytest
from pydantic import SecretStr

from app import nut
from app.config import NutConfig

from nutsim import SCENARIOS, FakeUpsd  # tests/ is on sys.path under pytest


def _cfg(port: int, **kw) -> NutConfig:
    kw.setdefault("timeout_s", 2.0)
    return NutConfig(id="u", host="127.0.0.1", port=port, ups_name="ups", **kw)


# --- value mapping ----------------------------------------------------------
async def test_poll_maps_mains_scenario():
    async with FakeUpsd(SCENARIOS["mains"]) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert state.reachable and state.error is None
    assert state.power_source == "mains"
    assert not state.on_battery
    assert state.battery_status == "normal"
    assert state.battery_charge_pct == 100
    assert state.runtime_remaining_min == 42          # 2520 s, rounded down
    assert state.seconds_on_battery is None           # NUT has no such variable
    assert state.manufacturer == "ACME"
    assert state.model == "Smart-UPS 1500"


async def test_poll_maps_outage_and_low_battery():
    async with FakeUpsd(SCENARIOS["battery"]) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert state.on_battery and state.battery_status == "normal"
    assert state.battery_charge_pct == 82
    assert state.runtime_remaining_min == 18

    async with FakeUpsd(SCENARIOS["low"]) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert state.on_battery and state.battery_low


@pytest.mark.parametrize(
    "status, source, low",
    [
        ("OL", "mains", False),
        ("OL CHRG", "mains", False),
        ("OL TRIM", "mains", False),          # voltage trim is still mains
        ("OL BOOST", "mains", False),
        ("OB", "battery", False),
        ("OB LB", "battery", True),
        ("OL BYPASS", "bypass", False),       # load unprotected, not normal operation
        ("OFF", "none", False),
        ("OL FSD", "mains", True),            # another NUT master forced a shutdown
        ("RB", "unknown", False),             # replace battery says nothing about power
    ],
)
async def test_status_flags_are_mapped(status, source, low):
    async with FakeUpsd({"ups.status": status}) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert state.power_source == source
    assert state.battery_low is low


async def test_missing_values_stay_none_instead_of_zero():
    """A driver without runtime/charge must skip those thresholds, not report 0."""
    async with FakeUpsd(SCENARIOS["sparse"]) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert state.reachable
    assert state.runtime_remaining_min is None
    assert state.battery_charge_pct is None


async def test_legacy_variable_names_are_accepted():
    async with FakeUpsd({"ups.status": "OL", "ups.mfr": "Old", "ups.model": "9000"}) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert (state.manufacturer, state.model) == ("Old", "9000")


async def test_quoted_values_with_spaces_and_escapes():
    variables = {"ups.status": "OL", "device.model": 'Back-UPS \\"XS\\" 700'}
    async with FakeUpsd(variables) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert state.model == 'Back-UPS "XS" 700'


# --- fail safe --------------------------------------------------------------
async def test_stale_data_is_unreachable_not_mains():
    """upsd keeps serving old values when its driver dies — that must not look healthy."""
    async with FakeUpsd(SCENARIOS["mains"], error="DATA-STALE") as srv:
        state = await nut.poll(_cfg(srv.port))
    assert not state.reachable
    assert state.power_source == "unknown"
    assert "stale" in state.error.lower()


async def test_driver_not_connected_is_unreachable():
    async with FakeUpsd(error="DRIVER-NOT-CONNECTED") as srv:
        state = await nut.poll(_cfg(srv.port))
    assert not state.reachable and state.error


async def test_missing_status_variable_is_unreachable():
    """Without ups.status we cannot tell mains from battery — never report "not on battery"."""
    async with FakeUpsd({"battery.charge": "100"}) as srv:
        state = await nut.poll(_cfg(srv.port))
    assert not state.reachable
    assert "ups.status" in state.error


async def test_unknown_ups_name_is_reported():
    async with FakeUpsd(ups_name="other") as srv:
        state = await nut.poll(_cfg(srv.port))
    assert not state.reachable
    assert "does not know" in state.error


async def test_wrong_credentials_are_reported():
    async with FakeUpsd(username="monitor", password="secret") as srv:
        cfg = _cfg(srv.port, username="monitor", password=SecretStr("wrong"))
        state = await nut.poll(cfg)
    assert not state.reachable
    assert "denied" in state.error
    assert "wrong" not in state.error  # the password must never leak into a message


async def test_correct_credentials_authenticate():
    async with FakeUpsd(SCENARIOS["mains"], username="monitor", password="secret") as srv:
        cfg = _cfg(srv.port, username="monitor", password=SecretStr("secret"))
        state = await nut.poll(cfg)
    assert state.reachable and state.power_source == "mains"


async def test_unconfigured_source_never_polls():
    state = await nut.poll(NutConfig())
    assert not state.reachable and state.error == "NUT server not configured"


async def test_unreachable_server_never_raises():
    # Nothing listens on port 1; the poller must turn that into a state, not an exception.
    state = await nut.poll(_cfg(1, timeout_s=0.5))
    assert not state.reachable and state.error


def test_transport_failures_get_our_own_english_text():
    """The OS message is localised and vague; ours must be English and name the cause.

    Tested on the mapping rather than a live socket: whether a closed port answers with
    a refusal or a timeout is up to the OS, and CI must not depend on that.
    """
    cfg = _cfg(3493)
    refused = nut._failure_text(cfg, ConnectionRefusedError(61, "irrelevant OS text"))
    assert "Connection refused by 127.0.0.1:3493" in refused
    assert "LISTEN" in refused  # points at the actual second cause

    unresolved = nut._failure_text(
        NutConfig(host="no-such-host.invalid", ups_name="ups"), socket.gaierror(-2, "x")
    )
    assert "no-such-host.invalid" in unresolved

    assert "closed the connection" in nut._failure_text(cfg, ConnectionResetError())
    assert "within 2 s" in nut._failure_text(cfg, asyncio.TimeoutError())
    # upsd's own error codes keep their protocol-level wording.
    assert "stale" in nut._failure_text(cfg, nut._NutError("DATA-STALE"))


async def test_server_closing_mid_list_never_raises():
    """A upsd that dies half way through the response must not crash the poll loop."""

    async def rude(reader, writer):
        await reader.readline()
        writer.write(b'BEGIN LIST VAR ups\nVAR ups ups.status "OL"\n')
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(rude, "127.0.0.1", 0)
    try:
        state = await nut.poll(_cfg(server.sockets[0].getsockname()[1]))
    finally:
        server.close()
        await server.wait_closed()
    assert not state.reachable and state.error


async def test_timeout_is_bounded_by_the_configured_value():
    """A server that accepts the connection and then says nothing must not hang the loop."""

    async def silent(reader, writer):
        await asyncio.sleep(30)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    try:
        cfg = _cfg(server.sockets[0].getsockname()[1], timeout_s=0.5)
        state = await asyncio.wait_for(nut.poll(cfg), timeout=10)
    finally:
        server.close()
        await server.wait_closed()
    assert not state.reachable and "No response" in state.error


# --- probe (manual test button) --------------------------------------------
async def test_probe_lists_every_variable():
    async with FakeUpsd(SCENARIOS["mains"]) as srv:
        result = await nut.probe(_cfg(srv.port))
    assert result.reachable
    assert result.ok_count == result.total == 6
    assert not result.missing_triggers
    assert {e.status for e in result.entries} == {"ok"}
    status = next(e for e in result.entries if e.name == "ups.status")
    assert status.raw == "OL" and "mains" in status.value


async def test_probe_names_the_triggers_the_device_cannot_feed():
    async with FakeUpsd(SCENARIOS["sparse"]) as srv:
        result = await nut.probe(_cfg(srv.port))
    assert result.reachable
    assert result.missing_triggers == ["runtime", "charge"]
    assert [e.status for e in result.entries if e.name == "battery.charge"] == ["missing"]
    assert "Not provided by this driver" in result.summary


async def test_probe_marks_stale_data_separately():
    async with FakeUpsd(error="DATA-STALE") as srv:
        result = await nut.probe(_cfg(srv.port))
    assert not result.reachable
    assert {e.status for e in result.entries} == {"stale"}


async def test_probe_summary_never_contains_the_password():
    async with FakeUpsd(username="monitor", password="hunter2") as srv:
        cfg = _cfg(srv.port, username="monitor", password=SecretStr("hunter2"))
        result = await nut.probe(cfg)
    assert "hunter2" not in result.summary
    assert all("hunter2" not in (e.error or "") for e in result.entries)


async def test_probe_of_unconfigured_source_skips_everything():
    result = await nut.probe(NutConfig())
    assert not result.reachable
    assert {e.status for e in result.entries} == {"skipped"}


# --- statuses stay inside the declared set ---------------------------------
async def test_probe_only_emits_declared_statuses():
    """The UI labels statuses via probe.st.<status>; an undeclared one would show raw."""
    from app.ups import PROBE_STATUSES, PROBE_TRIGGERS

    seen = set()
    cases = [
        FakeUpsd(SCENARIOS["mains"]),
        FakeUpsd(SCENARIOS["sparse"]),
        FakeUpsd(error="DATA-STALE"),
        FakeUpsd(ups_name="other"),
    ]
    for server in cases:
        async with server as srv:
            result = await nut.probe(_cfg(srv.port))
        seen.update(e.status for e in result.entries)
        assert set(result.missing_triggers) <= set(PROBE_TRIGGERS)
    seen.update(e.status for e in (await nut.probe(NutConfig())).entries)
    assert seen <= set(PROBE_STATUSES), f"undeclared probe statuses: {seen - set(PROBE_STATUSES)}"
