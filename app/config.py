"""Configuration model and persistence.

The entire appliance is configured through a *single* YAML file, written exclusively
by the web UI. No hand-editing of config files anywhere — that is the whole point of
this project. Secrets live in the same file, which is created with 0600 permissions.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import os
import secrets
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

# Location can be overridden for tests / local runs.
CONFIG_PATH = Path(os.environ.get("PVE_USV_CONFIG", "/etc/pve-usv/config.yaml"))

# Selectable self-test intervals in minutes. Every one of them divides 1440 evenly, so the
# schedule forms an exact daily grid anchored at ``selftest_hour`` (see engine.selftest_slot).
# The <select> in app/web/index.html must offer exactly these values.
SELFTEST_INTERVALS = (15, 30, 60, 120, 180, 360, 720, 1440)


class UpsSourceType(str, Enum):
    """How a UPS is read. The value is the ``type`` discriminator in the config.

    Every member needs a matching ``src.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    snmp = "snmp"  # UPS with an SNMP network card, RFC 1628
    nut = "nut"  # any Network UPS Tools server (upsd) over TCP


class SnmpVersion(str, Enum):
    v1 = "v1"
    v2c = "v2c"
    v3 = "v3"


class SnmpMib(str, Enum):
    """Which UPS MIB the SNMP poller reads (see app/ups.py: PROFILES).

    Every member needs a matching ``mib.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    auto = "auto"  # read RFC 1628, switch to a vendor MIB when the device answers it
    rfc1628 = "rfc1628"  # the standard UPS-MIB only
    apc = "apc"  # APC PowerNet-MIB (1.3.6.1.4.1.318) only


class SnmpAuthProto(str, Enum):
    none = "none"
    md5 = "md5"
    sha = "sha"
    sha256 = "sha256"
    sha512 = "sha512"


class SnmpPrivProto(str, Enum):
    none = "none"
    des = "des"
    aes = "aes"
    aes256 = "aes256"


class UpsThresholdOverride(BaseModel):
    """Optional per-UPS override of the global trigger thresholds.

    Every field defaults to ``None`` meaning "inherit the global value". Only the
    trigger-relevant thresholds are overridable; the loop cadence
    (poll intervals, ``unreachable_alarm_after_polls``) stays global.
    """

    on_battery_seconds: Optional[int] = None
    runtime_below_minutes: Optional[int] = None
    charge_below_percent: Optional[int] = None
    on_battery_low: Optional[bool] = None
    comm_loss_shutdown_after_min: Optional[int] = None
    keep_shutdown_on_comm_loss: Optional[bool] = None


class UpsBase(BaseModel):
    """Fields every UPS source shares, regardless of how it is read.

    The engine, the thresholds and the host↔UPS mapping only ever touch these — a new
    source type therefore inherits the whole trigger/policy/fail-safe machinery.
    """

    # Identity (multi-UPS): ``id`` is a stable slug referenced by hosts, ``name`` is the
    # human label shown in the UI. ``id`` is auto-filled on save if left empty.
    id: str = ""
    name: str = ""

    # Optional per-UPS threshold override (None fields inherit the global thresholds).
    overrides: UpsThresholdOverride = UpsThresholdOverride()

    @property
    def configured(self) -> bool:
        """True once the source has enough information to be polled."""
        return False

    @property
    def label(self) -> str:
        """Display label, falling back to id and a per-type hint when no name is set."""
        return self.name or self.id or self._fallback_label() or "UPS"

    def _fallback_label(self) -> str:
        return ""

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        """Secret field names -> default value, for the API's masked-secret reconcile."""
        return {}


class SnmpConfig(UpsBase):
    # Plain string literal (not the enum) so a YAML/JSON "snmp" validates directly and
    # round-trips through yaml.safe_dump untouched. UpsSourceType stays the single list
    # of valid values; tests/test_i18n.py keeps both in sync.
    type: Literal["snmp"] = "snmp"

    host: str = ""
    port: int = 161
    version: SnmpVersion = SnmpVersion.v2c
    timeout_s: float = 3.0
    retries: int = 1

    # Which MIB to read. "auto" needs no migration: a config written before 3.3.0 has no
    # ``mib`` key and picks up this default, which is exactly what an existing APC device
    # wants. See app/ups.py for the profiles and how "auto" resolves.
    mib: SnmpMib = SnmpMib.auto

    # v1/v2c
    community: SecretStr = SecretStr("public")

    # v3
    v3_user: str = ""
    v3_auth_proto: SnmpAuthProto = SnmpAuthProto.sha
    v3_auth_pass: SecretStr = SecretStr("")
    v3_priv_proto: SnmpPrivProto = SnmpPrivProto.aes
    v3_priv_pass: SecretStr = SecretStr("")

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def _fallback_label(self) -> str:
        return self.host

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        return {"community": "public", "v3_auth_pass": "", "v3_priv_pass": ""}


class NutConfig(UpsBase):
    """A UPS read from a Network UPS Tools server (``upsd``) over TCP.

    We are a read-only client: PVE-UPS never runs ``upsmon`` and never hands the
    shutdown decision to NUT. Any existing upsd works — a NAS with its built-in UPS
    server, a Raspberry Pi, or the driver bundled with this appliance.
    """

    type: Literal["nut"] = "nut"

    host: str = ""  # upsd host (127.0.0.1 for a locally attached UPS)
    port: int = 3493
    ups_name: str = ""  # section name in upsd's ups.conf, e.g. "ups"
    # Credentials are optional: reading variables is anonymous on a default upsd.
    username: str = ""
    password: SecretStr = SecretStr("")
    timeout_s: float = 3.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.ups_name)

    def _fallback_label(self) -> str:
        return f"{self.ups_name}@{self.host}" if self.host else self.ups_name

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        return {"password": ""}


# Discriminated union of every supported source. Adding a type means: a model here, a
# branch in app/sources.py, an entry in UpsSourceType and the matching i18n keys.
UpsSource = Annotated[Union[SnmpConfig, NutConfig], Field(discriminator="type")]

UPS_SOURCE_MODELS: dict[str, type[UpsBase]] = {
    "snmp": SnmpConfig,
    "nut": NutConfig,
}


class ShutdownMethod(str, Enum):
    api_token = "api_token"


class HostType(str, Enum):
    """Which Proxmox product a shutdown target is. The value is the ``type``
    discriminator in the config.

    Every member needs a matching ``htype.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    pve = "pve"  # Proxmox VE node
    pbs = "pbs"  # Proxmox Backup Server


class HostConfig(BaseModel):
    """Fields every shutdown target shares, regardless of which product it runs.

    The engine, the host↔UPS mapping and the whole trigger machinery only ever touch
    these — a new target type therefore inherits the eligibility, ordering and
    fail-safe logic unchanged. What differs per product (auth header, ACL path,
    privilege name, node path) lives in the client module behind app/targets.py.
    """

    # Identity, mirroring UpsBase and WebhookConfig: ``id`` is a stable slug (auto-filled
    # on save), ``name`` the label. Both are needed because ``name`` is edited — it is the
    # Proxmox node name, and correcting a typo in it must not read as "a different host".
    id: str = ""
    name: str  # Proxmox node name, e.g. "pve01"; a free label for products that ignore it
    api_url: str  # e.g. "https://10.0.0.10:8006"
    method: ShutdownMethod = ShutdownMethod.api_token
    # API token: user@realm!tokenid + secret
    token_id: str = ""  # "ups@pve!shutdown"
    token_secret: SecretStr = SecretStr("")
    verify_tls: bool = False  # PVE ships self-signed certs by default
    this_host: bool = False  # the host carrying this appliance -> shut down last
    order: int = 0  # ascending; this_host is forced last regardless
    enabled: bool = True

    # Multi-UPS: which UPS devices feed this host (by UPS id). Empty = depends
    # on ALL configured UPS (conservative fallback). ``ups_policy`` decides how the
    # feeds combine: "all" = shut down only when every feed has triggered (redundant
    # PSUs, default), "any" = shut down as soon as one feed triggers (split, non-
    # redundant load).
    ups_ids: list[str] = Field(default_factory=list)
    ups_policy: Literal["all", "any"] = "all"

    @property
    def key(self) -> str:
        """Stable runtime key (shutdown latches, per-host state, secret reconcile).

        The id, precisely because it does not change when the entry does. It used to be
        "<type>:<name>", which tied identity to an editable field: correcting a node name
        discarded the stored token secret and the self-test result, and two entries sharing
        a type and a name — a duplicated row with the IP not adjusted — shared one shutdown
        latch, so only the first of them was ever fired.

        Falls back to the old form while the id is still empty (a config loaded but not yet
        through assign_host_ids), because this key must never be blank.
        """
        return self.id or f"{getattr(self, 'type', 'pve')}:{self.name}"

    @property
    def api_node(self) -> str:
        """The ``{node}`` path segment of the shutdown call."""
        return self.name

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        """Secret field names -> default value, for the API's masked-secret reconcile."""
        return {"token_secret": ""}


class PveHostConfig(HostConfig):
    """A Proxmox VE node (the original, and still the default target)."""

    # Plain string literal (not the enum) for the same reason as SnmpConfig.type: a
    # YAML/JSON "pve" validates directly and round-trips through yaml.safe_dump
    # untouched. HostType stays the single list of valid values.
    type: Literal["pve"] = "pve"

    # --- cluster preparation (PBS has no cluster, hence only here) -------------
    # Opt-in: writing to a cluster's Ceph and HA state is never done unasked. Members of
    # one cluster are grouped at runtime by the cluster name discovered from the API,
    # not from configuration.
    cluster: bool = False
    # Whether the whole cluster goes down as soon as ONE of its nodes is due.
    #
    # Default on, because the preparation is already cluster-wide: it disarms HA and — with
    # the Ceph option — stops every guest in the cluster. Shutting down only the nodes whose
    # own UPS happened to trigger then leaves the rest standing with no guests, HA disarmed
    # and the maintenance flags set. On a hyper-converged cluster that is worse than it
    # sounds: take two of three monitors down and Ceph has no quorum, so the survivors have
    # no working storage either. Matching the shutdown to the preparation is the only
    # coherent option; the alternative is two different notions of "the cluster".
    #
    # Turn it off for a plain cluster where HA should recover the guests of a single failing
    # node onto the others — there a partial shutdown is a perfectly good outcome.
    cluster_shutdown_all: bool = True
    # Split because the privileges differ sharply: the Ceph flags need Sys.Modify, while
    # arming/disarming HA needs Sys.Console — effectively shell access to the nodes. Who
    # only wants the Ceph part must not be forced to hand out Sys.Console.
    #
    # Off by default, unlike the disarm below: Ceph is the exception rather than the rule
    # (ZFS replication, NFS/iSCSI), and on a cluster without it every single attempt would
    # fail. Writing into someone's storage layer is opted into, never inherited from
    # ticking "this is a cluster member".
    #
    # This one switch covers the WHOLE hyper-converged procedure, not just the flags:
    # stopping every guest cluster-wide first, then setting the flags (see
    # cluster._prepare). The guest stop deliberately has no switch of its own — with Ceph
    # it is not optional but the first step of the official procedure, and a tick whose
    # absence hangs the cluster during a power cut would be a trap. Without Ceph nothing
    # of this runs, which is why a plain cluster is unaffected.
    cluster_ceph: bool = False
    # Kept even where the endpoint is missing (PVE < 9.2): the value is checked against
    # runtime feature detection, so an upgrade to 9.2 activates it without the user
    # having to notice and re-tick anything.
    cluster_ha_disarm: bool = True


class PbsHostConfig(HostConfig):
    """A Proxmox Backup Server instance (default API port 8007)."""

    type: Literal["pbs"] = "pbs"

    @property
    def api_node(self) -> str:
        # PBS ignores the {node} path segment — its router matches any value and the
        # handler does not even take it — and its own web UI calls /nodes/localhost/…
        # So ``name`` is a free label here and the path stays constant, which removes
        # the "wrong node name" guessing that made PBS look unsupported.
        return "localhost"


# Discriminated union of every supported shutdown target. Adding a type means: a model
# here, a branch in app/targets.py, an entry in HostType and the matching i18n keys.
ShutdownTarget = Annotated[Union[PveHostConfig, PbsHostConfig], Field(discriminator="type")]

TARGET_MODELS: dict[str, type[HostConfig]] = {
    "pve": PveHostConfig,
    "pbs": PbsHostConfig,
}


class ApplianceConfig(BaseModel):
    """Where this appliance itself runs, so a cluster-wide guest shutdown skips it.

    One appliance runs in one guest, so this is a single top-level block rather than a
    per-host field: with three cluster members the same fact would otherwise be entered
    three times and could be entered inconsistently. It is not a per-*cluster* setting
    either — there is no cluster config object by design (members are grouped at runtime
    by the name the API reports).

    ``self_vmid`` is picked from a list in the UI (POST /api/cluster/guests), never typed:
    a mistyped id means the appliance shuts *itself* down in the middle of an outage.
    ``self_node`` is written along with that choice and disambiguates two guests sharing
    a name. Both empty means "not picked yet" — the guest stop then refuses rather than
    guessing (see engine._prepare_clusters).
    """

    self_vmid: Optional[int] = None
    self_node: str = ""
    # Explicit opt-out for an appliance that genuinely is not a guest of a managed
    # cluster (Docker on a NAS, bare metal). Deliberately NOT inferred from
    # PVE_USV_DEPLOYMENT=docker: a Docker container can perfectly well run inside a VM
    # of the very cluster being shut down.
    self_external: bool = False


class Thresholds(BaseModel):
    """Shutdown triggers. Any condition that is met (and enabled) fires the shutdown."""

    on_battery_seconds: Optional[int] = 600  # on battery longer than this
    runtime_below_minutes: Optional[int] = 10  # estimated runtime under this
    charge_below_percent: Optional[int] = 30  # battery charge under this
    on_battery_low: bool = True  # UPS reports batteryLow/Depleted

    poll_interval_normal_s: int = 30
    poll_interval_battery_s: int = 8

    # If the UPS is unreachable, do NOT shut down (fail safe, not fail shutdown).
    # We only raise an alarm. This is intentional and stays the default.
    unreachable_alarm_after_polls: int = 3

    # OPT-IN override of the fail-safe: if set, a *pure* communication loss (SNMP
    # unreachable) for this many minutes triggers a shutdown anyway. None = off
    # (recommended default); only set this when a comms loss must be treated like
    # an outage. The power state is unknown while unreachable — use with care.
    comm_loss_shutdown_after_min: Optional[int] = None

    # If communication is lost *while already on battery* (a confirmed outage, e.g. a
    # switch between us and the UPS just lost power), do NOT abort the shutdown: keep the
    # on_battery_seconds countdown running on our own clock and fire when it elapses. A
    # pure comms loss on mains stays fail safe (alarm only). Only effective when
    # on_battery_seconds is set (default 600 s) — runtime/charge are unreadable while blind.
    keep_shutdown_on_comm_loss: bool = True

    # Seconds to wait for a guest/node shutdown to be accepted before moving on.
    host_shutdown_timeout_s: int = 60

    # How long mains have to be back — every UPS reachable, on mains and no longer
    # triggered — before the appliance releases the shutdown latches by itself and is
    # ready for the next outage. Without it the latches only ever came off through the
    # manual "Reset state" button or a restart of the service, so a second outage found
    # every host still flagged as fired and shut down nothing. The delay exists because
    # mains coming back is not the same as mains staying: a grid that flickers would
    # otherwise re-arm between two dips. None = never, i.e. manual only (the pre-4.0
    # behaviour). Restoring a prepared cluster stays manual either way.
    rearm_after_mains_min: Optional[int] = 5

    # Total budget for the cluster preparation (Ceph flags + HA disarm) per cluster.
    # It runs while the battery drains, so it is a hard ceiling, not a per-call timeout —
    # but it IS the budget the HA disarm waits out (see cluster._prepare), so a value
    # under half a minute mostly buys a failed verification: CRM and every LRM work in
    # rounds of ten seconds, and the disarm is only done once the last watchdog is
    # released. The nodes of that cluster wait for this, so it is time off the battery.
    cluster_prep_timeout_s: int = 60

    # What to do when the preparation fails or times out. Default: shut down anyway.
    # A failed disarm leaves the LRM armed, and an armed LRM still stops the guests
    # itself — degraded (HA churn on the other nodes) but safe. Aborting instead would
    # mean letting the cluster lose power uncontrolled when the battery runs out.
    cluster_abort_on_prep_failure: bool = False

    # Budget for the cluster-wide guest shutdown, which runs between the HA disarm and
    # the Ceph flags. Deliberately its own number and NOT a share of the one above: that
    # one is documented as the time the HA disarm gets and is measured in CRM/LRM rounds
    # of ten seconds (a control-plane property, essentially constant), while this one is
    # measured against how long *this* estate takes to stop and scales with the number of
    # guests. Folding them together would silently reinterpret an existing setting and
    # start starving the disarm.
    cluster_guest_shutdown_timeout_s: int = 300

    # After how many seconds a guest that ignores the shutdown request is stopped hard.
    # None = never force, i.e. a hung guest makes the preparation fail and says so. The
    # value is also handed to Proxmox as the shutdown call's own timeout, so the kill
    # still happens if we lose the network right after asking.
    cluster_guest_force_after_s: Optional[int] = 120


class WebhookFormat(str, Enum):
    """Shape of the payload the webhook POSTs (see app/notify.py: FORMATTERS).

    Every member needs matching ``whfmt.<value>`` and ``whfmt.<value>Help`` i18n keys in
    en.js *and* de.js (enforced by tests/test_i18n.py).
    """

    json = "json"  # {"subject", "body", "severity", "status"} — the full status snapshot
    teams = "teams"  # Microsoft Teams adaptive card (Workflows / incoming webhook)
    text = "text"  # human-readable plain text, sent as text/plain
    slack = "slack"  # Slack incoming webhook (attachment with a colour bar)
    discord = "discord"  # Discord webhook (embed with fields)
    ntfy = "ntfy"  # ntfy.sh: plain body plus Title/Priority/Tags headers
    custom = "custom"  # user-defined body with {{placeholder}} substitution


class WebhookLevel(str, Enum):
    """Lowest event severity that still fires the webhook. Values match app/db.py.

    Every member needs a matching ``whlvl.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    info = "info"  # everything, including routine notices ("mains power restored")
    warning = "warning"  # default: warnings and critical events
    critical = "critical"  # only what needs an immediate reaction


class WebhookConfig(BaseModel):
    # Identity, mirroring UpsBase: ``id`` is a stable slug (auto-filled on save), ``name``
    # the label shown in the UI. Several webhooks may point at different chat systems.
    id: str = ""
    name: str = ""

    enabled: bool = False
    url: str = ""
    # POSTs a rendered notification on each notable event that passes ``min_severity``.
    format: WebhookFormat = WebhookFormat.json
    min_severity: WebhookLevel = WebhookLevel.warning

    # ``custom`` format only: the body to send, with {{placeholder}} substitution (see
    # app/notify.py: _render_custom). Deliberately NOT an expression language — plain
    # replacement covers the target systems without putting a template engine on the
    # notification path.
    template: str = ""
    content_type: str = "application/json"

    # Optional single auth header (e.g. "Authorization: Bearer …" for ntfy, or an API-key
    # header). One named header rather than a free-form map: it covers the real cases and
    # keeps the value on the proven masked-secret path below.
    auth_header_name: str = ""
    auth_header_value: SecretStr = SecretStr("")

    @property
    def label(self) -> str:
        """Display label, falling back to the id and finally the format."""
        return self.name or self.id or getattr(self.format, "value", self.format) or "Webhook"

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        """Secret field names -> default value, for the API's masked-secret reconcile."""
        return {"auth_header_value": ""}

    @field_validator("format", mode="before")
    @classmethod
    def _normalise_format(cls, value):
        """Fall back to the plain JSON payload instead of rejecting an unknown format.

        Same rule as the self-test validators below: a backup from another version must
        still import — a webhook in the wrong shape is fixable, a refused import is not.
        """
        try:
            return WebhookFormat(value)
        except (ValueError, TypeError):
            return WebhookFormat.json

    @field_validator("min_severity", mode="before")
    @classmethod
    def _normalise_min_severity(cls, value):
        """Unknown level -> the default; never reject the import."""
        try:
            return WebhookLevel(value)
        except (ValueError, TypeError):
            return WebhookLevel.warning


class Notifications(BaseModel):
    # Legacy configs (< 3.0.0) may still contain a ``smtp`` block; Pydantic ignores
    # unknown keys, so it is dropped silently on load and gone after the next save.
    webhooks: list[WebhookConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_webhook_list(cls, data):
        """Turn the pre-4.0 single ``webhook: {...}`` block into a one-element list.

        Same rule as the UPS/host migrations: an existing config.yaml, a backup import and
        the form POST of a stale, cached app.js all pass through here, so none of them may
        be refused over a key whose shape simply changed. A malformed block is dropped
        rather than rejected — losing one webhook is fixable, a refused import is not.
        """
        if not isinstance(data, dict):
            return data
        legacy = data.pop("webhook", None)
        if legacy is not None and not data.get("webhooks"):
            if isinstance(legacy, BaseModel):
                legacy = legacy.model_dump()
            if isinstance(legacy, dict):
                legacy = dict(legacy)
                legacy.setdefault("id", "webhook1")
                data["webhooks"] = [legacy]
        return data


class AppConfig(BaseModel):
    # Marks whether the setup wizard has been completed at least once.
    configured: bool = False

    # Master safety switch: when True the engine only logs, never shuts anything down.
    dry_run: bool = True

    ups: list[UpsSource] = Field(default_factory=list)
    hosts: list[ShutdownTarget] = Field(default_factory=list)
    thresholds: Thresholds = Thresholds()
    notifications: Notifications = Notifications()
    # Where the appliance itself runs. A config written before this existed validates to
    # the defaults, so no migration validator is needed.
    appliance: ApplianceConfig = ApplianceConfig()

    # Scheduled self-test: verify the API token + power-management privilege still work,
    # so a broken/expired credential is caught long before a real outage needs it.
    selftest_enabled: bool = True
    selftest_hour: int = 9  # anchor: hour of day (0-23, server local time)
    selftest_interval_min: int = 1440  # repeat every N minutes from the anchor; 1440 = daily

    # Optional NTP server pushed to the container's systemd-timesyncd (empty = leave
    # the system default untouched). Applied by the privileged deploy agent.
    ntp_server: str = ""

    # Optional IANA timezone (e.g. "Europe/Berlin") applied to the container by the
    # privileged deploy agent (empty = leave the system default, usually UTC, untouched).
    # Matters because the self-test schedule is interpreted in the container's local time.
    timezone: str = ""

    # Web UI auth. Read-only endpoints (/api/status, /api/health) are NOT protected.
    ui_password_hash: str = ""
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))

    @model_validator(mode="before")
    @classmethod
    def _migrate_ups_list(cls, data):
        """Bring older UPS schemas up to the current ``ups: [{type: ..., ...}]`` shape.

        Two steps, both required for an existing ``config.yaml`` to keep working:

        * pre-2.0 had a single ``snmp: {...}`` block and hosts without ``ups_ids`` — it
          is wrapped into one UPS ``id="ups1"`` that every host is pointed at;
        * pre-3.2 UPS entries have no ``type`` at all, and the discriminated union would
          reject them. SNMP was the only source back then, so that is the default.

        An import must never be refused (same rule as the self-test validators below).
        """
        if not isinstance(data, dict):
            return data
        legacy = data.pop("snmp", None)
        if legacy is not None and not data.get("ups"):
            if not isinstance(legacy, dict):  # a SnmpConfig instance
                legacy = legacy.model_dump() if isinstance(legacy, BaseModel) else None
            if isinstance(legacy, dict):
                legacy = dict(legacy)
                legacy.setdefault("id", "ups1")
                legacy.setdefault("name", "UPS")
                data["ups"] = [legacy]
                for host in data.get("hosts", []) or []:
                    if isinstance(host, dict) and not host.get("ups_ids"):
                        host["ups_ids"] = ["ups1"]
        ups = data.get("ups")
        if isinstance(ups, list):
            data["ups"] = [
                {**u, "type": u.get("type") or "snmp"} if isinstance(u, dict) else u for u in ups
            ]
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_host_list(cls, data):
        """Give pre-3.5 hosts the ``type`` the discriminated union needs.

        Proxmox VE was the only shutdown target back then, so a host without a type is
        a PVE node. This runs for every entry point that builds a config — the stored
        config.yaml, a backup import, and the form POST of a stale, cached app.js — so
        none of them can be refused over a key that simply did not exist yet.

        Only a missing/empty type is filled in. An unknown one is left alone and the
        union rejects it: mistaking one product for another would send the wrong auth
        header to a real machine, which is worse than a clear error.
        """
        if not isinstance(data, dict):
            return data
        hosts = data.get("hosts")
        if isinstance(hosts, list):
            data["hosts"] = [
                {**h, "type": h.get("type") or "pve"} if isinstance(h, dict) else h for h in hosts
            ]
        return data

    @field_validator("selftest_interval_min", mode="before")
    @classmethod
    def _normalise_selftest_interval(cls, value):
        """Snap an unsupported interval to the daily default instead of rejecting it.

        A backup from another version or a hand-edited YAML must still import: a
        self-test running on the wrong cadence is cosmetic, a refused import is not.
        """
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return 1440
        return minutes if minutes in SELFTEST_INTERVALS else 1440

    @field_validator("selftest_hour", mode="before")
    @classmethod
    def _clamp_selftest_hour(cls, value):
        """Clamp to 0-23 — an out-of-range hour must not break the import either."""
        try:
            return min(23, max(0, int(value)))
        except (TypeError, ValueError):
            return 9

    def effective_thresholds(self, ups: UpsBase) -> Thresholds:
        """Global thresholds with this UPS's non-None overrides applied."""
        ov = ups.overrides
        merged = self.thresholds.model_copy()
        for field in (
            "on_battery_seconds",
            "runtime_below_minutes",
            "charge_below_percent",
            "on_battery_low",
            "comm_loss_shutdown_after_min",
            "keep_shutdown_on_comm_loss",
        ):
            val = getattr(ov, field)
            if val is not None:
                setattr(merged, field, val)
        return merged

    def ups_by_id(self, ups_id: str) -> Optional[UpsBase]:
        for u in self.ups:
            if u.id == ups_id:
                return u
        return None

    def feed_ids_for(self, host: HostConfig) -> list[str]:
        """UPS ids feeding a host; empty ups_ids means "all configured UPS"."""
        if host.ups_ids:
            return [i for i in host.ups_ids if self.ups_by_id(i) is not None]
        return [u.id for u in self.ups]

    def ordered_hosts(self) -> list[HostConfig]:
        """Enabled hosts in shutdown order; the appliance's own host always last."""
        active = [h for h in self.hosts if h.enabled]
        return sorted(active, key=lambda h: (h.this_host, h.order, h.name))

    def duplicate_api_urls(self) -> list[str]:
        """API URLs shared by more than one enabled target, normalised.

        Almost always a copy-paste slip (entry duplicated, IP not adjusted), and the one
        state in which the node name still decides where a shutdown lands — see
        api_url_is_unique(). Across all types on purpose: two Backup Servers on one URL is
        the same mistake as two PVE nodes.
        """
        seen: dict[str, int] = {}
        for host in self.hosts:
            if host.enabled:
                key = _norm_api_url(host.api_url)
                seen[key] = seen.get(key, 0) + 1
        return sorted(url for url, count in seen.items() if url and count > 1)

    def api_url_is_unique(self, host: HostConfig) -> bool:
        """Whether this entry is the only enabled target behind its API URL.

        This is what makes "the node behind this URL" unambiguous, and therefore what
        decides whether the shutdown may address /nodes/localhost instead of a configured
        name (see app/proxmox.py). Where a URL serves several entries, PVE's proxying is
        the only thing that tells them apart, so the name has to stay in the path.
        """
        return _norm_api_url(host.api_url) not in self.duplicate_api_urls()


def _norm_api_url(url: str) -> str:
    """Compare form of an API URL — case and a trailing slash are not a difference."""
    return (url or "").strip().rstrip("/").lower()


def _slugify(text: str, fallback: str = "ups") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or fallback


def assign_ups_ids(ups: list[UpsBase]) -> None:
    """Fill empty UPS ids with stable, collision-free slugs (in place)."""
    taken = {u.id for u in ups if u.id}
    for i, u in enumerate(ups, start=1):
        if u.id:
            continue
        base = _slugify(u.name) if u.name else f"ups{i}"
        candidate = base
        n = 2
        while candidate in taken or candidate == "":
            candidate = f"{base}-{n}"
            n += 1
        u.id = candidate
        taken.add(candidate)


def assign_host_ids(hosts: list[HostConfig]) -> None:
    """Fill empty host ids with stable, collision-free slugs (in place).

    Same job as assign_ups_ids, and needed for the same reason: the id is what the UI's
    per-card secret reconcile matches on, and what the engine latches shutdowns against —
    so it has to survive renaming, reordering, and two entries carrying the same name.
    """
    taken = {h.id for h in hosts if h.id}
    for i, h in enumerate(hosts, start=1):
        if h.id:
            continue
        base = _slugify(h.name, f"host{i}") if h.name else f"host{i}"
        candidate = base
        n = 2
        while candidate in taken or candidate == "":
            candidate = f"{base}-{n}"
            n += 1
        h.id = candidate
        taken.add(candidate)


def assign_webhook_ids(hooks: list[WebhookConfig]) -> None:
    """Fill empty webhook ids with stable, collision-free slugs (in place).

    Same job as assign_ups_ids: the id is what the UI's per-card secret reconcile matches
    on, so it has to survive renaming and reordering.
    """
    taken = {h.id for h in hooks if h.id}
    for i, h in enumerate(hooks, start=1):
        if h.id:
            continue
        base = _slugify(h.name) if h.name else f"webhook{i}"
        candidate = base
        n = 2
        while candidate in taken or candidate == "":
            candidate = f"{base}-{n}"
            n += 1
        h.id = candidate
        taken.add(candidate)


def _to_serialisable(cfg: AppConfig) -> dict:
    """Dump the model to plain types: reveal SecretStr, unwrap Enums (so YAML works)."""

    def reveal(obj):
        if isinstance(obj, SecretStr):
            return obj.get_secret_value()
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: reveal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [reveal(v) for v in obj]
        return obj

    return reveal(cfg.model_dump(mode="python"))


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Load config from disk, or return defaults if it does not exist yet."""
    if not path.exists():
        return AppConfig()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    cfg = AppConfig.model_validate(data)
    # A config written before hosts had ids gets them here, so the runtime keys and the
    # ids the UI sends back are id-based from the first request on. Nothing is written to
    # disk until the next save, which is enough: the assignment is deterministic.
    #
    # UPS ids for the same reason, and because an empty one is worse than a wrong one: the
    # engine keys its per-UPS runtime on the id, so a hand-written entry without one would
    # be polled and its answer then dropped — the device would simply never trigger.
    assign_ups_ids(cfg.ups)
    assign_host_ids(cfg.hosts)
    return cfg


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    """Persist config to a single YAML file with 0600 permissions, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = _to_serialisable(cfg)
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
