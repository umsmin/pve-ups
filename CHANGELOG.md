# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

The single source of the version is `app/__init__.py` (`__version__`); `pyproject.toml`
reads it dynamically. On every release: bump `__version__` **and** add a section here.

## [Unreleased]

## [4.0.0] - 2026-08-29

The major number marks the scope of this release — cluster support as a new core topic —
and one REST-visible break: `/api/config` now returns `notifications.webhooks` (a list)
where it returned `notifications.webhook`, which a script reading that field has to follow.
Everything else is compatible: updates from 3.x install unchanged, an existing
`config.yaml` loads as it is (the single webhook is migrated into a one-element list on
load), and `/api/status` and `/api/health` only gained fields.

The **cluster preparation ships as Beta**. Being a new core topic and being new are not in
conflict: the mechanism is deliberately conservative — every step is verified rather than
assumed, the whole sequence runs under one hard timeout, and the defaults are the safe ones
— but it has run against few real clusters so far, and the variety out there is the part no
amount of care substitutes for. It is opt-in and does nothing until switched on.

### Fixed
- **The appliance re-arms itself once mains are back.** A shutdown that was really sent
  latched its host for good — deliberately, so a machine that is powering down is never
  told twice — but nothing ever released that latch again. The dashboard stayed on
  "Shutting down" with every UPS long since back on mains, and, worse, a *second* outage
  shut down nothing at all: every host still counted as already fired. The scheduled
  self-test, the start-up checks and the "Restore cluster" button all stand down while a
  shutdown is in progress, so they stayed down too. Only the "Reset state" button or a
  restart of the service brought the appliance back. It now releases the latches by
  itself once every UPS has been reachable and on mains for `rearm_after_mains_min`
  minutes (new, default 5, empty = manual only as before). The delay is the point: a grid
  that dips twice in a minute must not re-arm in between, and an unreachable UPS never
  counts as "mains are back". Restoring a prepared cluster stays manual — the re-arm only
  makes that button reachable again, and says so in the event when a cluster is still
  carrying the preparation.
- **"Reset state" no longer discards the self-test results.** The shutdown state and the
  credential/node-name verdicts share one structure per host, and the button cleared all
  of it, so `/api/health` reported "never tested" until the next scheduled slot — up to a
  day later. Only the shutdown keys are dropped now.
- **A cluster without Ceph is no longer asked to set Ceph flags.** The Ceph step was passed
  through unfiltered while the HA disarm was checked against feature detection, so on a
  cluster running ZFS replication or NFS/iSCSI every outage ran the full failure path — a
  rejected bulk PUT, four rejected single PUTs, a verification loop polling into the void —
  and reported a CRITICAL "preparation FAILED" for a component that is not installed. With
  "abort on failure" enabled it held every node of that cluster back. Both steps are now
  gated on what the cluster can actually do, and *absent* is told apart from *unreadable*:
  a denied read still attempts the write rather than silently skipping it. When neither
  step applies, the outcome is a quiet log line naming why, not a failure.
- **The shutdown preparation no longer discards its own feature detection.** When no
  self-test had run yet — the normal state after a restart — the cluster was inspected and
  that reading was then thrown away in favour of an empty cache, defaulting to "disarm-ha
  is supported". On Proxmox VE 8.x that meant POSTing an endpoint that does not exist,
  mid-outage, and a cluster held back over it under "abort on failure". The fresh
  inspection is now kept and used.
- **Leftovers from an outage are visible again as soon as the appliance is back.** Cluster
  state was only ever collected by the scheduled self-test, whose latch survives a restart
  — so after the appliance shut itself down last and came back, the next run could be a day
  away. In that window the dashboard showed no cluster and the "Restore cluster" button
  stayed hidden, although the cluster was still disarmed with the maintenance flags set.
  Since restoring is deliberately manual, that button is the only prompt there is. The
  clusters are now inspected once at start-up, independently of the schedule, and anything
  left over is reported.
- **"Restore cluster" is refused while a shutdown is running.** The button appears the
  moment the preparation lands, which is mid-shutdown; arming HA and clearing the flags
  there would undo the preparation at the one moment it is doing its job. The API now
  refuses it — the same guard the manual self-test already had — and the button is hidden.
- **A privilege is only demanded where it can do something.** `Sys.Modify` is no longer
  requested for a cluster without Ceph, nor `Sys.Console` on a release without `disarm-ha`,
  so a warning about a missing privilege always means one that is really needed. Each name
  now carries its purpose — `Sys.Console (HA disarm)` — in the self-test, the host test and
  the diagnostics panel.
- **"Not every node is a configured target" no longer fires at complete setups.** The
  warning counted only hosts carrying the cluster tick, although the tick governs the
  preparation (which runs once per cluster regardless) and not whether a node gets shut
  down. Ticking one member — a perfectly reasonable setup — produced a warning claiming the
  other nodes would be left running, which was false. It now counts every enabled PVE
  target belonging to the cluster.
- **A preparation that runs out of budget reports what it managed.** The timeout cancels
  the sequence mid-write, and "gave up" alone could not be told apart from "never touched
  it" — while half prepared is precisely the state worth knowing about. The event now names
  the completed steps. Same for a timed-out restore.
- **A cluster held back by "abort on failure" is no longer prepared again on every
  poll.** Holding a cluster back leaves no host latched, and that read as "the outage is
  over" — so the preparation latch was cleared and the whole failing sequence ran again on
  the next poll: every eight seconds, each round writing to the cluster and sending a pair
  of critical notifications, while the battery drained. The outcome of the preparation is
  now latched for the episode alongside the fact that it ran, which also fixes the other
  half of the same knot: whether the nodes may go down was derived from the work of a
  single iteration, so the abort stopped applying the moment the preparation was no longer
  being attempted — and the cluster was shut down after all, one poll later.
- **A UPS entry without an id in a hand-written `config.yaml` is no longer polled in
  vain.** The engine keys its per-UPS state on that id, so the answers were read and then
  dropped and the device could never trigger. Ids are now filled in on load, the way host
  ids already were.
- **A single dropped SNMP poll no longer sends a notification.** The "network connection
  lost" event was emitted on the very first failed poll at warning severity, so it passed
  the webhook's default filter — while `unreachable_alarm_after_polls` only ever governed
  the separate "unreachable" alarm further down. A short dropout on a busy network
  therefore produced an immediate webhook. Connection loss is now notified through that
  alarm alone, i.e. only after the configured number of consecutive failed polls, and
  "connection restored" is only notified if the loss was. Both transitions are still
  written to the event log, so diagnostics are unchanged.
- **Uploading a release package failed in Safari on macOS.** Two independent causes, both
  fixed: the file picker's `accept` list only named the compound extension `.tar.gz`, which
  Safari maps to a UTI and greys the file out, and Safari's "open safe files after
  downloading" unpacks the asset to a plain `.tar`, which the frontend, the API and the
  privileged agent all rejected. All three layers now detect the archive format by its
  *content* instead of its file name, so `.tar.gz`, `.tgz`, a Safari-unpacked `.tar` and
  `.zip` are equally accepted.
- **Renaming a host no longer discards its stored API token.** Hosts were identified by
  their type and node name — an *edited* field — so correcting a node name read as "a
  different host": the masked placeholder the UI sends for an unchanged secret found
  nothing to resolve to and silently became empty. The same mismatch made the host test
  answer "Authentication failed (token invalid?)" for a perfectly valid token, which is
  exactly what happened when accepting the node name that test now suggests. Hosts carry a
  stable id, like UPS sources and webhooks already did; a payload without one still matches
  by type and name, so nothing is lost on the way to this version.
- **Two host entries with the same name no longer share one shutdown latch.** They also
  shared their self-test result and shutdown state, so a duplicated entry whose IP was
  never adjusted counted as already fired and was left running. Identity now comes from the
  id, which is unique by construction.
- **A wrong Proxmox VE node name no longer goes unnoticed until the outage.** The node name
  is used verbatim in the shutdown call `POST /nodes/<name>/status`, but nothing ever
  checked it: the connection test only asks `/version` and `/access/permissions`, and
  neither looks at the node segment. A datacenter-wide token therefore let a misspelled
  name — a capital letter, a domain suffix, a label like "Proxmox 1" — pass every check in
  the wizard and in the scheduled self-test, and fail only when the battery was already
  draining. The node name is now verified against the API's own node list, in the host test
  and in the self-test, and once more at start-up so an appliance that was just updated
  reports the problem in seconds rather than at the next scheduled slot (which, with the
  persisted schedule latch, could be a day away). On a cluster the plain node
  index is not enough — every member is "a node this API knows", so any other member's
  name would pass — hence the listing is read from `/cluster/status` where the token may
  (it marks the node that actually answered) and from `/nodes` otherwise. A name that
  belongs to a different member is now reported as such and the right one is offered for a
  click. Without `Sys.Audit` the verdict stops at "this name exists here", which is
  honest rather than wrong.
- **The browser no longer serves the previous UI after an update.** None of the static
  files carried a `Cache-Control` header, so browsers were free to cache them
  heuristically — roughly a tenth of the file's age — and a weeks-old `app.js` could be
  reused for days without a single request reaching the appliance, leaving a new backend
  paired with an old interface. `index.html` is now sent with `no-store`, and every script
  and stylesheet it references gets a `?v=` stamp derived from the file's own
  mtime and size: changed files land under a new URL, unchanged ones stay cached, and
  nothing is hard-coded or generated at build time. Unstamped requests (the manuals, a
  hand-typed asset URL) must revalidate and answer with a 304 when nothing changed.

### Added
- **Several webhooks instead of one.** `notifications.webhook` became
  `notifications.webhooks`, a list with one card per target in the wizard, each with its own
  format, severity filter and test button. An existing single webhook is migrated into a
  one-element list on load, so nothing needs to be re-entered. The sends run concurrently
  and are reported individually: one unreachable target no longer costs the others their
  notification.
- **Slack, Discord and ntfy as payload formats.** Slack posts an attachment with a
  severity colour bar, Discord an embed, ntfy a plain-text push with `Title`, `Priority`
  and `Tags` set from the event.
- **A `custom` format for everything else.** Supply the body and content type yourself and
  use `{{subject}}`, `{{body}}`, `{{severity}}`, `{{severity_upper}}`, `{{facts}}`,
  `{{facts_json}}`, `{{status_json}}`, `{{timestamp}}` and `{{version}}`. Substitution is
  literal — deliberately not an expression language — and values are JSON-escaped when the
  content type is JSON, so a quote in an event text cannot produce a malformed payload.
- **An optional authentication header per webhook**, e.g. `Authorization` for a protected
  ntfy topic or an API-key header. The value is treated as a secret: masked in the API and
  carried over unchanged when settings are saved.
- **Uploaded update packages are validated before they are queued.** The API now checks
  that the upload is a readable archive containing `pyproject.toml` and `app/__init__.py`,
  and rejects it with a clear message otherwise. Previously the package was queued
  unchecked and only the privileged root agent noticed the problem, so a broken upload
  reached the privileged path; it no longer does.
- Release packages can be dropped onto the update card, as a way around browser file
  pickers that filter the archive out.
- **Cluster preparation before a shutdown — Beta (issues #1 and #6).** Once per cluster,
  before its first node goes down, PVE-UPS disarms the HA manager, so services are not
  recovered onto nodes that are shutting down themselves. **Needs Proxmox VE 9.2 or newer**
  (`disarm-ha`); that is now said plainly on the host card, in the host test, in the
  self-test and at the top of the manuals' cluster chapter, because on 8.x the preparation
  is reduced to the Ceph flags. Marked Beta in the UI, both manuals and the READMEs while it
  gathers field experience: the mechanism is conservative, but few real clusters have run
  it. Opt-in throughout, and it changes nothing until switched on.
  The step is *verified* rather than assumed: the disarm is polled until `armed-state`
  actually reaches `disarmed`. It runs under a hard timeout, because it happens while the
  battery drains — and that timeout (`cluster_prep_timeout_s`, default 60 s) is also the
  time the disarm is really given: it is handed down step by step, so raising it does what
  it says. Below roughly half a minute a disarm usually cannot be confirmed, because the HA
  stack answers in rounds of ten seconds and every node has to release its watchdog first.
  If it does not get there, the event names the state it stopped at — "still disarming" is a
  different message from a stack that never moved. A stack that is already disarmed (a
  second outage before the cluster was restored) is left alone instead of being disarmed
  again.
  The preparation **announces itself before it starts**, naming what it will do, how many of
  the cluster's nodes have triggered and how long the nodes may wait for it: it is the one
  step that deliberately delays the shutdown, and without that line the event log jumped
  from the outage straight to a result a minute later. In dry-run mode it is logged and
  nothing is changed.
  `resource-mode` is fixed to `ignore` and is not configurable — under `freeze` the guests
  stay HA-managed, `pve-guests` skips them and the disarmed LRM no longer stops them, so
  they would be killed by the power-off.
  Availability is detected per feature rather than by version number: `disarm-ha` (9.2+) is
  read from the endpoint index — which also covers backports — and is never POSTed blindly.
  If the preparation fails, the shutdown continues by default: an armed HA manager still
  stops the guests itself, which is degraded but safe, whereas aborting would mean losing
  power uncontrolled. `cluster_abort_on_prep_failure` opts into aborting instead.
  The cluster switches sit together in a **set-apart group** on the host card — the master
  switch stays plainly visible (an earlier collapsible section made the feature hard to
  find), while its sub-options appear underneath once it is ticked, listed in the order the
  steps actually run and split where the manuals split. Membership is shown on the
  **collapsed** card too, and next to the host name on the dashboard, the way the appliance
  is marked with a star: the discovered cluster name once it is known, the plain word until
  then. `/api/status` gained `cluster` and `cluster_name` per host for it.
  **Privileges follow the ticks:** `Sys.Audit` to read the cluster and `Sys.Console` for the
  HA disarm, both on `/`. An option left off costs nothing, and both manuals give one ready
  `pveum role add` line per combination instead of a single command granting everything, so
  nobody hands out `Sys.Console` (effectively shell access to the nodes) for something they
  did not switch on. `Sys.PowerMgmt` stays on `/nodes` where the base setup puts it.
- **A cluster is shut down as a unit** (`cluster_shutdown_all`, on by default). The
  preparation is cluster-wide — it disarms HA for the whole cluster, and with the Ceph
  option it stops every guest in it — while the shutdown is per host: a node is due when
  *its* UPS devices say so. When only some UPS devices trigger, those two disagree and the
  cluster is left in halves. Observed on a four-node cluster: one UPS failed, two nodes went
  down, and the two survivors sat there with every guest stopped, HA disarmed and two of
  three Ceph monitors gone — no guests, no HA and no storage quorum. With the switch on,
  every node of the cluster goes down as soon as one of them is due, in the usual `order`
  with the appliance's host last, and the event log names why each node was taken along.
  Turn it off for a plain cluster where HA should move a single failing node's guests onto
  the others; with Ceph it should stay on. When it is off and only part of a cluster shuts
  down, that is now reported as CRITICAL instead of being discovered afterwards.
  The self-test additionally warns when the nodes of one cluster hang on UPS devices that
  can trigger independently, with different wording depending on the switch — and points at
  `comm_loss_shutdown_after_min` for the common case where the UPS management switch hangs
  on the failing UPS itself, so the device goes unreachable instead of reporting battery and
  the fail-safe (correctly) refuses to trigger on it.
- **Hyper-converged clusters (Ceph) — Beta.** With the Ceph option on, the preparation
  follows the official Proxmox order: **disarm HA, stop every guest in the cluster, then set
  the maintenance flags** (`noout,nobackfill,norecover,norebalance`).
  **Why the guests have to stop first.** Letting each node stop its own guests as it powers
  off — what a per-node shutdown does — hangs a hyper-converged cluster: with
  `size=3/min_size=2` the pool falls below `min_size` once the second node's OSDs are gone,
  the guests still running on the survivor block on IO, their shutdown never finishes and
  that node never powers off. Proxmox documents the same sequence ("Shutdown Proxmox VE +
  Ceph HCI cluster"): stop all Ceph clients, then the flags, then the nodes. Every running
  guest is asked to shut down at once (at most 8 requests in flight — asking forty guests
  serially would spend a connect timeout on each), the result is verified by re-reading
  `/cluster/resources`, and a guest that ignores the request is force-stopped after
  `cluster_guest_force_after_s` (default 120 s, empty = never force, which then fails the
  preparation and names the survivors).
  The guest stop has **no switch of its own**: it belongs to the Ceph option, because with
  Ceph it is not optional but the first step of the procedure, and a tick whose absence
  hangs the cluster during a power cut would be a trap. Without Ceph nothing of it runs.
  **`cluster_guest_shutdown_timeout_s` (default 300 s) is its own budget**, deliberately
  not a share of `cluster_prep_timeout_s`: that one is measured in HA rounds of ten seconds
  and barely varies, while this one scales with the number of guests. Sharing one number
  would have the guests eat the disarm's budget on every existing config. The settings page
  shows the resulting worst-case hold-up (disarm + guests + node shutdown), and the
  self-test warns when the runtime trigger fires later than that — it warns only, and never
  adjusts a trigger by itself.
  **This appliance's own guest** is picked from a list under Settings → *This appliance*
  (`appliance.self_vmid`/`self_node`, or "not a guest of this cluster"), never typed: a
  mistyped id would stop the appliance in the middle of an outage. The pick also derives
  the **"this host"** mark on the matching host card, so the node carrying the appliance is
  shut down last without maintaining that fact twice. Until something is picked the guest
  stop is refused outright, loudly, and the shutdown continues without it. The appliance's
  guest must **not live on Ceph storage** — it is the one guest that has to outlive the
  cluster it shuts down — which `install.sh` now enforces and the self-test, the host test
  and the dashboard report at runtime.
  The flags are *verified* rather than assumed: the bulk `PUT /cluster/ceph/flags` is
  asynchronous and only returns a worker id, so they are read back, falling back to the
  synchronous per-flag endpoint — which is also the normal path on releases without the bulk
  variant. The Ceph part is **off by default** and enabled separately from the HA disarm:
  plenty of clusters run on ZFS replication or shared NFS/iSCSI, and this is the one part
  that writes into a storage layer. On a cluster without Ceph it is skipped rather than
  attempted, and its privileges are not required.
  **MON nodes should go last.** The monmap is read from the Ceph status that was fetched
  anyway, and a shutdown order contradicting it is reported in the host test and the
  self-test — never re-sorted automatically, because `order` is explicit configuration and
  silently overriding it would make the sequence shown on the dashboard a lie.
  New privileges follow the Ceph tick: `Sys.Modify`, `VM.Audit` and `VM.PowerMgmt` (the VM
  ones accepted on `/` or `/vms`). `Datastore.Audit` is *advisory* and reported separately —
  it only buys the Ceph-storage check, so its absence is never listed as a missing
  privilege. Note that `/cluster/resources` **filters by privilege instead of refusing**:
  without `VM.Audit` it answers 200 with an empty list, so "empty" is never read as "no
  guests to stop". `POST /api/cluster/guests` serves the guest picker without needing a
  credential test first.
  Both manuals now separate the plain cluster (`#cluster`) from the hyper-converged case
  (`#cluster-ceph`), each with its own privilege table: the difference in scope and in
  required rights was too large for one section.
- **The installer refuses Ceph-backed storage for the container.** `install.sh` now reads
  the storage *type* rather than only its content flags: `rbd`/`cephfs` storages are skipped
  when one is picked automatically and refused when named with `--storage`. The appliance is
  the one guest that has to outlive the cluster it shuts down, and on Ceph it cannot — once
  the pool loses `min_size` its own disk stops answering. `--allow-ceph-storage` overrides
  it deliberately. Refused rather than prompted, because the documented install path is
  `curl … | bash`, where stdin is the script itself.
- **A self-test runs automatically after the appliance re-arms.** Right after a re-arm is
  when a leftover problem shows up — a token that expired during the outage, a node that
  never came back, a cluster still prepared — and waiting hours for the next scheduled slot
  to find that out was the wrong trade. It is queued rather than run on the spot, so it
  never delays the eligibility check of that iteration, and it waits out a new outage
  instead of being lost to it.
- **"Run self-test now" on the dashboard.** Saving settings deliberately does not fire a
  credential check, and the next scheduled slot may be hours away — so a changed token or a
  freshly enabled cluster option could not be verified on the spot. The button runs the
  host and cluster checks immediately and always writes the outcome to the event log,
  bypassing the once-a-day throttle on the quiet "ok" lines. It is refused while a UPS is
  on battery, for the same reason the scheduled run is skipped then.
- **A healthy cluster now says so.** The cluster checks previously only spoke up about
  problems, which left "checked and fine" indistinguishable from "never checked" — a
  working self-test looked like a broken one. A cluster without findings is logged quietly
  as `Cluster <name>: ok` with node count, quorum, Ceph and HA state, at the same daily
  cadence as the per-host lines.
- **"Restore cluster" on the dashboard.** Arms the HA manager again and clears the
  maintenance flags, each verified, and only offered while something is actually left to
  undo. Deliberately manual: there is no automatic re-arm, because bringing HA back while
  nodes are still booting is a judgement call.
- **Cluster awareness for Proxmox VE (read-only part).** A PVE host is marked as a cluster
  member with a flag in the cluster group on the host card; members are grouped automatically by the cluster name read from
  `/cluster/status`, so no second token and no extra configuration section is needed. The
  scheduled self-test now also checks each cluster once and warns — never worse than a
  warning — about Ceph maintenance flags still set, an HA manager still disarmed, missing
  token privileges (named individually, not as a bare 403), a missing quorum, cluster
  nodes that are not configured as targets, and a `shutdown_policy` that would fight the
  shutdown (`migrate` makes the LRM delay it; the default `conditional` recovers services
  onto nodes that are shutting down). `/api/status` and `/api/health` gained a `clusters`
  block — monitoring information only, deliberately not part of the health status or its
  HTTP code, for the same reason as `hosts_ok`.
  The **host "Test" button checks the cluster privileges too** once the option is ticked,
  instead of only `Sys.PowerMgmt`: it names every missing privilege, reports the cluster it
  found (name, node count, Ceph and the HA arm state), and points out a node that is not a
  cluster member or a `disarm-ha` endpoint that does not exist. Missing cluster privileges
  are reported as a warning rather than a failure, since the connection itself works — the
  same convention as an unconfirmed `Sys.PowerMgmt`. Without `Sys.Audit` the check says the
  membership cannot be read instead of claiming the node is standalone. Below the result, a
  **details panel** lists every cluster query with its outcome — `ok`, `not permitted`,
  `not available` or the error — mirroring the UPS test's per-object diagnostics and
  unfolding by itself when something needs attention.
  The arm state of the HA stack and the number of HA-managed guests are tracked
  **separately**: a disarmed stack is reported, and offered for restoring, even on a
  cluster where no guest is HA-managed. (Tying them together had made a disarmed HA
  manager invisible on exactly such a cluster.) The `shutdown_policy` warning stays tied to
  the guests, because with none of them there is nothing that could be recovered onto a
  node that is shutting down.
- **Output load on the UPS status card.** Read from `upsOutputPercentLoad` (RFC 1628),
  `upsAdvOutputLoad` (APC PowerNet) and `ups.load` (NUT). Informational only: it feeds no
  trigger, and a device that does not report it simply shows nothing instead of warning
  about an unavailable shutdown condition.
- **Connection tolerance is configurable in the interface.** The per-UPS query timeout and
  (for SNMP) the retry count already existed in the config file but were not reachable from
  the wizard; they now sit on the UPS card. "Report unreachable after (polls)" joins the
  threshold card with a live hint spelling out what it means in seconds at the current poll
  interval — it is what decides when a connection loss is reported.
- **A reload prompt when the running version changes.** An update restarts the service
  while open tabs keep running the interface they were loaded with. `/api/status` already
  reports the version, so a tab that sees a different one than it started with now shows a
  note with a "Reload" button instead of letting the mismatch play out silently.
- **The shutdown addresses the node behind the host entry's API URL directly.** With one
  API URL per node — what the manual has always required, because a node that is already
  powered off cannot forward the shutdown for the ones still to come — the machine behind
  that URL *is* the intended one, so the call goes to `/nodes/localhost/status` and the
  configured name can no longer misdirect it. This also removes a cluster dependency from
  the shutdown path: Proxmox handles `localhost` locally instead of resolving the node
  through the cluster, which matters precisely when nodes are dropping and quorum is
  shaky. Proxmox Backup Server has always worked this way.
- **A failed shutdown is retried once with the other form of the path** — by name if the
  direct call failed, directly if the name failed. This covers a misspelled name as well as
  a token whose `Sys.PowerMgmt` sits on `/nodes/<name>` rather than on `/nodes`, without
  having to know in advance which applies. It can only run where the shutdown had already
  failed, and it reports what happened as a critical event: the machine is down, but the
  host entry is still wrong and needs fixing.
- **A warning when several host entries share one API URL** — shown while editing and
  repeated by the self-test, since a restored or hand-edited config never passes the form.
  That configuration is the one case in which the node name still decides where a shutdown
  lands, so both the warning and the stricter check apply there.
- `/api/health` reports `hosts_node_ok`, and `/api/status` a `node_state` per host. The
  dashboard shows it as a chip next to the host name, because that is the one place the
  mismatch would otherwise stay invisible: where an entry has its API URL to itself the
  self-test rightly counts it as working, so nothing else on the page says anything.

### Changed
- The cluster self-test warning about nodes without a shutdown target now names both sides
  of the comparison — the cluster's node names and the configured entries — plus the node
  an entry most likely meant, instead of only counting them. Entries that are merely
  disabled are reported as disabled rather than as misnamed.
- The host test checks the node name against the API, offers the right one for a click and
  fills an empty field — including the case where the name belongs to another member of the
  same cluster, where the node behind this API URL is the offer. The verdict is also shown
  as the same chip the dashboard uses, instead of only as a clause in the result sentence.
  An invalid name (a domain suffix, for instance) is reported without a request at all:
  Proxmox rejects it in its own parameter check.

## [3.5.0] - 2026-08-23

### Added
- **Proxmox Backup Server as a shutdown target.** Each host now has a type (Proxmox VE or
  Proxmox Backup Server) that decides how it is talked to. PBS uses its own
  `PBSAPIToken=<id>:<secret>` header scheme and needs the `Sys.PowerManagement` privilege
  on `/system/status`, which is why entering one as a PVE host used to fail as an invalid
  token. The node name is a free label for PBS entries: PBS ignores the node in the API
  path, so the shutdown always addresses `/nodes/localhost/status`.
- **Self-test results per host.** The scheduled credential check now records its outcome
  for every target (`credentials_ok`, `power_mgmt_ok`, `last_test_at`, `last_test_error`).
  They appear in `/api/status` and in the dashboard's host table, so an expired token is
  visible long before an outage needs it.
- `/api/health` reports the shutdown targets: `hosts_total`, `hosts_ok`,
  `hosts_selftest_ok` and `hosts_selftest_at`. The `status` field and the HTTP code are
  deliberately unchanged — they still track the engine, not the credentials.
- **Live preview of the shutdown sequence** under the host list in the wizard. It shows the
  order the engine would actually use, including which hosts share a stage and therefore go
  at the same time — previously that was only readable in a tooltip, and the staged
  behaviour was not visible at all. The *Order* field now also says `0 = first` in its
  label, and it is hidden while *This host* is ticked: that flag is the first sort key, so
  a marked host is last whatever number it carries. The value is kept, so unticking
  restores it.

### Changed
- **Hosts are shut down in stages instead of strictly one after another.** Targets sharing
  a shutdown `order` are now commanded at the same time, and the appliance's own host still
  forms the final stage. Together with a hard per-target deadline this means one machine
  that stops responding can no longer delay the other hosts, the poll loop or the battery
  countdown.
- The feed diagram shows a host's name only; the product name is no longer prefixed, which
  kept overflowing the node. The type stays visible as a chip in the dashboard's host table
  and in the host card's heading, shortened there to `Proxmox BS` so a Backup Server row is
  no wider than a Proxmox VE one.
- *This host* is hidden for Proxmox Backup Server entries in LXC deployments, where the
  combination is impossible — an LXC never runs on a Backup Server. It stays available in
  Docker deployments, where the container may genuinely sit on the PBS and the mark decides
  whether it shuts itself down last.

## [3.4.0] - 2026-08-19

### Added
- **Selectable webhook payload format.** The webhook is no longer tied to one JSON shape.
  Besides `JSON (full status)` (unchanged default) it can now post a **Microsoft Teams**
  adaptive card — the message envelope an incoming webhook / "post to a channel when a
  webhook request is received" workflow expects — or a short, human-readable **plain text**
  status (`text/plain`). Each format is one entry in a table in `app/notify.py`, so further
  target systems are a table row rather than new code.
- **Severity filter for notifications.** A new *Send from level* setting decides which
  events reach the webhook at all: all events, warnings and critical (new default), or
  critical only. Everything below the threshold still goes to the event log.
- **"Send test notification" button** in the notification settings: posts a sample message
  with the values currently entered — without saving first, and regardless of the severity
  filter — and reports the HTTP result (`POST /api/test/webhook`).

### Changed
- **Info events are no longer sent by default.** Existing installations move to the new
  *warnings and critical* default and therefore receive fewer messages; set the filter back
  to *All events (including info)* for the previous behaviour.
- `Host …: shutdown sent` and `Host …: shutdown aborted` are logged as **warnings** instead
  of info — an executed or withdrawn shutdown is not routine, and both now pass the default
  notification filter. They appear amber in the event log from now on.
- Webhook posts check the HTTP status: a `404`/`401` from the target is written to the
  process log instead of passing silently. Notification failures still never affect the
  shutdown logic.
- Documentation touch-ups: the Proxmox user and API token in a cluster, NUT on a
  QNAP/Synology NAS, and community-scripts.org as an additional listing.

## [3.3.0] - 2026-08-06

### Added
- **Vendor MIB support, starting with APC PowerNet.** An SNMP UPS is no longer read only
  through RFC 1628. Every SNMP UPS has a new **MIB** setting — `Automatic` (default),
  `RFC 1628 (standard)` or `APC PowerNet` — and the poller carries a table-driven profile
  per MIB, so further vendors are a table rather than new code.
- `Automatic` needs no configuration and is what existing UPS entries get on update: the
  regular poll carries the vendor anchor object alongside the standard ones, and the UPS
  is read on the vendor MIB as soon as it answers there. Under SNMPv1, where a single
  missing object aborts the whole request, the poller retries on the vendor MIB instead.
- This makes APC cards work that could not be used before: Schneider only supports
  RFC 1628 on Network Management Card 2 (AP9630/AP9631/AP9635) from firmware sumx/sy
  v5.1.7 on, while the older NMC1 cards (AP9617/AP9618/AP9619) speak PowerNet only.
- Cards that support both are now more accurate as well. `upsAdvBatteryRunTimeRemaining`
  is reported in hundredths of a second instead of whole minutes, and PowerNet reports a
  self-test (`onBatteryTest`) as its own state — RFC 1628 cannot tell one apart from a
  real outage.
- The UPS test walks both MIBs in `Automatic` mode and says which one it settled on and
  why, so "nothing works" becomes "this card has no RFC 1628, but PowerNet answers every
  object". The dashboard names the MIB a UPS is being read on.

## [3.2.0] - 2026-07-26

### Added
- **UPS sources**: a UPS is no longer necessarily an SNMP device. Every UPS entry now has
  a "Read via" selector, and the second source is a **NUT server** (Network UPS Tools,
  TCP 3493) — the answer for UPS devices with no network card, i.e. everything attached
  by USB or a serial cable. Any existing `upsd` works: the UPS server built into a
  Synology/QNAP/TrueNAS NAS, a Raspberry Pi, an OPNsense box, or a NUT install on a
  Proxmox host. Configuration is host, port, the UPS name from upsd's `ups.conf`, and
  optionally a user name and password.
- PVE-UPS acts strictly as a *read-only* NUT client: it only ever sends `LIST VAR`, never
  a command, and never runs `upsmon`. NUT is used as a device driver — the shutdown
  decision, the thresholds and the host policy stay in this appliance, so there are still
  no config files to write and no shutdown scripting on any host.
- The UPS test now names the trigger conditions the device cannot feed at all ("This UPS
  does not report: runtime remaining, battery charge"), because a threshold that can never
  fire is more dangerous than a visible error. Applies to both source types — plenty of
  NUT drivers omit `battery.runtime`, and plenty of SNMP cards omit
  `upsEstimatedMinutesRemaining`.
- `type` field in each UPS entry of the `/api/status` snapshot, and the dashboard card
  says which source a UPS is read through.
- `tests/nutsim.py`, a fake `upsd` for development and tests — the NUT counterpart of
  snmpsim + `snmpdata/`. Runnable standalone (`python -m tests.nutsim --scenario battery`)
  to click through the wizard without any UPS hardware.

### Changed
- `POST /api/test/ups` replaces `POST /api/test/snmp` as the wizard's test endpoint and
  handles every source type. The old path stays as an alias and behaves identically.
- Event-log wording and trigger reasons no longer say "SNMP" where they apply to any
  source ("No response for 3 polls …" instead of "No SNMP response for 3 polls …").
  The fail-safe behaviour itself is unchanged.
- The per-object test diagnosis was generalised from "per SNMP object (OID)" to "per
  object" and gained two outcomes: `missing` (the driver does not publish this variable)
  and `stale` (upsd answers, but its driver has lost contact with the UPS).
- Configuration schema: UPS entries carry an explicit `type` (`snmp` or `nut`). Existing
  configuration files and backups are migrated on load — an entry without `type` is read
  as SNMP, exactly as before — and the field is written out on the next save.

### Security
- A NUT server that reports stale data (`ERR DATA-STALE`, `ERR DRIVER-NOT-CONNECTED`) is
  treated as unreachable rather than as a valid reading. `upsd` keeps serving the last
  known values when its driver dies, and accepting those would have meant reporting
  "on mains" through an outage. The same applies when a driver omits `ups.status`
  entirely: the UPS counts as unreachable (alarm, never a shutdown) instead of silently
  looking healthy.

## [3.1.0] - 2026-07-26

### Added
- SNMP test now reports every RFC 1628 object individually, in an expandable "Details per
  SNMP object (OID)" block below the result line: value or
  `noSuchObject`/`noSuchInstance`/error per OID, plus a short overall diagnosis. It unfolds
  on its own when something is wrong. Each object is queried with its own GET, so one
  unimplemented OID no longer hides the state of all the others — notably the SNMPv1 case
  where a single missing object aborts the whole multi-object GET and the regular poll
  fails entirely.
- Configurable self-test interval (15 min, 30 min, 1 h, 2 h, 3 h, 6 h, 12 h, 24 h),
  anchored at the self-test start time, so the resulting times of day are fixed and
  predictable (start 09:00 every 6 h -> 09:00, 15:00, 21:00, 03:00). Existing
  configurations keep the previous daily cadence (24 h default).
- `next_selftest_at` in the `/api/status` appliance snapshot (naive local time).
- Optional Docker deployment alongside the LXC install: a `Dockerfile` and
  `docker-compose.example.yml`, with images published to
  `ghcr.io/ffind-dev/pve-ups` on each release tag. Configuration and event log
  persist via two volumes (`/etc/pve-usv`, `/var/lib/pve-usv`); the LXC path
  remains the default and unaffected.
- Deployment-mode flag `PVE_USV_DEPLOYMENT` (`lxc` default / `docker`). In Docker
  mode there is no privileged companion agent, so the NTP/timezone fields and the
  in-app update uploader are hidden in the wizard, and `POST /api/update/upload`
  returns HTTP 501 with guidance to pull a new image tag and recreate the container.
- Links to the GitHub project in the appliance's own documentation: a GitHub button in
  both manuals' top bar, a link row in the footer of the manuals and the web UI
  (repository, releases, issues, security policy), and the previously plain-text
  references to the releases page and the changelog are now real links.
- Docker section of both manuals now carries a self-contained Compose snippet, so the
  manuals stay the only documentation needed inside a release.

### Changed
- `selftest_hour` is now the *start time* of the self-test schedule rather than a plain
  daily hour, and the self-test itself changed in three ways that a short interval makes
  necessary: the slot that last ran is persisted in `engine-state.json` so a service
  restart no longer re-triggers the test, the hosts are queried concurrently instead of
  one after another (five unreachable hosts stalled the poll loop for 50 s), and a
  successful run is written to the event log at most once a day plus whenever it recovers
  from a failure. Failures are reported and notified as before. While a UPS is on battery
  the self-test is skipped entirely — the countdown has priority. The last result
  (`last_selftest_at`/`last_selftest_ok`) is persisted with the schedule latch, so
  `/api/status` keeps reporting it across a restart.
- `selftest_hour` and `selftest_interval_min` are normalised rather than rejected on load:
  an out-of-range hour is clamped to 0-23 and an unsupported interval falls back to daily,
  so a backup from another version always imports.
- `docker-compose.example.yml` sets `TZ` (the self-test schedule is interpreted in the
  container's local time, and the timezone cannot be set from the web UI in Docker mode)
  and adds `start_period: 15s` to the health check, matching the `Dockerfile`.
- Release workflow additionally tags the image with the bare version
  (`ghcr.io/ffind-dev/pve-ups:3.1.0`) next to the tag name (`:v3.1.0`) and `:latest`, so
  the image can be pinned with the usual registry convention.
- README (EN/DE) and both manuals warn about Docker's default address pool
  (`172.17.0.0/16` – `172.31.0.0/16`) shadowing networks in that range, which would cut
  the container off from a UPS or Proxmox host there.

### Fixed
- **SNMPv3 with encryption (authPriv) never worked.** Every poll failed with
  `Ciphering services not available`, while authNoPriv and v1/v2c were fine: pysnmp 7.x
  ships no ciphers of its own and delegates DES/3DES/AES to the `cryptography` package,
  which it declares only in its dev extra — so nothing installed it. It is now a declared
  dependency and comes along automatically, including on existing installations, because
  applying an update re-runs `pip install`. All privacy protocols were affected equally,
  so switching from DES to AES was no workaround; authNoPriv was. The dependency is capped
  below `cryptography` 50 because pysnmp's AES still uses an API that 49 deprecates and
  announces for removal; a test guards the exact symbols it relies on.
- The same failure is now reported as what it is instead of as "unreachable": it is
  detected before anything is sent, and the message names the missing package and the
  authNoPriv fallback — the previous wording sent users looking for firewall problems
  even though no packet ever left the appliance.

## [3.0.0] - 2026-07-15

First public release, under the public name **PVE-UPS** (technical identifiers — package,
paths, systemd units, env vars — deliberately stay `pve-usv` so in-place updates from 2.x
keep working).

### Added
- Bilingual web UI: English (default) and German. The language is picked automatically
  from the browser language and is easy to extend
  (`app/web/i18n/<lang>.js` + one `<script>` tag).
- English user manual (`manual.html`) alongside the German one (`handbuch.html`), same
  structure and anchors; the ?-icon and all deep links open the manual matching the UI
  language.
- i18n consistency tests (`tests/test_i18n.py`): key parity between the dictionaries,
  placeholder parity, and referenced-key checks for `index.html`/`app.js`.
- Installation and updates via GitHub releases: install one-liner downloads
  `install.sh` from the latest release; the release tarball doubles as the update package
  for the web UI uploader.

### Changed
- **Breaking:** all backend texts — event log entries, trigger reasons, webhook messages,
  API error details — are now uniformly English, regardless of the UI language.
- Webhook subject prefix is now `[PVE-UPS]`.
- Visible product name is PVE-UPS (UI, documentation, webhook); internal names stay
  `pve-usv`.

### Fixed
- The UPS card and the outage banner no longer show the time-based countdown once a UPS
  has triggered (battery low, runtime or charge threshold) — those conditions fire
  immediately and the ticking countdown wrongly suggested the shutdown would wait for
  it. The banner now also explains when triggered UPS are waiting for a host's
  AND/OR policy.

### Removed
- **Breaking:** e-mail (SMTP) notifications. The webhook remains and covers notification
  needs; a legacy `notifications.smtp` config key is ignored on load and dropped on the
  next save.

[Unreleased]: https://github.com/ffind-dev/pve-ups/compare/v4.0.0...HEAD
[4.0.0]: https://github.com/ffind-dev/pve-ups/compare/v3.5.0...v4.0.0
[3.5.0]: https://github.com/ffind-dev/pve-ups/compare/v3.4.0...v3.5.0
[3.4.0]: https://github.com/ffind-dev/pve-ups/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/ffind-dev/pve-ups/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/ffind-dev/pve-ups/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/ffind-dev/pve-ups/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/ffind-dev/pve-ups/releases/tag/v3.0.0
