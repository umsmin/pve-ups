"""Proxmox Backup Server API client.

Shuts down a PBS instance via ``POST /nodes/{node}/status {command: shutdown}`` using
an API token (``user@realm!tokenid``). The call looks like the Proxmox VE one, but
three things differ and each of them is enough to make PBS reject the request:

* the Authorization header is ``PBSAPIToken=<tokenid>:<secret>`` — a different scheme
  name than PVE's, and a colon where PVE puts its second ``=``;
* the privilege is spelled ``Sys.PowerManagement`` (PVE: ``Sys.PowerMgmt``) and lives
  on the ACL path ``/system/status``;
* PBS has no fine-grained power role — only the built-in ``Admin`` role carries that
  privilege — and a token's rights are intersected with its user's, so both need the
  ACL entry. See the manual, section "Creating the API token".

The ``{node}`` path segment is ignored by PBS (its router matches any value and the
handler does not take it), so ``PbsHostConfig.api_node`` pins it to ``localhost``,
which is what the PBS web UI itself uses.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import logging

import httpx

from .config import HostConfig
from .proxmox import CONNECT_TIMEOUT_S, TestResult

log = logging.getLogger("pve-usv.pbs")

# The privilege the shutdown endpoint demands, and the ACL paths that can carry it:
# the endpoint's own path first, then the ancestors an inherited grant would sit on.
POWER_PRIV = "Sys.PowerManagement"
PRIV_PATHS = ("/system/status", "/system", "/")


def _auth_header(host: HostConfig) -> dict[str, str]:
    secret = host.token_secret.get_secret_value()
    return {"Authorization": f"PBSAPIToken={host.token_id}:{secret}"}


def _client(host: HostConfig, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=host.api_url.rstrip("/") + "/api2/json",
        headers=_auth_header(host),
        verify=host.verify_tls,
        timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_S)),
    )


async def test_connection(host: HostConfig, timeout: float = 10.0) -> TestResult:
    """Validate URL, token and that the token may power off this instance."""
    try:
        async with _client(host, timeout) as client:
            # Version endpoint confirms reachability + token validity.
            resp = await client.get("/version")
            if resp.status_code == 401:
                return TestResult(False, "Authentication failed (token invalid?)")
            resp.raise_for_status()

            # Check effective permissions for Sys.PowerManagement on /system/status.
            perm = await client.get("/access/permissions")
            has_power = False
            if perm.status_code == 200:
                data = perm.json().get("data", {})
                for path in PRIV_PATHS:
                    if data.get(path, {}).get(POWER_PRIV):
                        has_power = True
                        break

            if not has_power:
                return TestResult(
                    True,
                    "Connection ok, but the 'Sys.PowerManagement' privilege could not be "
                    "confirmed. On PBS only the 'Admin' role carries it, and it has to be "
                    "granted on '/system/status' to the user AND to the token.",
                    has_power_mgmt=False,
                )
            return TestResult(
                True, "Connection and 'Sys.PowerManagement' privilege ok.", has_power_mgmt=True
            )

    except httpx.HTTPStatusError as exc:
        return TestResult(False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        return TestResult(False, f"Connection error: {exc}")


async def shutdown_node(host: HostConfig, timeout: float = 60.0) -> tuple[bool, str]:
    """Issue an orderly shutdown. Returns (ok, message)."""
    try:
        async with _client(host, timeout) as client:
            resp = await client.post(
                f"/nodes/{host.api_node}/status", data={"command": "shutdown"}
            )
            if resp.status_code in (200, 201):
                return True, "Shutdown command accepted"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        log.error("Shutdown of %s failed: %s", host.name, exc)
        return False, f"Error: {exc}"
