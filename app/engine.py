"""Decision engine and background poll loop.

Per-UPS state machine (each configured UPS is evaluated independently)::

    ONLINE  ->  ON_BATTERY  ->  (UPS triggered)

A host is shut down based on *its* feeding UPS devices (``HostConfig.ups_ids``) and
its policy (``ups_policy``): ``"all"`` (redundant PSUs — shut down only when every
feed has triggered, the default) or ``"any"`` (shut down as soon as one feed
triggers). A return to mains on any required feed aborts a not-yet-committed shutdown
(hysteresis, no flapping). An unreachable UPS raises an alarm but never triggers a
shutdown on its own (fail safe); a trigger already fired on fresh data, however, stays
latched while the UPS is unreachable (blind = never downgrade). When ``dry_run`` is set, the engine logs what it
*would* do and latches per host until power returns, instead of actually shutting
hosts down.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from itertools import groupby

from . import __version__, cluster, db, notify, targets
from .config import AppConfig, HostConfig, PveHostConfig, Thresholds, UpsBase
from .sources import poll
from .ups import UpsState

log = logging.getLogger("pve-usv.engine")

ONLINE = "ONLINE"
ON_BATTERY = "ON_BATTERY"
SHUTDOWN_PENDING = "SHUTDOWN_PENDING"
SHUTTING_DOWN = "SHUTTING_DOWN"

# Per-UPS battery timers survive a service restart via this file (next to events.db).
# Without it, a restart during a "blind" outage (on battery, then contact lost) would drop
# the running countdown and never shut down — even though the outage was confirmed.
STATE_PATH = db.DB_PATH.parent / "engine-state.json"
STATE_RESTORE_MAX_AGE_H = 24  # discard persisted timers older than this


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    """Naive *local* wall clock, kept separate from _now().

    The self-test schedule is a wall-clock time the user configures, so it has to follow
    the container's timezone; durations and timestamps stay UTC (_now()). Also the single
    seam the schedule tests patch.
    """
    return datetime.now()


def _hostname() -> str:
    """This appliance's host name, for matching it against the cluster's guest list.

    Total by construction: a name is a convenience here (the guest is normally picked
    explicitly in the settings), so every way of not getting one ends in an empty string
    rather than an exception on the poll loop.
    """
    try:
        name = socket.gethostname()
    except Exception:  # noqa: BLE001
        name = ""
    if not name:
        try:
            name = open("/etc/hostname", encoding="utf-8").read()
        except Exception:  # noqa: BLE001
            name = ""
    return name.strip()


def shutdown_budget_s(th: Thresholds) -> int:
    """Worst-case seconds from "trigger fires" to "the last node was told to go".

    Pure, so the self-test warning and the UI hint quote the same number. The guest stop
    is by far the largest term, which is exactly why it has to be visible next to the
    battery reserve.
    """
    return (
        int(th.cluster_prep_timeout_s)
        + int(th.cluster_guest_shutdown_timeout_s)
        + int(th.host_shutdown_timeout_s)
    )


def _prep_intent(wanted: bool, effective: bool, why_not: str) -> str:
    """One cluster preparation step, as the event log should read.

    Three states, not two: switched off, switched on and going to run, or switched on but
    skipped because this cluster cannot do it. Collapsing the last two into "no" is what
    used to make a cluster without Ceph look like a configuration mistake.
    """
    if not wanted:
        return "off"
    return "yes" if effective else f"skipped ({why_not})"


def selftest_slot(now_local: datetime, hour: int, interval_min: int) -> datetime:
    """Start of the self-test slot ``now_local`` falls into (naive local time).

    The grid is anchored at ``hour``:00 and repeats every ``interval_min`` minutes. Every
    selectable interval divides 1440 evenly, so the grid is the same on every day and
    wraps cleanly across midnight (anchor 23:00, every 2 h -> 23:00, 01:00, 03:00, ...).
    Total by construction: a nonsensical hour or interval degrades to a daily grid rather
    than raising, because this runs inside the poll loop.

    DST is deliberately handled by doing nothing: in spring the slots inside the skipped
    hour simply do not occur that day, in autumn the repeated hour yields the same slot
    value twice and the caller's latch swallows the second run.
    """
    step = interval_min if interval_min and interval_min > 0 else 1440
    anchor = (hour % 24) * 60
    minute_of_day = now_local.hour * 60 + now_local.minute
    elapsed = (minute_of_day - anchor) % step
    return now_local.replace(second=0, microsecond=0) - timedelta(minutes=elapsed)


@dataclass
class _UpsRuntime:
    """Per-UPS runtime state (one instance per configured UPS, keyed by the UPS id)."""

    state: UpsState = field(default_factory=UpsState)
    on_battery_since: Optional[datetime] = None
    unreachable_count: int = 0
    unreachable_since: Optional[datetime] = None  # wall clock, for the comms-loss opt-in
    alarm_active: bool = False
    last_reachable: Optional[bool] = None  # for connect/disconnect logging
    # True once a connection LOSS was actually notified (i.e. the unreachable alarm fired).
    # "Connection restored" is only worth a notification if the loss was one too — otherwise
    # a single dropped SNMP packet would produce a lost/restored pair out of nowhere.
    loss_notified: bool = False
    comm_loss_fired: bool = False  # latch for the optional comms-loss alarm wording
    triggered: bool = False  # this UPS currently demands a shutdown of its hosts
    trigger_reason: Optional[str] = None


class Engine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.started_at = _now()
        self.state = ONLINE

        # Per-UPS runtime, keyed by the UPS id.
        self.ups_rt: dict[str, _UpsRuntime] = {}
        self._sync_runtimes()

        # Self-test scheduling, on two clocks on purpose:
        #   last_selftest_slot: naive LOCAL grid position -> the schedule latch (persisted)
        #   last_selftest_at:   UTC timestamp of the actual run -> REST API only
        # Declared before _restore_state() below, which re-arms the slot from the state
        # file — assigning them afterwards would wipe the restored value.
        self.last_selftest_slot: Optional[datetime] = None
        self.last_selftest_at: Optional[datetime] = None
        self.last_selftest_ok: Optional[bool] = None
        # Date (naive local) on which a successful run was last written to the event log.
        self.last_selftest_ok_logged: Optional[date] = None

        # Re-arm battery timers + latched triggers from the state file (None = nothing
        # written yet, so the first _evaluate always persists and clears a stale file).
        self._persisted_state: Optional[dict] = None
        self._restore_state()

        # Aggregate shutdown bookkeeping (any host fired this episode).
        self.shutdown_triggered = False
        self.shutdown_reason: Optional[str] = None
        self.triggered_at: Optional[datetime] = None

        # Per-host latch: do not re-fire while still pending. host_states holds the
        # committed (real) shutdown result and the last self-test result per host.
        # Both are keyed by HostConfig.key — the host's stable id, never anything the
        # user edits: correcting a node name would otherwise discard the self-test result
        # and the latch, and two entries sharing a type and a name (a duplicated row with
        # the IP not adjusted) would share one latch, leaving the second never fired.
        self.host_fired: dict[str, bool] = {}
        self.host_states: dict[str, dict] = {}
        # Since when every UPS has been back on mains, for the automatic re-arm (see
        # _maybe_rearm). Runtime only: a restart clears the latches it guards anyway.
        self._mains_ok_since: Optional[datetime] = None

        # Cluster awareness, all runtime-only (nothing here belongs in the config):
        # last inspection per cluster name, the "prepared this episode" latch, the
        # outcome of that preparation, and the names already warned about a missing
        # disarm-ha endpoint.
        self.cluster_states: dict[str, "cluster.ClusterInfo"] = {}
        self.cluster_prepared: dict[str, bool] = {}
        # Clusters whose preparation FAILED this episode. Separate from the latch above,
        # because the two answer different questions: "was it attempted" decides whether
        # to attempt it again, "did it work" decides whether the nodes may go down under
        # ``cluster_abort_on_prep_failure``. Deriving the second from the work of a single
        # iteration made the opt-in last exactly one poll.
        self.cluster_prep_failed: dict[str, bool] = {}
        # What the last preparation of this episode actually did, per cluster, for the
        # dashboard and the shutdown preview. Per-episode, so it is cleared with the
        # latches above.
        self.cluster_prep_steps: dict[str, list[str]] = {}
        # Guest facts discovered by the last inspection, per cluster name. A discovered
        # fact like cluster_states, so it survives the episode teardown.
        self.cluster_guest_state: dict[str, dict] = {}
        # Hosts pulled into this episode's shutdown because their cluster goes down as
        # a unit. They have no trigger reason of their own, so without this they would
        # look "no longer eligible" on the next poll and have their latch released.
        self.cluster_unit_hosts: set[str] = set()
        self._disarm_unsupported_warned: set[str] = set()
        # Set by the automatic re-arm, consumed by _maybe_selftest: right after a
        # re-arm is exactly when a leftover problem (an expired token, a cluster still
        # prepared, a node that never came back) should surface, rather than hours later
        # at the next scheduled slot.
        self._rearm_selftest_pending = False
        # One inspection per process start, independent of the self-test schedule — see
        # _maybe_cluster_startup_check(). Deliberately not persisted.
        self._cluster_startup_done = False
        self._node_startup_done = False
        # API URLs already warned about. Both the startup node check and the scheduled
        # self-test report them, and on a process start with a due slot both run in the
        # same iteration — the operator would read the identical warning twice.
        self._dup_url_warned: set[str] = set()

        # Daily housekeeping: keep the event log bounded.
        self.last_prune_date = None  # type: ignore[var-annotated]

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # -- runtime/config sync -------------------------------------------------
    def _sync_runtimes(self) -> None:
        """Ensure there is exactly one _UpsRuntime per configured UPS id."""
        ids = {u.id for u in self.cfg.ups if u.id}
        for uid in ids:
            self.ups_rt.setdefault(uid, _UpsRuntime())
        for uid in list(self.ups_rt):
            if uid not in ids:
                del self.ups_rt[uid]

    # -- battery-timer persistence (survives service restarts) ---------------
    def _restore_state(self) -> None:
        """Best-effort: re-arm per-UPS battery timers from the state file at startup."""
        try:
            if not STATE_PATH.exists():
                return
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            entries = data.get("on_battery_since", {})
            triggers = data.get("trigger_reason", {})
            now = _now()
            for uid, ts in entries.items():
                rt = self.ups_rt.get(uid)
                if rt is None or not isinstance(ts, str):
                    continue
                try:
                    since = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if since.tzinfo is None or since > now:
                    continue
                if now - since > timedelta(hours=STATE_RESTORE_MAX_AGE_H):
                    continue
                rt.on_battery_since = since
                # A latched trigger (e.g. battery low before the restart) is re-armed
                # together with its timer, so the restart cannot demote it back to the
                # remaining countdown.
                reason = triggers.get(uid)
                if isinstance(reason, str) and reason:
                    rt.triggered = True
                    rt.trigger_reason = reason
                self._log_quiet(
                    "On-battery timer restored",
                    f"UPS {uid}: on battery since {ts} (from state file after restart). "
                    + (
                        f"Latched trigger persists: {reason}."
                        if rt.triggered
                        else "The countdown continues."
                    ),
                    db.WARNING,
                )
            self._restore_selftest_slot(data.get("selftest_slot"))
            self._restore_selftest_outcome(data.get("selftest_at"), data.get("selftest_ok"))
        except Exception as exc:  # noqa: BLE001 - a broken state file must never block startup
            log.warning("Engine state restore failed: %s", exc)

    def _restore_selftest_slot(self, raw) -> None:
        """Re-arm the self-test latch, so a restart does not re-run the test immediately.

        Written as a naive *local* timestamp (unlike the UTC values above). A value in the
        future can only come from a backwards clock jump (NTP correction, timezone change)
        and is dropped — keeping it would block self-tests until the clock caught up.
        """
        if not isinstance(raw, str):
            return
        try:
            slot = datetime.fromisoformat(raw)
        except ValueError:
            return
        if slot.tzinfo is None and slot <= _local_now() + timedelta(minutes=1):
            self.last_selftest_slot = slot

    def _restore_selftest_outcome(self, raw_at, raw_ok) -> None:
        """Carry the last result over a restart, so /api/status keeps reporting it."""
        if not isinstance(raw_at, str):
            return
        try:
            when = datetime.fromisoformat(raw_at)
        except ValueError:
            return
        if when.tzinfo is None:  # ours is UTC-aware; anything else is not our value
            return
        self.last_selftest_at = when
        self.last_selftest_ok = raw_ok if isinstance(raw_ok, bool) else None

    def _persist_state(self) -> None:
        """Best-effort: write the per-UPS battery timers + latched triggers on change."""
        current = {
            "on_battery_since": {
                uid: rt.on_battery_since.isoformat()
                for uid, rt in self.ups_rt.items()
                if rt.on_battery_since is not None
            },
            # Only triggers tied to a running battery timer are worth restoring: a pure
            # comms-loss trigger re-arms from unreachable_since, which a restart resets.
            "trigger_reason": {
                uid: rt.trigger_reason
                for uid, rt in self.ups_rt.items()
                if rt.triggered and rt.trigger_reason and rt.on_battery_since is not None
            },
        }
        # Naive LOCAL timestamp, unlike everything above — the self-test grid is
        # wall-clock based. Key absent = the test has not run yet on this instance.
        if self.last_selftest_slot is not None:
            current["selftest_slot"] = self.last_selftest_slot.isoformat()
        # The outcome travels with the latch: since a restart no longer re-runs the test,
        # /api/status would otherwise report "never tested" until the next slot.
        if self.last_selftest_at is not None:
            current["selftest_at"] = self.last_selftest_at.isoformat()
            current["selftest_ok"] = self.last_selftest_ok
        if current == self._persisted_state:
            return
        try:
            tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
            tmp.write_text(json.dumps(current), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
            self._persisted_state = current
        except Exception as exc:  # noqa: BLE001 - persistence must never affect the loop
            log.warning("Engine state persist failed: %s", exc)

    @property
    def alarm_active(self) -> bool:
        """Aggregate alarm: True if any UPS is in alarm state."""
        return any(rt.alarm_active for rt in self.ups_rt.values())

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="pve-usv-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def update_config(self, cfg: AppConfig) -> None:
        """Apply a new config (from the UI) without restarting the loop.

        The self-test latch is deliberately kept: saving settings must not fire a
        credential check, and a changed schedule takes effect at its next slot.

        The node-name check is the exception, and re-armed here. What makes the credential
        test too expensive to repeat on every save — up to 10 s per host — does not apply
        to one GET /nodes, and a node name saved without ever pressing "Test" is exactly
        the mistake that used to stay hidden until an outage.
        """
        self.cfg = cfg
        self._node_startup_done = False
        # A changed host list is a new question: say it again for whatever is wrong now.
        self._dup_url_warned = set()
        self._sync_runtimes()

    def _release_shutdown_latches(self) -> None:
        """Un-latch the shutdown episode: the appliance is ready for the next outage.

        Shared by the manual reset and the automatic re-arm, so both leave exactly the
        same state behind.

        Only the shutdown keys are dropped from ``host_states`` — the self-test verdicts
        and the node-name check live in the same dict and answer a different question.
        Clearing the whole dict (which the manual reset used to do) made /api/health
        report "never tested" for every host until the next scheduled slot, hours away.
        """
        self.shutdown_triggered = False
        self.shutdown_reason = None
        self.triggered_at = None
        self.host_fired = {}
        for st in self.host_states.values():
            for key in ("shutdown_state", "last_action_at", "last_error",
                        "reachable", "this_host", "order"):
                st.pop(key, None)
        # Not the discovered cluster facts (those are refreshed by the self-test), only
        # the per-episode latches: a new outage must prepare again, and a preparation
        # that failed last time must not keep holding its nodes back. What was written to
        # the cluster stays written until someone presses "Restore cluster".
        self.cluster_prepared = {}
        self.cluster_prep_failed = {}
        self.cluster_prep_steps = {}
        self.cluster_unit_hosts = set()
        self._mains_ok_since = None

    def reset(self) -> None:
        """Clear shutdown latches and alarms — the dashboard's "Reset state" button."""
        self._release_shutdown_latches()
        for rt in self.ups_rt.values():
            rt.alarm_active = False
            rt.loss_notified = False
            rt.comm_loss_fired = False
            rt.triggered = False
            rt.trigger_reason = None
        self._recompute_state()

    # -- main loop -----------------------------------------------------------
    async def _loop(self) -> None:
        log.info("Engine started (dry_run=%s)", self.cfg.dry_run)
        while not self._stop.is_set():
            try:
                self._sync_runtimes()
                # Freeze the UPS list for this iteration: a config save during the awaited
                # polls may swap self.cfg, and zipping against the NEW list would assign
                # results to the wrong UPS ids.
                ups_list = list(self.cfg.ups)
                if ups_list:
                    results = await asyncio.gather(*(poll(u) for u in ups_list))
                    for u, st in zip(ups_list, results):
                        rt = self.ups_rt.get(u.id)
                        if rt is not None:
                            rt.state = st
                await self._evaluate()
                await self._maybe_cluster_startup_check()
                await self._maybe_node_startup_check()
                await self._maybe_selftest()
                self._maybe_prune()
            except Exception as exc:  # noqa: BLE001
                log.exception("Engine iteration failed: %s", exc)

            # Keep the fast battery cadence while we believe ANY UPS is on battery — even if
            # one just went blind (unreachable) mid-outage — so the countdown stays responsive.
            on_battery_now = any(
                rt.state.on_battery or rt.on_battery_since is not None
                for rt in self.ups_rt.values()
            )
            interval = (
                self.cfg.thresholds.poll_interval_battery_s
                if on_battery_now
                else self.cfg.thresholds.poll_interval_normal_s
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # -- evaluation ----------------------------------------------------------
    async def _evaluate(self) -> None:
        self._sync_runtimes()

        # Phase A: evaluate every UPS independently.
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is not None:
                await self._evaluate_ups(u, rt)

        # Phase A2: release last episode's latches once mains have proven to be back.
        # Before phase B, so the very same iteration can act on a fresh outage.
        await self._maybe_rearm()

        # Phase B + C: per-host eligibility and shutdown execution.
        await self._evaluate_hosts()

        self._recompute_state()
        self._persist_state()

    async def _evaluate_ups(self, u: UpsBase, rt: _UpsRuntime) -> None:
        """Per-UPS state machine: connectivity logging, fail-safe alarm, battery timer,
        and the resulting ``rt.triggered`` / ``rt.trigger_reason``."""
        th = self.cfg.effective_thresholds(u)
        st = rt.state
        name = u.label

        # --- log every connectivity change (the first poll, None, is silent) -----
        # Both directions are logged, but only NOTIFIED once the loss has proven to be
        # more than a blip: a single dropped SNMP packet used to fire a warning-level
        # webhook here, ahead of (and in addition to) the unreachable alarm below, which
        # is the one that honours ``unreachable_alarm_after_polls``. The event log keeps
        # every transition, so diagnostics stay complete either way.
        if rt.last_reachable is not None and st.reachable != rt.last_reachable:
            if st.reachable:
                subject = f"{name}: network connection restored"
                body = "The UPS is answering again."
                if rt.loss_notified:
                    await self._emit(subject, body, db.INFO)
                else:
                    self._log_quiet(subject, body, db.INFO)
            else:
                self._log_quiet(
                    f"{name}: network connection lost",
                    f"No response from the UPS ({st.error or 'timeout'}).",
                    db.WARNING,
                )
        rt.last_reachable = st.reachable

        # --- unreachable handling: alarm only (fail safe) -----------------------
        if not st.reachable:
            rt.unreachable_count += 1
            if rt.unreachable_since is None:
                rt.unreachable_since = _now()

            # A trigger fired on fresh data (battery low / charge / runtime) stays latched
            # while blind — the battery only drains further, so never downgrade it to the
            # time countdown. Otherwise recompute so a blind on-battery countdown / armed
            # comms-loss opt-in can still arm this UPS. Cleared only on fresh data again
            # (mains return or a reachable re-evaluation).
            if not rt.triggered:
                rt.trigger_reason = self._ups_trigger_reason(u, rt)
                rt.triggered = rt.trigger_reason is not None

            # A confirmed on-battery outage whose time-based countdown survives a comms loss.
            shutdown_pending_blind = (
                th.keep_shutdown_on_comm_loss
                and th.on_battery_seconds is not None
                and rt.on_battery_since is not None
            )

            if rt.unreachable_count >= th.unreachable_alarm_after_polls and not rt.alarm_active:
                rt.alarm_active = True
                # This IS the connection-loss notification (see the transition log above):
                # it only fires once the loss has persisted for the configured number of
                # polls, and it licenses the matching "restored" notification later.
                rt.loss_notified = True
                if rt.triggered:
                    await self._emit(
                        f"{name} unreachable — trigger stays latched",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). The already fired trigger persists: "
                        f"{rt.trigger_reason}.",
                        db.WARNING,
                    )
                elif shutdown_pending_blind:
                    remaining = self._ups_countdown_remaining_s(u, rt)
                    await self._emit(
                        f"{name} unreachable — on-battery countdown continues",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). The on-battery countdown keeps running "
                        f"on the local clock — shutdown when it expires"
                        + (f" (~{remaining} s)." if remaining is not None else "."),
                        db.WARNING,
                    )
                elif th.comm_loss_shutdown_after_min is not None:
                    await self._emit(
                        f"{name} unreachable — shutdown on prolonged loss",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). No power outage confirmed, but if the "
                        f"communication loss persists, a shutdown will be triggered after "
                        f"{th.comm_loss_shutdown_after_min} min.",
                        db.WARNING,
                    )
                else:
                    await self._emit(
                        f"{name} unreachable",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). NO shutdown will be triggered.",
                        db.WARNING,
                    )

            # Latch the opt-in comms-loss event once it has armed the trigger.
            if (
                th.comm_loss_shutdown_after_min is not None
                and rt.triggered
                and not rt.comm_loss_fired
            ):
                rt.comm_loss_fired = True
            return

        # Reachable again — clear alarm/comms-loss latches. Runs *after* the transition
        # log above, so clearing loss_notified here cannot swallow the "restored" notice
        # for a loss that was actually reported.
        rt.alarm_active = False
        rt.loss_notified = False
        rt.comm_loss_fired = False
        rt.unreachable_count = 0
        rt.unreachable_since = None

        # --- power restored -----------------------------------------------------
        if not st.on_battery:
            if rt.on_battery_since is not None or rt.triggered:
                await self._emit(
                    f"{name}: mains power restored",
                    "UPS is back on mains power.",
                    db.INFO,
                )
            rt.on_battery_since = None
            rt.triggered = False
            rt.trigger_reason = None
            return

        # --- on battery ---------------------------------------------------------
        if rt.on_battery_since is None:
            rt.on_battery_since = _now()
            await self._emit(
                f"Power outage — {name} on battery",
                f"Runtime remaining ~{st.runtime_remaining_min} min, "
                f"charge {st.battery_charge_pct}%.",
                db.WARNING,
            )

        rt.trigger_reason = self._ups_trigger_reason(u, rt)
        rt.triggered = rt.trigger_reason is not None

    async def _evaluate_hosts(self) -> None:
        """Per-host eligibility (Phase B) and shutdown execution (Phase C).

        Eligible, not-yet-fired hosts are processed in ``ordered_hosts()`` order, which
        places the appliance's own host last — so "own host last" holds within the
        current batch automatically.
        """
        eligible: list[tuple[HostConfig, str]] = []
        for host in self.cfg.ordered_hosts():
            reason = self._host_trigger_reason(host)
            committed = self.host_states.get(host.key, {}).get("shutdown_state") in (
                "sent",
                "failed",
            )

            if reason is None:
                # A host its cluster took down as a unit never had a reason of its own, so
                # "no reason now" says nothing about it. Without this the latch would be
                # released on the very next poll — in dry-run once per host, with a
                # misleading "shutdown aborted", and it could never fire again because the
                # preparation latch is already set.
                if host.key in self.cluster_unit_hosts:
                    continue
                # No longer eligible (a required feed recovered): release a not-yet-committed
                # (dry-run) latch so the dashboard can recover. A real, sent shutdown stays.
                if self.host_fired.get(host.key) and not committed:
                    self.host_fired[host.key] = False
                    await self._emit(
                        f"Host {host.name}: shutdown aborted",
                        "Feeding UPS device(s) sufficient again — shutdown no longer needed.",
                        # A withdrawn shutdown is not routine: warning, so it also passes
                        # the webhook's default severity filter.
                        db.WARNING,
                    )
                continue

            if self.host_fired.get(host.key):
                continue  # already fired this episode
            eligible.append((host, reason))

        # Prepare each affected cluster exactly once, BEFORE the first node of it goes
        # down. Returns the hosts that may still be shut down (all of them unless the
        # user opted into aborting on a failed preparation).
        #
        # Kept from before that filtering: a cluster held back leaves nothing to fire and
        # nothing latched, which would otherwise read as "the episode is over" at the end
        # of this method — clearing the preparation latch and running the whole failed
        # sequence again on the next poll, every 8 s, with a CRITICAL pair each time.
        had_eligible = bool(eligible)
        if eligible:
            eligible = await self._prepare_clusters(eligible)

        # Fire in stages: hosts sharing an ``order`` go at once, and ``this_host`` forms
        # the final stage on its own. Concurrency within a stage is what keeps one target
        # that stops responding from delaying its peers — and, because every stage is
        # awaited inside the poll loop, from stalling the battery countdown as well.
        # ``eligible`` is already sorted by (this_host, order, name), so the groups are
        # contiguous; targets.shutdown() carries a hard deadline, so no stage can outlast it.
        for _, group in groupby(eligible, key=lambda hr: (hr[0].this_host, hr[0].order)):
            await asyncio.gather(*(self._fire_host(h, r) for h, r in group))

        # Clear the aggregate once nothing is pending, committed or still due. The last
        # condition matters for a cluster that is being held back: no host of it is
        # latched, yet the outage is very much still running.
        if not any(self.host_fired.values()) and not had_eligible:
            self.shutdown_triggered = False
            self.shutdown_reason = None
            self.triggered_at = None
            # Episode over (mains back, nothing pending): the next outage prepares afresh.
            # The clusters stay prepared on the Proxmox side until someone presses
            # "Restore cluster" — the health check keeps saying so.
            self.cluster_prepared = {}
            self.cluster_prep_failed = {}
            self.cluster_prep_steps = {}
            self.cluster_unit_hosts = set()

    async def _maybe_rearm(self) -> None:
        """Release a finished shutdown episode once mains have been back long enough.

        A *sent* shutdown latches its host for good (_evaluate_hosts only releases
        uncommitted ones, so a host that is powering down is never told twice). Nothing
        ever cleared that latch again: the appliance stayed in SHUTTING_DOWN for as long
        as the process lived, a second outage found every host still flagged as fired and
        shut down nothing, and the self-test, the startup checks and the "Restore cluster"
        button — all of which stand down while ``shutdown_triggered`` — never came back.

        The delay is the point: mains returning is not mains staying. A grid that dips
        twice in a minute must not re-arm in between, so every UPS has to be *reachable,
        on mains and no longer triggered* for the whole waiting time. An unreachable UPS
        does not count as mains-ok — the same fail-safe reading as everywhere else: we do
        not know, so we do not act.

        Restoring a prepared cluster stays manual (see restore_clusters); this only makes
        that button reachable again, and says so when a cluster is still carrying the
        preparation.
        """
        minutes = self.cfg.thresholds.rearm_after_mains_min
        latched = self.shutdown_triggered or any(self.host_fired.values())
        if minutes is None or not latched:
            self._mains_ok_since = None
            return

        mains_ok = bool(self.ups_rt) and all(
            rt.state.reachable
            and not rt.state.on_battery
            and rt.on_battery_since is None
            and not rt.triggered
            for rt in self.ups_rt.values()
        )
        if not mains_ok:
            self._mains_ok_since = None
            return
        if self._mains_ok_since is None:
            self._mains_ok_since = _now()
            return
        waited_s = (_now() - self._mains_ok_since).total_seconds()
        if waited_s < minutes * 60:
            return

        pending = [
            name for name, info in self.cluster_states.items() if info.needs_recovery
        ]
        self._release_shutdown_latches()
        self._recompute_state()
        await self._emit(
            "Appliance re-armed",
            f"Mains have been back on every UPS for {minutes} min. The shutdown latches "
            f"are released, so a new outage is handled from scratch."
            + (
                f" Still prepared for shutdown and waiting for 'Restore cluster': "
                f"{', '.join(pending)}."
                if pending
                else ""
            ),
            db.INFO,
        )
        # Not run from here: this is phase A2 of _evaluate(), and a self-test costs up to
        # ten seconds per host — it would delay the eligibility check of the very same
        # iteration. _maybe_selftest() picks the flag up a few lines later in the loop and
        # already owns the guard that refuses to run one during an outage.
        self._rearm_selftest_pending = True

    def _host_trigger_reason(self, host: HostConfig) -> Optional[str]:
        """A host is eligible when its feeds satisfy its policy.

        ``all`` (default, redundant PSUs): every feed must have triggered.
        ``any``: at least one feed has triggered.
        Empty ``ups_ids`` falls back to "all configured UPS".
        """
        feed_ids = self.cfg.feed_ids_for(host)
        rts = [(uid, self.ups_rt[uid]) for uid in feed_ids if uid in self.ups_rt]
        if not rts:
            return None
        fired = [(uid, rt) for uid, rt in rts if rt.triggered]
        if host.ups_policy == "any":
            ready = len(fired) >= 1
        else:  # "all"
            ready = len(fired) == len(rts)
        if not ready:
            return None

        def _label(uid: str) -> str:
            u = self.cfg.ups_by_id(uid)
            return u.label if u else uid

        return "; ".join(f"{_label(uid)}: {rt.trigger_reason}" for uid, rt in fired)

    def _ups_elapsed_on_battery(self, rt: _UpsRuntime) -> Optional[int]:
        """Prefer the UPS counter; fall back to our own timer."""
        if rt.state.seconds_on_battery is not None:
            return rt.state.seconds_on_battery
        if rt.on_battery_since is not None:
            return int((_now() - rt.on_battery_since).total_seconds())
        return None

    def _ups_trigger_reason(self, u: UpsBase, rt: _UpsRuntime) -> Optional[str]:
        """Whether (and why) a single UPS currently demands a shutdown.

        These thresholds must never fire on mains, so a UPS recharging after an outage is
        not mistaken for a reason to shut down. While unreachable, only the on_battery_seconds
        timer (blind countdown) or the opt-in pure comms-loss can match — the fresh UpsState
        carries no runtime/charge/battery_low data.
        """
        th = self.cfg.effective_thresholds(u)
        st = rt.state

        # Opt-in: a prolonged *pure* comms loss is treated as an outage (independent of battery).
        # Wall clock, not poll count: the loop cadence varies (battery interval), so counting
        # polls would misestimate the elapsed time.
        if (
            not st.reachable
            and th.comm_loss_shutdown_after_min is not None
            and rt.unreachable_since is not None
        ):
            elapsed_min = (_now() - rt.unreachable_since).total_seconds() / 60
            if elapsed_min >= th.comm_loss_shutdown_after_min:
                return (
                    f"communication lost for ~{int(elapsed_min)} min "
                    f"(threshold {th.comm_loss_shutdown_after_min} min)"
                )

        on_battery = st.on_battery or rt.on_battery_since is not None
        if not on_battery:
            return None

        # Blind (unreachable) while on battery: only the time-based countdown can match, and
        # only when keep_shutdown_on_comm_loss is enabled.
        if not st.reachable:
            if not (th.keep_shutdown_on_comm_loss and th.on_battery_seconds is not None):
                return None
            elapsed = self._ups_elapsed_on_battery(rt)
            if elapsed is not None and elapsed >= th.on_battery_seconds:
                return (
                    f"on battery for {elapsed} s ≥ {th.on_battery_seconds} s "
                    f"(contact lost while on battery, countdown kept running)"
                )
            return None

        # Reachable and on battery: the full threshold set.
        if th.on_battery_low and st.battery_low:
            return f"UPS reports '{st.battery_status}'"

        if th.runtime_below_minutes is not None and st.runtime_remaining_min is not None:
            if st.runtime_remaining_min <= th.runtime_below_minutes:
                return (
                    f"runtime remaining {st.runtime_remaining_min} min ≤ {th.runtime_below_minutes} min"
                )

        if th.charge_below_percent is not None and st.battery_charge_pct is not None:
            if st.battery_charge_pct <= th.charge_below_percent:
                return f"charge {st.battery_charge_pct}% ≤ {th.charge_below_percent}%"

        if th.on_battery_seconds is not None:
            elapsed = self._ups_elapsed_on_battery(rt)
            if elapsed is not None and elapsed >= th.on_battery_seconds:
                return f"on battery for {elapsed} s ≥ {th.on_battery_seconds} s"

        return None

    def _ups_countdown_remaining_s(self, u: UpsBase, rt: _UpsRuntime) -> Optional[int]:
        th = self.cfg.effective_thresholds(u)
        # Once this UPS has triggered (battery low, charge/runtime threshold, ...), the
        # time-based countdown is moot — hiding it keeps the UI from suggesting the
        # shutdown would wait for it.
        if rt.triggered:
            return None
        if th.on_battery_seconds is None:
            return None
        if not rt.state.on_battery and rt.on_battery_since is None:
            return None
        elapsed = self._ups_elapsed_on_battery(rt)
        if elapsed is None:
            return None
        return max(0, th.on_battery_seconds - elapsed)

    def _ups_comm_loss_remaining_s(self, u: UpsBase, rt: _UpsRuntime) -> Optional[int]:
        """Seconds until the opt-in *pure* comms-loss shutdown fires for this UPS, or None."""
        th = self.cfg.effective_thresholds(u)
        if th.comm_loss_shutdown_after_min is None or rt.comm_loss_fired:
            return None
        if rt.state.reachable or rt.unreachable_since is None:
            return None
        elapsed_s = (_now() - rt.unreachable_since).total_seconds()
        return max(0, int(th.comm_loss_shutdown_after_min * 60 - elapsed_s))

    def _recompute_state(self) -> None:
        committed = any(
            st.get("shutdown_state") in ("sent", "failed") for st in self.host_states.values()
        )
        if committed:
            self.state = SHUTTING_DOWN
        elif any(self.host_fired.values()):
            self.state = SHUTDOWN_PENDING
        elif any(
            rt.state.on_battery or rt.on_battery_since is not None
            for rt in self.ups_rt.values()
        ):
            self.state = ON_BATTERY
        else:
            self.state = ONLINE

    async def simulate_shutdown(self) -> str:
        """Log/notify what a shutdown *would* do, without touching the state machine.

        Safe to call at any time, including during a real outage: it never sets a host
        latch and so cannot suppress a genuine shutdown.
        """

        def _desc(h: HostConfig) -> str:
            feeds = [self.cfg.ups_by_id(i) for i in self.cfg.feed_ids_for(h)]
            labels = "+".join(u.label for u in feeds if u) or "(no UPS)"
            policy = "AND" if h.ups_policy == "all" else "OR"
            kind = str(getattr(h, "type", "pve")).upper()
            return f"{h.name} ({kind}) [{labels}, {policy}]"

        hosts = ", ".join(_desc(h) for h in self.cfg.ordered_hosts()) or "(no hosts)"
        msg = f"Test (dry-run): order {hosts}."
        extra = self._cluster_preview()
        if extra:
            msg += " " + extra
        msg += " NOTHING was shut down."
        await self._emit("Test shutdown executed", msg, db.WARNING)
        return msg

    def _cluster_preview(self) -> str:
        """What the cluster preparation would do, in one line per cluster.

        Built entirely from the last inspection — no I/O. This runs behind a POST the
        operator clicks, and a preview that talks to five nodes would be neither instant
        nor free of side effects.
        """
        if not self.cluster_hosts():
            return ""
        if not self.cluster_states:
            return "No cluster has been inspected yet - run the self-test first."

        th = self.cfg.thresholds
        ap = self.cfg.appliance
        own_hostname = _hostname()
        lines = []
        for name, info in self.cluster_states.items():
            members = [
                h for h in self.cluster_hosts()
                if self.host_states.get(h.key, {}).get("cluster_name") == name
            ]
            want_ceph = any(h.cluster_ceph for h in members) and not info.ceph_unavailable
            want_disarm = (
                any(h.cluster_ha_disarm for h in members) and not info.disarm_unavailable
            )
            steps = []
            if want_disarm:
                steps.append("HA disarm")
            self_guest, _source = cluster.find_self_guest(
                info.guests, ap.self_vmid, ap.self_node, own_hostname
            )
            if want_ceph and not info.guests_unreadable and (
                self_guest is not None or ap.self_external
            ):
                targets = cluster.stop_targets(info.guests, self_guest, own_hostname)
                steps.append(
                    f"stop {len(targets)} of {len(info.running_guests)} running guests"
                    + (f" (sparing {self_guest.label})" if self_guest else "")
                )
            if want_ceph:
                steps.append(f"Ceph flags {','.join(cluster.CEPH_MAINTENANCE_FLAGS)}")
            if not steps:
                steps.append("nothing (no Ceph, no HA disarm)")
            order = ", ".join(
                h.name + ("*" if h.name in info.mon_nodes else "")
                for h in self.cfg.ordered_hosts()
                if self.host_states.get(h.key, {}).get("cluster_name") == name
            )
            lines.append(
                f"Cluster {name}: " + " -> ".join(steps)
                + (f" -> nodes {order}" if order else "")
                + (" (* carries a Ceph MON and should go last)"
                   if any(h.name in info.mon_nodes for h in members) else "")
                + f" Up to {shutdown_budget_s(th)}s in total."
            )
        return " ".join(lines)

    def _maybe_prune(self) -> None:
        """Trim the event log once per day so events.db stays bounded over months."""
        today = _local_now().date()
        if self.last_prune_date == today:
            return
        self.last_prune_date = today
        try:
            db.prune()
        except Exception as exc:  # noqa: BLE001 - housekeeping must never affect the loop
            log.warning("Event log prune failed: %s", exc)

    # -- scheduled self-test of the shutdown targets' credentials -----------
    async def _maybe_cluster_startup_check(self) -> None:
        """Inspect the configured clusters once per process start.

        The self-test latch survives a restart (see _restore_selftest_slot), so after the
        appliance itself has been shut down and comes back, the next scheduled run may be
        a whole interval away — a day, by default. That is exactly the window in which the
        cluster is still disarmed with the maintenance flags set: without this, the
        dashboard would show no cluster at all and the "Restore cluster" button would stay
        hidden, because both are driven by cluster_states. Since restoring is deliberately
        manual, that button is the only prompt the operator gets.

        Read-only, concurrent and hard-bounded, so it costs one poll iteration at most.
        Skipped during an outage for the same reason as the self-test: the countdown has
        to stay responsive, and the preparation reads the state it needs itself.
        """
        if self._cluster_startup_done:
            return
        if self.shutdown_triggered or any(
            rt.on_battery_since is not None or rt.state.on_battery
            for rt in self.ups_rt.values()
        ):
            return  # no latch: try again on the next iteration, once mains are back
        if not self.cluster_hosts():
            self._cluster_startup_done = True
            return
        self._cluster_startup_done = True
        try:
            self.cluster_states = await self._inspect_clusters()
        except Exception as exc:  # noqa: BLE001 - never break the loop over a health read
            log.warning("Cluster startup inspection failed: %s", exc)
            return
        for name, info in self.cluster_states.items():
            if info.needs_recovery:
                # Emitted, not quiet: this is the state a previous outage left behind, and
                # nobody is watching the dashboard at the moment the appliance boots.
                await self._emit(
                    f"Cluster {name}: still prepared for shutdown",
                    "Left over from an earlier outage: "
                    + (
                        f"Ceph flags {', '.join(info.ceph_flags_set)}"
                        if info.ceph_flags_set
                        else ""
                    )
                    + ("; " if info.ceph_flags_set and info.ha_disarmed else "")
                    + (f"HA is {info.ha_armed_state}" if info.ha_disarmed else "")
                    + ". Use 'Restore cluster' once every node is back up.",
                    db.WARNING,
                )

    async def _maybe_node_startup_check(self) -> None:
        """Verify every PVE target's node name once per process start.

        Without this, the answer to "what happens after an update to a config whose node
        name is wrong?" would be "nothing, for up to a day": last_selftest_slot is
        persisted and survives a restart (see _restore_selftest_slot), so the next
        scheduled run may be a whole interval away. The cluster check has had a startup
        override for the same reason; the credential check has none.

        Deliberately NOT the full credential test: that costs up to 10 s per host, while
        this is one GET /nodes per host, read-only and concurrent. It also covers the
        config that arrived by backup import or by hand, which never passed the wizard.
        """
        if self._node_startup_done:
            return
        if self.shutdown_triggered or any(
            rt.on_battery_since is not None or rt.state.on_battery
            for rt in self.ups_rt.values()
        ):
            return  # no latch: try again on the next iteration, once mains are back
        hosts = [
            h for h in self.cfg.hosts if isinstance(h, PveHostConfig) and h.enabled
        ]
        self._node_startup_done = True
        if not hosts:
            return
        try:
            verdicts = await asyncio.gather(
                *(targets.verify_node(h) for h in hosts)
            )
        except Exception as exc:  # noqa: BLE001 - never break the loop over a health read
            log.warning("Node name startup check failed: %s", exc)
            return
        for host, verdict in zip(hosts, verdicts):
            self.host_states.setdefault(host.key, {})["node_state"] = verdict.state
            if verdict.state in ("ok", "unverified"):
                continue
            # Critical only where the name still decides where the shutdown lands; with
            # its own API URL the entry is addressed as /nodes/localhost and the name is
            # a label. Wrong either way, and worth saying either way.
            load_bearing = not self.cfg.api_url_is_unique(host)
            await self._emit(
                f"Host {host.name}: node name does not match this API",
                verdict.detail
                + (
                    " This entry shares its API URL with another one, so the name is what "
                    "addresses the node — the shutdown will not reach it."
                    if load_bearing
                    else " The shutdown itself is unaffected (it addresses the node behind "
                    "this API URL directly), but every event and dashboard entry carries "
                    "this name. Correct it on the host card."
                ),
                db.CRITICAL if load_bearing else db.WARNING,
            )
        await self._warn_about_duplicate_api_urls()

    async def _maybe_selftest(self) -> None:
        """Run the credential self-test once per scheduled slot (see selftest_slot())."""
        cfg = self.cfg
        if not cfg.selftest_enabled or not cfg.hosts:
            return
        # Never spend the poll budget on credential checks during an outage: every host
        # costs up to 10 s and the battery countdown has to stay responsive. No latch is
        # set, so the test runs at the next slot once mains are back.
        if self.shutdown_triggered or any(
            rt.on_battery_since is not None or rt.state.on_battery
            for rt in self.ups_rt.values()
        ):
            return

        # A re-arm asked for one run, off-schedule. Cleared only when it actually runs,
        # so an outage arriving right after the re-arm postpones it instead of losing it.
        # force_log because "everything is fine again" is the answer being looked for, and
        # a silent success would be indistinguishable from no check at all.
        if self._rearm_selftest_pending:
            self._rearm_selftest_pending = False
            await self._run_selftest(force_log=True)
            return

        slot = selftest_slot(_local_now(), cfg.selftest_hour, cfg.selftest_interval_min)
        # ">" rather than "!=": lowering the start hour at runtime moves the slot
        # backwards, which must not fire an extra run.
        if self.last_selftest_slot is not None and slot <= self.last_selftest_slot:
            return
        # Latch and persist *before* running: a crash halfway through the test must not
        # turn into a restart loop that hammers the Proxmox API.
        self.last_selftest_slot = slot
        self._persist_state()
        await self._run_selftest()

    async def run_selftest_now(self) -> tuple[bool, str]:
        """Run the scheduled checks immediately, on explicit request.

        Needed because saving settings deliberately does NOT fire a self-test (see
        update_config) and the next slot may be hours away: after changing a token or
        ticking the cluster option there has to be a way to see the result now.

        Refused during an outage for the same reason _maybe_selftest skips then — every
        host costs up to 10 s and the battery countdown has to stay responsive.
        """
        if self.shutdown_triggered or any(
            rt.on_battery_since is not None or rt.state.on_battery
            for rt in self.ups_rt.values()
        ):
            return False, "Not while a power outage is in progress."
        if not self.cfg.hosts:
            return False, "No hosts configured."
        # An explicit check should report the full current picture, so the "say it once"
        # latch for a missing disarm-ha endpoint does not apply here.
        self._disarm_unsupported_warned.clear()
        await self._run_selftest(force_log=True)
        hosts = self.cfg.ordered_hosts()
        ok = sum(
            1
            for h in hosts
            if self.host_states.get(h.key, {}).get("credentials_ok")
            and self.host_states.get(h.key, {}).get("power_mgmt_ok")
        )
        msg = f"{ok} of {len(hosts)} hosts ok"
        if self.cluster_states:
            msg += f", {len(self.cluster_states)} cluster(s) checked"
        return ok == len(hosts), msg + ". See the event log for details."

    def _node_name_ok(self, host: HostConfig, node_state: str) -> bool:
        """Whether a node-name verdict should count against this host.

        The severity follows whether the name is load-bearing at all. With one API URL per
        entry the shutdown addresses /nodes/localhost and a wrong name is only a
        misleading label — reported, but not a broken target. Where entries share a URL,
        PVE's proxying is the only thing telling them apart, the name IS the path, and the
        same verdict means the shutdown will not land.
        """
        if node_state in ("ok", "unverified"):
            return True
        return self.cfg.api_url_is_unique(host)

    async def _warn_about_duplicate_api_urls(self) -> None:
        """One warning per self-test run for URLs serving more than one enabled entry.

        Nearly always a copy-paste slip, and the state in which the node name is back in
        the shutdown path — so it is worth saying even though the UI flags it while
        editing: a config restored from a backup or edited by hand never passes the UI.

        Said once per URL and config: the startup check and the self-test both call this,
        and repeating an unchanged warning every run trains the operator to skip the feed.
        """
        for url in self.cfg.duplicate_api_urls():
            if url in self._dup_url_warned:
                continue
            self._dup_url_warned.add(url)
            names = [
                h.name for h in self.cfg.hosts
                if h.enabled and h.api_url.strip().rstrip("/").lower() == url
            ]
            await self._emit(
                "Several hosts share one API URL",
                f"{url} is configured for {', '.join(names)}. Every node needs its own "
                f"API URL — one that is already powered off cannot forward the shutdown "
                f"for the others. Until this is fixed, those entries are addressed by "
                f"their configured node name, so that name has to be exactly right.",
                db.WARNING,
            )

    async def _run_selftest(self, force_log: bool = False) -> None:
        """Verify token + power-management privilege per host. Success is logged quietly
        (no notify), failure is emitted (notify) so a broken credential is noticed.

        ``force_log`` bypasses the daily throttle on the quiet "ok" lines — an explicitly
        requested run must show its result, not stay silent because today's line is
        already in the log."""
        hosts = self.cfg.ordered_hosts()
        # Concurrently: sequentially, five unreachable hosts would stall the poll loop for
        # 5 x 10 s. gather preserves the order, so the events stay in host order.
        results = await asyncio.gather(*(targets.test_connection(h) for h in hosts))
        today = _local_now().date()
        # At a 15-minute cadence a quiet "ok" per host and run would be ~100 events per
        # host per day and drown the 48 h event feed. Write one per day, plus whenever the
        # test recovers from a failure. Computed once, before the flags below are updated,
        # and shared with the cluster check so both follow the same cadence.
        log_ok = (
            force_log
            or self.last_selftest_ok is False
            or self.last_selftest_ok_logged != today
        )
        ok_all = True
        for host, result in zip(hosts, results):
            # Keep the outcome per host: it is what /api/status and /api/health report,
            # so a credential that broke months before an outage is visible on the
            # dashboard instead of only in the event log.
            host_ok = result.ok and result.has_power_mgmt and self._node_name_ok(
                host, result.node_state
            )
            self.host_states.setdefault(host.key, {}).update(
                {
                    "credentials_ok": result.ok,
                    "power_mgmt_ok": result.has_power_mgmt,
                    "node_state": result.node_state,
                    "last_test_at": _now().isoformat(),
                    "last_test_error": None if host_ok else result.message,
                }
            )
            if host_ok:
                if log_ok:
                    self._log_quiet(f"Self-test {host.name}: ok", result.message, db.INFO)
                else:
                    log.info("Self-test %s: ok — %s", host.name, result.message)
            else:
                ok_all = False
                sev = db.WARNING if result.ok else db.CRITICAL
                await self._emit(f"Self-test {host.name}: FAILED", result.message, sev)
        if ok_all:
            self.last_selftest_ok_logged = today
        self.last_selftest_ok = ok_all
        self.last_selftest_at = _now()
        await self._warn_about_duplicate_api_urls()
        # Cluster health rides along with the credential check: same schedule, and it is
        # suspended during an outage for the same reason (see _maybe_selftest).
        await self._check_clusters(log_ok=log_ok)

    async def _prepare_clusters(
        self, eligible: list[tuple[HostConfig, str]]
    ) -> list[tuple[HostConfig, str]]:
        """Run the cluster preparation once per cluster, before any of its nodes go down.

        Returns the hosts that may proceed. Ordinarily that is all of them: a failed
        preparation leaves the HA manager armed, and an armed LRM still stops the guests
        itself — degraded, but the machines come down. Only when the user explicitly opted
        into ``cluster_abort_on_prep_failure`` are that cluster's hosts held back, because
        the alternative there is losing power uncontrolled.
        """
        th = self.cfg.thresholds
        # Freeze the candidate list per iteration, like the UPS list in _loop(): a config
        # save during the awaits below can swap self.cfg underneath us.
        candidates = [
            (host, reason)
            for host, reason in eligible
            if isinstance(host, PveHostConfig) and host.cluster
        ]
        if not candidates:
            return eligible

        ap = self.cfg.appliance
        own_hostname = _hostname()
        # Hosts pulled in because their cluster goes down as a unit. Collected here and
        # merged once at the end, so the canonical ordering is rebuilt exactly once.
        extra: list[tuple[HostConfig, str]] = []

        for host, reason in candidates:
            name = self.host_states.get(host.key, {}).get("cluster_name") or ""
            info = self.cluster_states.get(name) if name else None
            # The name may be unknown when no self-test has run yet — read it now, from
            # the node we are about to shut down, and KEEP that reading: it already
            # carries the feature detection the writes below depend on. Re-fetching from
            # cluster_states instead would throw it away and fall back to guessing.
            if info is None:
                discovered = await cluster.inspect(host)
                if not discovered.reachable or not discovered.is_cluster:
                    continue
                name = discovered.name or host.name
                self.host_states.setdefault(host.key, {})["cluster_name"] = name
                self.cluster_states.setdefault(name, discovered)
                info = discovered

            if self.cluster_prepared.get(name):
                continue
            self.cluster_prepared[name] = True  # latch first: never prepare twice

            # Who else belongs to this cluster, and how many of them asked for this on
            # their own. Both numbers go into the announcement: a preparation that runs
            # on a partial outage is the one case where the event log has to say so.
            members = self._cluster_members(info, name)
            due_keys = {h.key for h, _ in eligible} | {
                k for k, fired in self.host_fired.items() if fired
            }
            due = [m for m in members if m.key in due_keys]

            # Ask for nothing this cluster cannot do. Ceph flags need Ceph, disarm-ha
            # needs PVE 9.2+ — and both are told apart from "could not read" (see
            # ClusterInfo.ceph_unavailable / .disarm_unavailable), so a denied read still
            # attempts the write instead of silently skipping it. Without this, a cluster
            # without Ceph reported a FAILED preparation on every single outage, and with
            # abort-on-failure it held every node back over a component that is not there.
            want_ceph = host.cluster_ceph and not info.ceph_unavailable
            want_disarm = host.cluster_ha_disarm and not info.disarm_unavailable

            # The cluster-wide guest stop rides on the Ceph switch and has none of its
            # own. On a hyper-converged cluster it is not an option but the first step of
            # the official procedure: letting each node stop its own guests as it powers
            # off drops the pool below min_size, and the guests still running on the
            # survivors then block on IO and never finish shutting down. Without Ceph none
            # of this applies and nothing changes.
            self_guest, source = cluster.find_self_guest(
                info.guests, ap.self_vmid, ap.self_node, own_hostname
            )
            # Stopping guests while the HA manager is live and holding resources only
            # feeds it work. With zero HA resources there is nobody to restart them, so
            # the disarm stops being a precondition.
            guest_needs_disarm = info.ha_resources
            if not want_ceph:
                guest_block = "no Ceph"
            elif info.guests_unreadable:
                guest_block = "guest list unreadable"
            elif guest_needs_disarm and not want_disarm:
                guest_block = "needs HA disarm (PVE 9.2+)"
            elif self_guest is None and not ap.self_external:
                guest_block = f"own guest not identified ({source})"
            else:
                guest_block = ""
            want_guests = want_ceph and not guest_block

            def _guest_clause() -> str:
                return _prep_intent(host.cluster_ceph, want_guests, guest_block)

            if self.cfg.dry_run:
                await self._emit(
                    f"DRY-RUN: cluster {name} would be prepared",
                    f"HA disarm: "
                    f"{_prep_intent(host.cluster_ha_disarm, want_disarm, 'needs PVE 9.2+')}; "
                    f"guest shutdown: {_guest_clause()}"
                    + (
                        f" ({len(cluster.stop_targets(info.guests, self_guest, own_hostname))} "
                        f"guests"
                        + (f", sparing {self_guest.label}" if self_guest else "")
                        + ")"
                        if want_guests
                        else ""
                    )
                    + f"; Ceph flags: {_prep_intent(host.cluster_ceph, want_ceph, 'no Ceph')}"
                    + self._unit_clause(host, members, due)
                    + ". NOTHING is changed.",
                    db.WARNING,
                )
                extra += self._unit_additions(host, name, members, eligible, extra, reason)
                continue

            if not want_ceph and not want_disarm:
                # Quiet, not a failure: the operator asked for features this cluster does
                # not have. Saying it once per outage beats a CRITICAL that reads as if
                # the shutdown went wrong.
                self._log_quiet(
                    f"Cluster {name}: nothing to prepare",
                    f"HA disarm: "
                    f"{_prep_intent(host.cluster_ha_disarm, False, 'needs PVE 9.2+')}; "
                    f"guest shutdown: {_guest_clause()}; "
                    f"Ceph flags: {_prep_intent(host.cluster_ceph, False, 'no Ceph')}. "
                    f"The nodes are shut down normally.",
                    db.INFO,
                )
                extra += self._unit_additions(host, name, members, eligible, extra, reason)
                continue

            # Said BEFORE the work, not only after it: the preparation is the one step
            # that deliberately delays the shutdown, for up to cluster_prep_timeout_s.
            # Without this line the event log jumped straight from "power outage" to a
            # result a minute later, and nothing said that the nodes were waiting on
            # purpose. WARNING like the outage and the shutdown itself, so the chain is
            # complete in the webhook too; once per cluster and episode (the latch above
            # is already set), so it cannot repeat every eight seconds.
            budget = th.cluster_prep_timeout_s + (
                th.cluster_guest_shutdown_timeout_s if want_guests else 0
            )
            await self._emit(
                f"Cluster {name}: preparing for shutdown",
                f"HA disarm: "
                f"{_prep_intent(host.cluster_ha_disarm, want_disarm, 'needs PVE 9.2+')}; "
                f"guest shutdown: {_guest_clause()}"
                + (
                    f" ({len(cluster.stop_targets(info.guests, self_guest, own_hostname))} "
                    f"guests"
                    + (f", sparing {self_guest.label}" if self_guest else "")
                    + ")"
                    if want_guests
                    else ""
                )
                + f"; Ceph flags: {_prep_intent(host.cluster_ceph, want_ceph, 'no Ceph')}."
                + self._unit_clause(host, members, due)
                + f" The nodes of this cluster wait up to {budget}s for this"
                + (
                    " and are held back entirely if it fails ('abort on failure')."
                    if th.cluster_abort_on_prep_failure
                    else ", then shut down whether it worked or not."
                ),
                db.WARNING,
            )

            # Refusals worth their own CRITICAL, because each one means the cluster is
            # about to lose power with its guests still writing to Ceph — the exact
            # failure the guest stop exists to prevent. Said before the work so it is not
            # buried in a result line a minute later. "no Ceph" is not among them: there
            # the guest stop is simply not applicable.
            if want_ceph and guest_block and guest_block != "no Ceph":
                await self._emit(
                    f"Cluster {name}: guest shutdown skipped",
                    {
                        "guest list unreadable": (
                            "The cluster's guest list could not be read (the token needs "
                            "VM.Audit), so it is unknown what would have to stop. Note "
                            "that this endpoint filters by privilege instead of refusing, "
                            "so an empty answer is never taken to mean 'no guests'."
                        ),
                        "needs HA disarm (PVE 9.2+)": (
                            "HA manages guests on this cluster and cannot be disarmed "
                            "(disarm-ha needs Proxmox VE 9.2 or newer, or the switch is "
                            "off), so the HA manager would restart the guests as fast as "
                            "they are stopped."
                        ),
                    }.get(
                        guest_block,
                        "This appliance's own guest could not be identified "
                        f"({source}), and stopping every guest would have stopped this "
                        "appliance in the middle of the outage. Pick it under Settings "
                        "-> Appliance.",
                    )
                    + " The nodes are shut down without it, which is what earlier "
                    "releases did.",
                    db.CRITICAL,
                )

            result = await cluster.prepare(
                host,
                want_ceph=want_ceph,
                want_disarm=want_disarm,
                want_guests=want_guests,
                guests=info.guests,
                self_guest=self_guest,
                guest_needs_disarm=guest_needs_disarm,
                hostname=own_hostname,
                timeout=th.cluster_prep_timeout_s,
                guest_timeout=th.cluster_guest_shutdown_timeout_s,
                force_after_s=th.cluster_guest_force_after_s,
            )
            # Latched, not re-derived per iteration: the preparation runs once per
            # episode, so this is the only moment the outcome is known — and the only
            # moment these two events may be written.
            self.cluster_prep_failed[name] = not result.ok
            self.cluster_prep_steps[name] = list(result.steps)
            await self._emit(
                f"Cluster {name}: preparation {'done' if result.ok else 'FAILED'}",
                result.message,
                db.WARNING if result.ok else db.CRITICAL,
            )
            if not result.ok and th.cluster_abort_on_prep_failure:
                await self._emit(
                    f"Cluster {name}: shutdown aborted",
                    "The cluster preparation failed and 'abort on failure' is enabled, "
                    "so no node of this cluster will be shut down.",
                    db.CRITICAL,
                )

            extra += self._unit_additions(host, name, members, eligible, extra, reason)
            # The state the field test ran into: the preparation is cluster-wide — HA
            # disarmed, with Ceph every guest stopped — while only the nodes whose own UPS
            # triggered go down. The rest stand there without guests, without HA and with
            # the maintenance flags set, and on a hyper-converged cluster their storage is
            # gone too once the monitors follow. Said out loud rather than left to be
            # discovered afterwards.
            if not host.cluster_shutdown_all and len(due) < len(members):
                await self._emit(
                    f"Cluster {name}: only part of the cluster is shut down",
                    f"{len(due)} of {len(members)} nodes triggered, and 'shut the whole "
                    f"cluster down' is off, so "
                    f"{', '.join(m.name for m in members if m not in due)} keep running — "
                    f"with HA disarmed"
                    + (", every guest stopped and the Ceph maintenance flags set"
                       if want_ceph else "")
                    + ". Switch the option on, or feed every node of this cluster from "
                    "UPS devices that fail together.",
                    db.CRITICAL,
                )

        if extra:
            # Re-sorted through ordered_hosts() rather than appended: _evaluate_hosts
            # groups by (this_host, order) and relies on that sort making the groups
            # contiguous, so tacking the additions onto the end would split a stage in two
            # and could let the appliance's own host go before its peers.
            merged = {h.key: (h, r) for h, r in eligible + extra}
            eligible = [
                merged[h.key] for h in self.cfg.ordered_hosts() if h.key in merged
            ]

        # From the latch, not from the work of this iteration: the preparation happens
        # once per episode, while this filter has to hold on every poll that follows.
        # Reading it off the current round instead let the hosts through on the very next
        # one, silently undoing the opt-in the operator chose.
        if not th.cluster_abort_on_prep_failure:
            return eligible
        blocked = {name for name, failed in self.cluster_prep_failed.items() if failed}
        if not blocked:
            return eligible
        return [
            (host, reason)
            for host, reason in eligible
            if self.host_states.get(host.key, {}).get("cluster_name") not in blocked
        ]

    def _cluster_members(self, info: "cluster.ClusterInfo", name: str) -> list[PveHostConfig]:
        """Every enabled PVE host belonging to this cluster.

        Two ways to match, because a shutdown can happen before any self-test has run:
        the discovered cluster name in host_states, or the node list the API just gave us.
        The second is the same verbatim name comparison cluster.node_coverage() makes —
        the shutdown call uses the name literally, so anything looser would be a lie.
        """
        nodes = {n for n in info.nodes if n}
        found: dict[str, PveHostConfig] = {}
        for h in self.cfg.hosts:
            if not isinstance(h, PveHostConfig) or not h.enabled:
                continue
            known = self.host_states.get(h.key, {}).get("cluster_name")
            if (name and known == name) or h.name in nodes:
                found[h.key] = h
        return list(found.values())

    def _unit_clause(
        self, host: PveHostConfig, members: list[PveHostConfig], due: list[PveHostConfig]
    ) -> str:
        """The "n of m nodes" half-sentence for the preparation events."""
        if len(members) <= 1:
            return ""
        part = f" {len(due)} of {len(members)} nodes triggered"
        if len(due) == len(members):
            return part + "."
        return part + (
            "; the rest are shut down with them."
            if host.cluster_shutdown_all
            else "; the rest keep running ('shut the whole cluster down' is off)."
        )

    def _unit_additions(
        self, host: PveHostConfig, name: str, members: list[PveHostConfig],
        eligible: list[tuple[HostConfig, str]], extra: list[tuple[HostConfig, str]],
        reason: str,
    ) -> list[tuple[HostConfig, str]]:
        """Nodes of this cluster that go down with it although nothing triggered for them.

        The preparation is cluster-wide by nature — it disarms HA and, with Ceph, stops
        every guest in the cluster. Shutting down only the nodes whose own UPS triggered
        leaves the others without guests, without HA and (once the monitors follow)
        without storage. This is what keeps the two halves in step.
        """
        if not host.cluster_shutdown_all:
            return []
        taken = {h.key for h, _ in eligible} | {h.key for h, _ in extra}
        out = []
        for member in members:
            if member.key in taken or self.host_fired.get(member.key):
                continue
            self.cluster_unit_hosts.add(member.key)
            # Record the membership we just established. On a cold start these hosts were
            # matched through the API's node list, not through host_states — and the
            # abort-on-failure filter below keys on exactly that entry, so without this a
            # held-back cluster would still shut its taken-along nodes down.
            self.host_states.setdefault(member.key, {})["cluster_name"] = name
            out.append(
                (member, f"cluster {name} is shut down as a unit ({reason})")
            )
        return out

    # -- cluster awareness (read-only part) ----------------------------------
    def cluster_hosts(self) -> list[PveHostConfig]:
        """Enabled PVE hosts with cluster preparation switched on."""
        return [
            h
            for h in self.cfg.hosts
            if isinstance(h, PveHostConfig) and h.enabled and h.cluster
        ]

    async def _inspect_clusters(self) -> dict[str, cluster.ClusterInfo]:
        """Inspect each configured cluster once, via its first reachable member.

        Grouping is by the cluster NAME the API reports, not by anything configured:
        two separate clusters stay two groups, and a host that turns out to be
        standalone drops out on its own. Members are probed concurrently — five
        unreachable ones would otherwise stall the poll loop for 5 x 10 s.
        """
        hosts = self.cluster_hosts()
        if not hosts:
            return {}
        ap = self.cfg.appliance
        own_hostname = _hostname()
        infos = await asyncio.gather(
            *(
                cluster.inspect(
                    h, self_vmid=ap.self_vmid, self_node=ap.self_node,
                    hostname=own_hostname,
                )
                for h in hosts
            )
        )

        found: dict[str, cluster.ClusterInfo] = {}
        for host, info in zip(hosts, infos):
            self.host_states.setdefault(host.key, {}).update(
                {
                    "cluster_name": info.name,
                    "cluster_is_cluster": info.is_cluster,
                    "cluster_error": info.error,
                }
            )
            if not info.reachable or not info.is_cluster:
                continue
            # First reachable member of a cluster wins; the rest are the same cluster.
            found.setdefault(info.name or host.name, info)
        self.cluster_guest_state = {
            name: {
                "total": len(info.guests),
                "running": len(info.running_guests),
                "readable": info.guests_read,
                "self_vmid": info.self_guest.vmid if info.self_guest else None,
                "self_label": info.self_guest.label if info.self_guest else "",
                "self_node": info.self_guest.node if info.self_guest else "",
                "self_source": info.self_guest_source,
                "self_on_ceph": info.self_guest_on_ceph,
                "mon_nodes": list(info.mon_nodes),
            }
            for name, info in found.items()
        }
        return found

    async def restore_clusters(self) -> tuple[bool, list[dict]]:
        """Undo the shutdown preparation on every cluster that still carries it.

        Driven by the dashboard button, never by a timer. Each cluster is restored
        through its first reachable member, and the state is re-read afterwards so the
        dashboard and the health warnings agree immediately instead of at the next
        self-test.

        Returns (allowed, results). Refused during an outage: the button becomes visible
        the moment the preparation lands, which is mid-shutdown — and arming HA while the
        nodes are powering off would undo the preparation at the one moment it is doing
        its job. The confirmation dialog alone is too soft a guard for that.
        """
        if self.shutdown_triggered or any(
            rt.on_battery_since is not None or rt.state.on_battery
            for rt in self.ups_rt.values()
        ):
            return False, []

        infos = await self._inspect_clusters()
        results: list[dict] = []
        for name, info in infos.items():
            if not info.needs_recovery:
                continue
            host = next(
                (
                    h
                    for h in self.cluster_hosts()
                    if self.host_states.get(h.key, {}).get("cluster_name") == name
                ),
                None,
            )
            if host is None:
                continue
            # The same budget the preparation gets, and never less than the module
            # default: arming HA back is exactly as slow as disarming it, and this runs
            # with mains back, so there is no battery to spend.
            result = await cluster.restore(
                host,
                timeout=max(60.0, float(self.cfg.thresholds.cluster_prep_timeout_s)),
            )
            await self._emit(
                f"Cluster {name}: restore {'done' if result.ok else 'FAILED'}",
                result.message,
                db.WARNING if result.ok else db.CRITICAL,
            )
            results.append({"cluster": name, "ok": result.ok, "message": result.message})
            self.cluster_prepared.pop(name, None)
            self.cluster_prep_failed.pop(name, None)
        if results:
            # Re-read so /api/status stops showing the warning the moment it is fixed.
            self.cluster_states = await self._inspect_clusters()
        return True, results

    async def _check_clusters(self, log_ok: bool = False) -> None:
        """Health checks per cluster, run as part of the scheduled self-test.

        Everything here is a WARNING at most: these are problems for the *next* outage,
        not reasons to call a running appliance broken. They go through _emit()/
        _log_quiet() so they land on the dashboard, /api/status and the webhooks without
        needing a channel of their own.

        ``log_ok`` writes a quiet "checked, all good" line per healthy cluster. Warnings
        alone would leave "checked and fine" indistinguishable from "never checked", so
        the self-test says what it found — throttled by the caller to the same daily
        cadence as the per-host "ok" lines.
        """
        try:
            infos = await self._inspect_clusters()
        except Exception as exc:  # noqa: BLE001 - a health check must never break the loop
            log.warning("Cluster inspection failed: %s", exc)
            return
        self.cluster_states = {name: info for name, info in infos.items()}

        for name, info in infos.items():
            warned = 0
            wants_disarm = any(
                h.cluster_ha_disarm for h in self.cluster_hosts()
                if self.host_states.get(h.key, {}).get("cluster_name") == name
            )
            wants_ceph = any(
                h.cluster_ceph for h in self.cluster_hosts()
                if self.host_states.get(h.key, {}).get("cluster_name") == name
            )

            missing = cluster.missing_privileges(
                info, wants_ceph, wants_disarm, want_guests=wants_ceph
            )
            if missing:
                warned += 1
                await self._emit(
                    f"Cluster {name}: token privileges incomplete",
                    f"Missing: {', '.join(missing)}. The cluster preparation before a "
                    f"shutdown will not work until they are granted. Sys.* are read on "
                    f"'/', the VM ones also count on '/vms'.",
                    db.WARNING,
                )

            # How the nodes are fed decides whether a partial outage is possible at
            # all — and that question stands whether or not Ceph is involved.
            warned += await self._check_cluster_feeds(name, info, wants_ceph)

            # --- the appliance's own guest ------------------------------------
            # Everything below only matters once the guest stop is in play, i.e. with
            # Ceph. On a plain cluster none of it applies and none of it is said.
            if wants_ceph:
                warned += await self._check_self_guest(name, info)

            # Asked for a feature this cluster does not have. Only a warning — the
            # shutdown itself is unaffected (the step is skipped, see _prepare_clusters)
            # — but the tick is dead weight that also asks for a privilege nobody needs.
            if wants_ceph and info.ceph_unavailable:
                warned += 1
                await self._emit(
                    f"Cluster {name}: Ceph flags enabled, but no Ceph found",
                    "'Set Ceph maintenance flags' is ticked, yet this cluster does not "
                    "run Ceph. The step is skipped before every shutdown. Untick it — "
                    f"then {cluster.PRIV_MODIFY} is not needed on '/' either.",
                    db.WARNING,
                )

            if not info.quorate:
                warned += 1
                await self._emit(
                    f"Cluster {name}: no quorum",
                    "The cluster has no quorum. Cluster-wide preparation before a "
                    "shutdown cannot be carried out in this state.",
                    db.WARNING,
                )

            # Counted over every enabled PVE target, NOT only the cluster-ticked ones:
            # the tick governs the preparation (which runs once per cluster anyway), while
            # this warning is about nodes that nobody will shut down. Counting the ticks
            # instead accused a perfectly complete setup of leaving nodes running.
            pve = [h for h in self.cfg.hosts if isinstance(h, PveHostConfig)]
            cov = cluster.node_coverage(
                info.nodes,
                [h.name for h in pve if h.enabled],
                [h.name for h in pve if not h.enabled],
            )
            if info.nodes and cov.missing:
                warned += 1
                await self._emit(
                    f"Cluster {name}: not every node is a configured target",
                    cluster.coverage_report(info.nodes, cov),
                    db.WARNING,
                )

            # Left over from an earlier outage — these two are why the check exists.
            if info.ceph_flags_set:
                warned += 1
                await self._emit(
                    f"Cluster {name}: Ceph maintenance flags still set",
                    f"{', '.join(info.ceph_flags_set)} are still active, so Ceph is not "
                    f"rebalancing or recovering. Use 'Restore cluster' once the cluster "
                    f"is fully back.",
                    db.WARNING,
                )
            # The arm state alone: a disarmed stack means no fencing cluster-wide, even
            # with zero HA guests — and it stays disarmed until someone re-arms it.
            if info.ha_disarmed:
                warned += 1
                await self._emit(
                    f"Cluster {name}: HA is still disarmed",
                    "The HA manager is disarmed, so a real node failure would not be "
                    "handled. Use 'Restore cluster' to arm it again.",
                    db.WARNING,
                )

            # Either an armed state is reported or HA manages guests — otherwise there is
            # nothing to disarm and the version limitation is not worth a word.
            if (
                wants_disarm
                and (info.ha_resources or info.ha_present)
                and info.disarm_unavailable
            ):
                # Once per appliance run: repeating a version limitation every self-test
                # would train the operator to ignore the feed.
                # Counted either way: the cluster is not "all good" just because the
                # limitation was already reported on an earlier run.
                warned += 1
                if name not in self._disarm_unsupported_warned:
                    self._disarm_unsupported_warned.add(name)
                    await self._emit(
                        f"Cluster {name}: HA disarm not available",
                        "Arming/disarming HA needs Proxmox VE 9.2 or newer. Ceph flags "
                        "are still set as configured. As an alternative, set the "
                        "datacenter option shutdown_policy=freeze, which keeps HA "
                        "services from being recovered onto nodes that are shutting down.",
                        db.WARNING,
                    )

            # Independent of the version: whenever HA will NOT be disarmed, the
            # datacenter's shutdown policy decides what happens to the services.
            will_disarm = wants_disarm and not info.disarm_unavailable
            if info.ha_resources and not will_disarm and info.shutdown_policy in (
                "conditional", "migrate", "failover", ""
            ):
                policy = info.shutdown_policy or "conditional (default)"
                extra = (
                    " With 'migrate' the LRM even delays the shutdown until everything "
                    "has been migrated away — the opposite of what a draining battery "
                    "needs."
                    if info.shutdown_policy == "migrate"
                    else ""
                )
                warned += 1
                await self._emit(
                    f"Cluster {name}: shutdown_policy is '{policy}'",
                    f"HA will not be disarmed, so on power-off the HA services are "
                    f"recovered onto nodes that are shutting down themselves."
                    f"{extra} Recommended: shutdown_policy=freeze.",
                    db.WARNING,
                )

            # Nothing to complain about: say so, or "checked and fine" is indistinguishable
            # from "never checked" in the event feed. Throttled by the caller (log_ok) to
            # the same daily cadence as the per-host "ok" lines.
            if not warned and log_ok:
                self._log_quiet(
                    f"Cluster {name}: ok",
                    f"{len(info.nodes)} nodes ({info.nodes_online} online), quorum ok, "
                    f"Ceph {'flags clean' if info.ceph_configured else 'not configured'}, "
                    f"HA {info.ha_armed_state or 'stack not reporting a state'} "
                    f"({info.ha_services} HA guests)"
                    f"{'' if info.disarm_supported else ', disarm-ha not available'}.",
                    db.INFO,
                )

    async def _check_cluster_feeds(
        self, name: str, info: "cluster.ClusterInfo", wants_ceph: bool
    ) -> int:
        """Warn when the nodes of one cluster can be triggered independently.

        Pure configuration, no I/O: two nodes fed by different UPS devices mean one of
        them can become due while the other does not. What follows from that depends on
        the "as a unit" switch, and both outcomes are worth knowing in advance — which is
        why this is said at self-test time rather than discovered during an outage.
        """
        members = [h for h in self._cluster_members(info, name) if h.cluster]
        if len(members) < 2:
            return 0
        feeds = {h.key: frozenset(self.cfg.feed_ids_for(h)) for h in members}
        if len(set(feeds.values())) < 2:
            return 0

        unit = [h.cluster_shutdown_all for h in members]
        if all(unit):
            body = (
                "One UPS device failing is therefore enough to shut down the whole "
                "cluster, including the nodes that still have power - that is what "
                "'shut the whole cluster down as a unit' does, and on a hyper-converged "
                "cluster it is the right answer."
            )
        elif not any(unit):
            body = (
                "A single UPS device failing therefore shuts down only the nodes it "
                "feeds, while the preparation has already disarmed HA"
                + (" and stopped every guest in the cluster" if wants_ceph else "")
                + " for all of them. Switch 'shut the whole cluster down as a unit' on, "
                "or feed every node from UPS devices that fail together."
            )
        else:
            body = (
                "'Shut the whole cluster down as a unit' is also set on some nodes of "
                "this cluster and not on others, so what happens depends on which node "
                "happens to trigger first. Set it the same way everywhere."
            )
        await self._emit(
            f"Cluster {name}: its nodes are fed by different UPS devices",
            ", ".join(
                f"{h.name}: " + (", ".join(
                    (self.cfg.ups_by_id(u).label if self.cfg.ups_by_id(u) else u)
                    for u in sorted(feeds[h.key])
                ) or "all UPS devices")
                for h in members
            )
            + ". " + body
            + " Note also that a UPS which goes unreachable instead of reporting battery "
            "(a management switch on the failing UPS) never triggers at all - see "
            "'Shutdown on pure communication loss after (min)'.",
            db.WARNING,
        )
        return 1

    async def _check_self_guest(self, name: str, info: "cluster.ClusterInfo") -> int:
        """Warnings around the cluster-wide guest stop. Returns how many were emitted.

        Split out of _check_clusters because it is a self-contained question — "can this
        appliance stop the other guests without stopping itself, and will it survive
        doing so" — and because all of it is pointless without Ceph.
        """
        ap = self.cfg.appliance
        warned = 0

        if info.guests_unreadable:
            warned += 1
            await self._emit(
                f"Cluster {name}: the guest list cannot be read",
                "Stopping the guests before the nodes needs VM.Audit and VM.PowerMgmt "
                "(on '/' or '/vms'). Note that /cluster/resources filters by privilege "
                "instead of refusing, so an empty answer is never read as 'no guests'.",
                db.WARNING,
            )
            return warned

        guest, source = cluster.find_self_guest(
            info.guests, ap.self_vmid, ap.self_node, _hostname()
        )
        if guest is None and not ap.self_external:
            warned += 1
            reason = {
                "missing": (
                    f"The selected guest {ap.self_vmid} does not exist in this cluster "
                    f"(renumbered, or it belongs to a different one)."
                ),
                "ambiguous": (
                    f"Several guests are named '{_hostname()}', so the hostname cannot "
                    f"identify this one."
                ),
            }.get(source, "No guest has been selected and none carries this hostname.")
            await self._emit(
                f"Cluster {name}: this appliance's own guest is unknown",
                f"{reason} Until it is selected under Settings -> Appliance the "
                f"cluster-wide guest shutdown is skipped, because stopping every guest "
                f"would stop this appliance in the middle of the outage. Tick 'not a "
                f"guest of this cluster' if it really runs elsewhere.",
                db.WARNING,
            )
        elif guest is not None:
            # Storage first: this is the deployment mistake that only shows up when it is
            # far too late to fix, and the appliance is the one guest that has to outlive
            # the cluster it is shutting down.
            if info.self_guest_on_ceph:
                warned += 1
                await self._emit(
                    f"Cluster {name}: this appliance runs on Ceph storage",
                    f"{guest.label} uses "
                    f"{', '.join(s for s in info.self_guest_storages if s in info.ceph_storages)}"
                    f", which is Ceph-backed. Once the OSDs drop below min_size its own "
                    f"IO blocks, so it can no longer shut anything down - the appliance "
                    f"would freeze halfway through the outage. Move it to local storage "
                    f"(local-lvm/local-zfs).",
                    db.WARNING,
                )
            # A guest that resolved but sits on a node nobody shuts down last is the same
            # mistake as an unset this_host, only harder to see.
            owner = next(
                (h for h in self.cfg.hosts if h.enabled and h.name == guest.node), None
            )
            if owner is not None and not owner.this_host:
                warned += 1
                await self._emit(
                    f"Cluster {name}: this appliance's node is not marked 'this host'",
                    f"{guest.label} runs on {guest.node}, but that host is not marked as "
                    f"the one carrying this appliance, so it is not forced to shut down "
                    f"last. Selecting the guest under Settings -> Appliance sets this "
                    f"automatically.",
                    db.WARNING,
                )

        advisory = cluster.advisory_privileges(info)
        if advisory:
            self._log_quiet(
                f"Cluster {name}: storage check unavailable",
                f"Without {', '.join(advisory)} it cannot be checked whether this "
                f"appliance's own guest lives on Ceph. Everything else works.",
                db.INFO,
            )

        # MON ordering: advice, never enforcement (see cluster.mon_order_report).
        order = [
            h.name
            for h in self.cfg.ordered_hosts()
            if self.host_states.get(h.key, {}).get("cluster_name") == name
        ]
        this_node = next(
            (h.name for h in self.cfg.hosts if h.enabled and h.this_host), ""
        )
        report = cluster.mon_order_report(order, info.mon_nodes, this_node)
        if report:
            warned += 1
            await self._emit(f"Cluster {name}: MON nodes are not shut down last",
                             report, db.WARNING)

        # The whole sequence now holds the battery for far longer than a bare node
        # shutdown. Warned about, never acted on: lowering someone's trigger for them is
        # not this appliance's decision.
        th = self.cfg.thresholds
        budget = shutdown_budget_s(th)
        reserve = th.runtime_below_minutes
        if reserve is not None and reserve * 60 < budget:
            warned += 1
            await self._emit(
                f"Cluster {name}: battery reserve is shorter than the shutdown",
                f"The shutdown trigger fires at {reserve} min ({reserve * 60}s) of "
                f"estimated runtime, but preparing this cluster and shutting it down can "
                f"take up to {budget}s (HA disarm "
                f"{th.cluster_prep_timeout_s}s + guests "
                f"{th.cluster_guest_shutdown_timeout_s}s + nodes "
                f"{th.host_shutdown_timeout_s}s). Raise the trigger or lower the guest "
                f"timeout.",
                db.WARNING,
            )
        return warned

    def _log_quiet(self, subject: str, body: str, severity: str) -> None:
        """Write an event without firing notifications (for routine successes)."""
        log.info("%s — %s", subject, body)
        try:
            db.log_event(subject, body, severity)
        except Exception as exc:  # noqa: BLE001
            log.warning("Event log write failed: %s", exc)

    # -- shutdown execution --------------------------------------------------
    async def _fire_host(self, host: HostConfig, reason: str) -> None:
        """Shut down a single host (or latch/log it in dry-run).

        Runs concurrently with its stage peers (see _evaluate_hosts), so it must not
        assume it is alone: state is merged into host_states, never replaced.
        """
        self.host_fired[host.key] = True
        if not self.shutdown_triggered:
            self.shutdown_triggered = True
            self.triggered_at = _now()
        self.shutdown_reason = f"{host.name}: {reason}"

        if self.cfg.dry_run:
            await self._emit(
                "DRY-RUN: shutdown would be triggered",
                f"Host {host.name} — reason: {reason}. NOTHING will be shut down.",
                db.CRITICAL,
            )
            return

        ok, msg = await targets.shutdown(
            host,
            timeout=self.cfg.thresholds.host_shutdown_timeout_s,
            # Only where this entry owns its API URL is "the node behind it" unambiguous.
            # Sharing one URL across entries makes PVE's proxying the only thing telling
            # them apart, so there the configured name has to stay in the path.
            use_localhost=self.cfg.api_url_is_unique(host),
        )
        # Merge, so the last self-test result stays visible next to the shutdown result.
        self.host_states.setdefault(host.key, {}).update(
            {
                "shutdown_state": "sent" if ok else "failed",
                "last_action_at": _now().isoformat(),
                "last_error": None if ok else msg,
                "reachable": ok,
                "this_host": host.this_host,
                "order": host.order,
            }
        )
        await self._emit(
            f"Host {host.name}: shutdown {'sent' if ok else 'FAILED'}",
            f"Reason: {reason}. {msg}",
            # An executed shutdown is not routine either (see "shutdown aborted" above).
            db.WARNING if ok else db.CRITICAL,
        )

    # -- notifications + event log ------------------------------------------
    async def _emit(self, subject: str, body: str, severity: str) -> None:
        log.log(
            logging.WARNING if severity != db.INFO else logging.INFO,
            "%s — %s",
            subject,
            body,
        )
        try:
            db.log_event(subject, body, severity)
        except Exception as exc:  # noqa: BLE001
            log.warning("Event log write failed: %s", exc)
        await notify.notify(
            self.cfg.notifications, f"[PVE-UPS] {subject}", body, self.snapshot(), severity
        )

    # -- status snapshot for the REST API -----------------------------------
    def _ups_snapshot(self, u: UpsBase, rt: _UpsRuntime) -> dict:
        st = rt.state
        th = self.cfg.effective_thresholds(u)
        return {
            "id": u.id,
            "name": u.label,
            "type": getattr(u, "type", "snmp"),
            # Which MIB the last poll actually read ("" until the first answer, and for
            # sources that have no MIB). Diagnostics only — "auto" is otherwise invisible.
            "mib": st.mib,
            "reachable": st.reachable,
            "manufacturer": st.manufacturer,
            "model": st.model,
            "last_poll": st.last_poll.isoformat() if st.last_poll else None,
            "poll_interval_s": (
                self.cfg.thresholds.poll_interval_battery_s
                if st.on_battery
                else self.cfg.thresholds.poll_interval_normal_s
            ),
            "power_source": st.power_source,
            "battery_status": st.battery_status,
            "runtime_remaining_min": st.runtime_remaining_min,
            "battery_charge_pct": st.battery_charge_pct,
            "load_pct": st.load_pct,
            "seconds_on_battery": self._ups_elapsed_on_battery(rt),
            "triggered": rt.triggered,
            "trigger_reason": rt.trigger_reason,
            "countdown_remaining_s": self._ups_countdown_remaining_s(u, rt),
            "comm_loss_remaining_s": self._ups_comm_loss_remaining_s(u, rt),
            "alarm": rt.alarm_active,
            "error": st.error,
        }

    def _aggregate_countdown_s(self) -> Optional[int]:
        vals = []
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is None:
                continue
            v = self._ups_countdown_remaining_s(u, rt)
            if v is not None:
                vals.append(v)
        return min(vals) if vals else None

    def _aggregate_comm_loss_s(self) -> Optional[int]:
        vals = []
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is None:
                continue
            v = self._ups_comm_loss_remaining_s(u, rt)
            if v is not None:
                vals.append(v)
        return min(vals) if vals else None

    def snapshot(self) -> dict:
        ups_list = []
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is not None:
                ups_list.append(self._ups_snapshot(u, rt))

        hosts = []
        for h in self.cfg.ordered_hosts():
            st = self.host_states.get(h.key, {})
            feed_ids = self.cfg.feed_ids_for(h)
            feeds = []
            for uid in feed_ids:
                u = self.cfg.ups_by_id(uid)
                rt = self.ups_rt.get(uid)
                feeds.append(
                    {
                        "id": uid,
                        "name": u.label if u else uid,
                        "triggered": bool(rt.triggered) if rt else False,
                    }
                )
            hosts.append(
                {
                    "name": h.name,
                    # getattr default: a config that predates the type field still renders.
                    "id": h.key,
                    "type": getattr(h, "type", "pve"),
                    "this_host": h.this_host,
                    # Cluster membership as configured, plus the name discovered from the
                    # API (None until a cluster has been inspected). The UI shows it on the
                    # collapsed host card, the way "this host" shows its star.
                    "cluster": bool(getattr(h, "cluster", False)),
                    "cluster_name": st.get("cluster_name") or None,
                    "order": h.order,
                    "ups_ids": list(h.ups_ids),
                    "ups_policy": h.ups_policy,
                    "feeds": feeds,
                    "eligible": self._host_trigger_reason(h) is not None,
                    "pending_reason": self._host_trigger_reason(h),
                    "reachable": st.get("reachable"),
                    "shutdown_state": st.get("shutdown_state", "idle"),
                    "last_action_at": st.get("last_action_at"),
                    "last_error": st.get("last_error"),
                    # Last self-test outcome (None until the first run).
                    "credentials_ok": st.get("credentials_ok"),
                    "power_mgmt_ok": st.get("power_mgmt_ok"),
                    # Member of proxmox.NODE_STATES; None until anything checked. "ok"
                    # and "unverified" are both fine, the rest is a misnamed entry.
                    "node_state": st.get("node_state"),
                    "last_test_at": st.get("last_test_at"),
                    "last_test_error": st.get("last_test_error"),
                }
            )

        return {
            "appliance": {
                "version": __version__,
                "uptime_s": int((_now() - self.started_at).total_seconds()),
                "engine_state": self.state,
                "dry_run": self.cfg.dry_run,
                "config_valid": self.cfg.configured,
                "alarm": self.alarm_active,
                "last_selftest_at": (
                    self.last_selftest_at.isoformat() if self.last_selftest_at else None
                ),
                "last_selftest_ok": self.last_selftest_ok,
                # Naive LOCAL time (no offset), unlike last_selftest_at above: it is a
                # position on the wall-clock grid, not a UTC instant.
                "next_selftest_at": (
                    (
                        self.last_selftest_slot
                        + timedelta(minutes=self.cfg.selftest_interval_min)
                    ).isoformat()
                    if self.cfg.selftest_enabled and self.last_selftest_slot
                    else None
                ),
                # Which guest this appliance runs in, as far as the last inspection could
                # tell. Everything is None until one has run — the UI must show "not
                # established", never a confident "not on Ceph".
                "self_guest": self._self_guest_snapshot(),
                "shutdown_budget_s": shutdown_budget_s(self.cfg.thresholds),
            },
            "ups": ups_list,
            "shutdown": {
                "triggered": self.shutdown_triggered,
                "reason": self.shutdown_reason,
                "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
                "countdown_remaining_s": self._aggregate_countdown_s(),
                "comm_loss_remaining_s": self._aggregate_comm_loss_s(),
            },
            "hosts": hosts,
            "clusters": self.cluster_snapshot(),
        }

    def _self_guest_snapshot(self) -> dict:
        """The appliance's own guest as the last inspection saw it, across all clusters.

        Only one guest can be ours, so the first cluster that resolved one wins; the
        configured values are echoed either way so the settings page can show what it is
        looking for even before anything has been inspected.
        """
        ap = self.cfg.appliance
        out = {
            "vmid": ap.self_vmid,
            "node": ap.self_node,
            "external": ap.self_external,
            "label": "",
            "source": "none",
            "on_ceph": None,
            "hostname": _hostname(),
        }
        for state in self.cluster_guest_state.values():
            if state.get("self_vmid") is None and state.get("self_source") == "none":
                continue
            out.update(
                {
                    "vmid": state.get("self_vmid", ap.self_vmid),
                    "node": state.get("self_node") or ap.self_node,
                    "label": state.get("self_label", ""),
                    "source": state.get("self_source", "none"),
                    "on_ceph": state.get("self_on_ceph"),
                }
            )
            if state.get("self_vmid") is not None:
                break
        return out

    def cluster_snapshot(self) -> list[dict]:
        """Per-cluster monitoring information from the last inspection.

        Purely informational, like the per-host self-test results: it never influences
        the engine state or the health endpoint's HTTP code. Empty until the first
        self-test has run, or when no host has cluster preparation enabled.
        """
        out = []
        for name, info in self.cluster_states.items():
            out.append(
                {
                    "name": name,
                    "quorate": info.quorate,
                    "nodes": list(info.nodes),
                    "nodes_online": info.nodes_online,
                    "ceph_configured": info.ceph_configured,
                    "ceph_flags_set": info.ceph_flags_set,
                    "ha_services": info.ha_services,
                    "ha_armed_state": info.ha_armed_state,
                    "disarm_supported": info.disarm_supported,
                    "shutdown_policy": info.shutdown_policy,
                    "needs_recovery": info.needs_recovery,
                    "prepared": bool(self.cluster_prepared.get(name)),
                    "guests_total": len(info.guests),
                    "guests_running": len(info.running_guests),
                    "guests_readable": info.guests_read,
                    "mon_nodes": list(info.mon_nodes),
                    "self_guest_vmid": info.self_guest.vmid if info.self_guest else None,
                    "self_guest_on_ceph": info.self_guest_on_ceph,
                    "prep_steps": list(self.cluster_prep_steps.get(name, [])),
                }
            )
        return out
