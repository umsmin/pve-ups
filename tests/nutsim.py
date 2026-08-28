"""Minimal fake ``upsd`` — the NUT counterpart of snmpsim + snmpdata/.

Used by tests/test_nut.py, and runnable on its own to click through the wizard without
any UPS hardware::

    python -m tests.nutsim --port 3493 --scenario battery
    # then add a UPS of type "NUT", host 127.0.0.1, port 3493, UPS name "ups"

Speaks just enough of the protocol for a read-only client: USERNAME/PASSWORD, LIST UPS,
LIST VAR, LOGOUT. Scenarios mirror snmpdata/{public,battery}.snmprec.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

# Scenario name -> variables the fake server publishes.
SCENARIOS: dict[str, dict[str, str]] = {
    # On mains, fully charged.
    "mains": {
        "device.mfr": "ACME",
        "device.model": "Smart-UPS 1500",
        "ups.status": "OL",
        "battery.charge": "100",
        "battery.runtime": "2520",
        "ups.load": "37",
    },
    # Power outage, still plenty of runtime.
    "battery": {
        "device.mfr": "ACME",
        "device.model": "Smart-UPS 1500",
        "ups.status": "OB DISCHRG",
        "battery.charge": "82",
        "battery.runtime": "1080",
        "ups.load": "41",
    },
    # Outage, the UPS itself reports a low battery -> immediate trigger.
    "low": {
        "device.mfr": "ACME",
        "device.model": "Smart-UPS 1500",
        "ups.status": "OB LB DISCHRG",
        "battery.charge": "12",
        "battery.runtime": "120",
        "ups.load": "44",
    },
    # A driver that publishes neither runtime nor charge — the wizard must warn about it.
    "sparse": {
        "ups.status": "OL",
    },
}


class FakeUpsd:
    """A upsd that serves one UPS. Change ``variables``/``error`` at any time from a test."""

    def __init__(
        self,
        variables: Optional[dict[str, str]] = None,
        ups_name: str = "ups",
        error: Optional[str] = None,
        username: str = "",
        password: str = "",
    ):
        self.variables = dict(variables or SCENARIOS["mains"])
        self.ups_name = ups_name
        self.error = error  # e.g. "DATA-STALE": answered instead of the variable list
        self.username = username
        self.password = password
        self._server: Optional[asyncio.AbstractServer] = None
        self.port = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._serve, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "FakeUpsd":
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.stop()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authenticated = not self.username
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                parts = line.split()
                if not parts:
                    continue
                command = parts[0].upper()

                if command == "LOGOUT":
                    writer.write(b"OK Goodbye\n")
                    await writer.drain()
                    break
                if command == "USERNAME":
                    ok = len(parts) > 1 and parts[1] == self.username
                    writer.write(b"OK\n" if ok else b"ERR ACCESS-DENIED\n")
                elif command == "PASSWORD":
                    authenticated = len(parts) > 1 and parts[1] == self.password
                    writer.write(b"OK\n" if authenticated else b"ERR ACCESS-DENIED\n")
                elif command == "LIST" and len(parts) > 1 and parts[1].upper() == "UPS":
                    writer.write(
                        f'BEGIN LIST UPS\nUPS {self.ups_name} "fake"\nEND LIST UPS\n'.encode()
                    )
                elif command == "LIST" and len(parts) > 2 and parts[1].upper() == "VAR":
                    writer.write(self._var_response(parts[2], authenticated))
                else:
                    writer.write(b"ERR UNKNOWN-COMMAND\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def _var_response(self, name: str, authenticated: bool) -> bytes:
        if not authenticated:
            return b"ERR ACCESS-DENIED\n"
        if name != self.ups_name:
            return b"ERR UNKNOWN-UPS\n"
        if self.error:
            return f"ERR {self.error}\n".encode()
        lines = [f"BEGIN LIST VAR {name}"]
        lines += [f'VAR {name} {k} "{v}"' for k, v in self.variables.items()]
        lines.append(f"END LIST VAR {name}")
        return ("\n".join(lines) + "\n").encode()


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Fake NUT server for development")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3493)
    ap.add_argument("--ups-name", default="ups")
    ap.add_argument("--scenario", default="mains", choices=sorted(SCENARIOS))
    args = ap.parse_args()

    server = FakeUpsd(SCENARIOS[args.scenario], ups_name=args.ups_name)
    port = await server.start(args.host, args.port)
    print(f"fake upsd on {args.host}:{port}, UPS '{args.ups_name}', scenario '{args.scenario}'")
    print("Ctrl-C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
