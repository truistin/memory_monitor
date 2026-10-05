"""Read operating-system counters without inspecting monitored process memory.

Snapshots are best effort: processes can exit between enumeration and reading, or
be invisible due to permissions. Such records are omitted and counted in errors;
they are never replaced with zero RSS. A completely unavailable source raises.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    identity: str
    rss_bytes: int
    state: str = ""


@dataclass(frozen=True)
class Snapshot:
    records: dict[int, ProcessInfo]
    errors: int = 0


class ProcessSampler:
    """Linux /proc reader, with a single ps invocation per macOS snapshot.

    Linux identities use kernel start ticks. macOS ps exposes creation time only
    to second resolution. RSS is resident memory, including shared pages; adding
    RSS across processes can count a shared page more than once.
    """

    def __init__(self, proc_root: Path | str = "/proc", *, platform: str | None = None):
        self.proc_root = Path(proc_root)
        self.platform = sys.platform if platform is None else platform
        if not (self.platform.startswith("linux") or self.platform == "darwin"):
            raise RuntimeError(f"Unsupported operating system: {self.platform}")
        self.page_size = int(os.sysconf("SC_PAGE_SIZE"))

    def read(self) -> Snapshot:
        return self._linux() if self.platform.startswith("linux") else self._macos()

    def _linux(self) -> Snapshot:
        records: dict[int, ProcessInfo] = {}
        errors = 0
        try:
            with os.scandir(self.proc_root) as entries:
                pids = [int(entry.name) for entry in entries if entry.name.isdigit()]
        except OSError as exc:
            raise RuntimeError(f"Cannot enumerate process source {self.proc_root}: {exc}") from exc
        for pid in pids:
            try:
                text = (self.proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
                # comm is parenthesized but may itself contain spaces or ')'.
                prefix, tail = text.rsplit(")", 1)
                if int(prefix.split("(", 1)[0]) != pid:
                    raise ValueError("PID does not match directory")
                fields = tail.split()
                ppid, start, rss = int(fields[1]), int(fields[19]), int(fields[21])
                if min(ppid, start, rss) < 0:
                    raise ValueError("Negative process counter")
                records[pid] = ProcessInfo(pid, ppid, f"linux:{start}", rss * self.page_size, fields[0])
            except (OSError, ValueError, IndexError):
                errors += 1
        if not records:
            raise RuntimeError(f"No readable processes in {self.proc_root} ({errors} failed records)")
        return Snapshot(records, errors)

    def _macos(self) -> Snapshot:
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,rss=,stat=,lstart="], capture_output=True,
                text=True, errors="replace", check=False, timeout=10,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Cannot read process source ps: {exc}") from exc
        if result.returncode:
            raise RuntimeError(f"ps failed ({result.returncode}): {result.stderr.strip()}")
        records: dict[int, ProcessInfo] = {}
        errors = 0
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                pid_text, ppid_text, rss_text, state, start = line.split(maxsplit=4)
                pid, ppid, rss = int(pid_text), int(ppid_text), int(rss_text)
                if pid <= 0 or min(ppid, rss) < 0 or len(start.split()) != 5:
                    raise ValueError("Invalid ps record")
                records[pid] = ProcessInfo(pid, ppid, f"ps:{start}", rss * 1024, state)
            except ValueError:
                errors += 1
        if not records:
            raise RuntimeError(f"ps returned no readable processes ({errors} failed records)")
        return Snapshot(records, errors)

    def pss(self, pids: list[int]) -> tuple[int | None, int]:
        """Return readable PSS sum and successful PID count; unsupported = None.

        Missing or denied smaps_rollup files reduce the count. A partial sum is
        not an estimate for all requested processes; callers must label coverage.
        Reading PSS costs more than RSS and should be explicitly enabled.
        """
        if not self.platform.startswith("linux"):
            return None, 0
        total, count = 0, 0
        for pid in dict.fromkeys(pids):
            if not isinstance(pid, int) or pid <= 0:
                continue
            try:
                lines = (self.proc_root / str(pid) / "smaps_rollup").read_text().splitlines()
                for line in lines:
                    if line.startswith("Pss:"):
                        _, value, unit = line.split()
                        amount = int(value)
                        if unit != "kB" or amount < 0:
                            raise ValueError("Invalid PSS counter")
                        total += amount * 1024
                        count += 1
                        break
            except (OSError, ValueError):
                continue
        return (total if count else None), count


class ProcessTree:
    """Track observed descendants across reparenting, guarded by start identity.

    Descendants born and reparented between snapshots cannot be discovered.
    A record missing from a snapshot is forgotten; permission/race omissions can
    therefore make a later orphan untraceable. A reused root PID is never taken.
    """

    def __init__(self, root_pid: int, root_identity: str):
        self.root_pid = root_pid
        self.root_identity = root_identity
        self._tracked: dict[int, str] = {}

    def select(self, snapshot: Snapshot) -> list[ProcessInfo]:
        records = snapshot.records
        root = records.get(self.root_pid)
        selected = {
            pid for pid, identity in self._tracked.items()
            if pid in records and records[pid].identity == identity
        }
        if root is not None and root.identity == self.root_identity:
            selected.add(self.root_pid)
        else:
            selected.discard(self.root_pid)
        children: dict[int, list[int]] = {}
        for process in records.values():
            children.setdefault(process.ppid, []).append(process.pid)
        pending = list(selected)
        while pending:
            for pid in children.get(pending.pop(), []):
                if pid not in selected and pid != self.root_pid:
                    selected.add(pid)
                    pending.append(pid)
        self._tracked = {pid: records[pid].identity for pid in selected}
        return [records[pid] for pid in sorted(selected)]


class CgroupReader:
    """Read counters from one explicit cgroup-v2 directory; never reset them."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_dir():
            raise ValueError(f"Cgroup directory does not exist: {self.path}")

    def read(self) -> dict[str, int]:
        if not self.path.is_dir():
            raise RuntimeError(f"Cgroup directory is no longer accessible: {self.path}")
        result: dict[str, int] = {}
        for filename, key in (("memory.current", "current_bytes"), ("memory.peak", "peak_bytes")):
            text = self._read_optional(filename)
            if text is not None:
                result[key] = self._counter(text.strip(), filename)
        events = self._read_optional("memory.events")
        if events is not None:
            for line in events.splitlines():
                try:
                    name, value = line.split()
                except ValueError as exc:
                    raise RuntimeError(f"Invalid cgroup memory.events in {self.path}") from exc
                count = self._counter(value, "memory.events")
                if name in {"oom", "oom_kill"}:
                    result[f"events_{name}"] = count
        return result

    def _read_optional(self, filename: str) -> str | None:
        try:
            return (self.path / filename).read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError(f"Cannot read cgroup {self.path / filename}: {exc}") from exc

    def _counter(self, text: str, filename: str) -> int:
        try:
            value = int(text)
            if value < 0:
                raise ValueError("Negative counter")
            return value
        except ValueError as exc:
            raise RuntimeError(f"Invalid cgroup {filename} in {self.path}: {text!r}") from exc
