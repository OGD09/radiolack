"""Which local process owns a TCP socket, read from Linux /proc (read-only).

RadioLack must only drive the Gqrx that reads its I/Q bridge. Another Gqrx
(for example one started with an older profile) may hold the remote-control
port; connecting to it would retune and unmute that program instead. Before
connecting, the monitor compares the process listening on the remote-control
port with the process connected to the bridge.

When /proc cannot tell (another user's process, a system without /proc), the
helpers return "unknown" and callers keep their previous behaviour: missing
information never blocks reception.
"""

from __future__ import annotations

import os
from pathlib import Path

PROC = Path("/proc")
_TCP_TABLES = ("net/tcp", "net/tcp6")
_LISTEN = "0A"
_ESTABLISHED = "01"


def _socket_inodes(proc: Path, wanted) -> set[str]:
    """Inodes of /proc/net/tcp{,6} rows for which ``wanted(local_port, remote_port, state)`` holds."""
    found = set()
    for table in _TCP_TABLES:
        try:
            rows = (proc / table).read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeDecodeError):
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10:
                continue
            try:
                local_port = int(fields[1].rpartition(":")[2], 16)
                remote_port = int(fields[2].rpartition(":")[2], 16)
            except ValueError:
                continue
            # Inode 0: sockets without an owner (TIME_WAIT and the like).
            if fields[9] != "0" and wanted(local_port, remote_port, fields[3]):
                found.add(fields[9])
    return found


def _socket_owners(proc: Path, inodes: set[str]) -> set[int]:
    """PIDs with an open descriptor on one of the socket inodes (readable processes only)."""
    if not inodes:
        return set()
    targets = {f"socket:[{inode}]" for inode in inodes}
    owners = set()
    try:
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return owners
    for entry in entries:
        descriptors = entry / "fd"
        try:
            names = os.listdir(descriptors)
        except OSError:  # another user's process, or a process that just exited
            continue
        for name in names:
            try:
                if os.readlink(descriptors / name) in targets:
                    owners.add(int(entry.name))
                    break
            except OSError:
                continue
    return owners


def listening_pids(port: int, *, proc: Path = PROC) -> set[int]:
    """Processes listening on TCP ``port`` (IPv4 or IPv6); empty when unknown."""
    return _socket_owners(proc, _socket_inodes(
        proc, lambda local, remote, state: state == _LISTEN and local == port))


def connected_pid(local_port: int, remote_port: int, *, proc: Path = PROC) -> int | None:
    """Process owning the established socket ``local_port`` -> ``remote_port``, when unique and known."""
    owners = _socket_owners(proc, _socket_inodes(
        proc, lambda local, remote, state: state == _ESTABLISHED and local == local_port
        and remote == remote_port))
    return owners.pop() if len(owners) == 1 else None


def command_line(pid: int, *, proc: Path = PROC) -> str:
    """Command line of ``pid`` with spaces between arguments; "?" when unreadable."""
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return "?"
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or "?"


def describe(pid: int, *, proc: Path = PROC, width: int = 100) -> str:
    text = command_line(pid, proc=proc)
    if len(text) > width:
        text = text[:width - 3] + "..."
    return f"PID {pid} ({text})"


def foreign_remote_control(client_port: int, bridge_port: int, remote_port: int, *,
                           proc: Path = PROC) -> str | None:
    """Explain who holds the remote-control port when it is not the bridge's I/Q reader.

    ``client_port`` is the source port of the bridge client (Gqrx's I/Q
    connection). Returns None when the remote control belongs to that reader,
    or when either process cannot be identified.
    """
    reader = connected_pid(client_port, bridge_port, proc=proc)
    holders = listening_pids(remote_port, proc=proc)
    if reader is None or not holders or holders == {reader}:
        return None
    others = ", ".join(describe(pid, proc=proc) for pid in sorted(holders - {reader}))
    return (f"the Gqrx remote-control port {remote_port} belongs to {others}, not to the Gqrx "
            f"reading the I/Q bridge (PID {reader})")
