"""Launch or attach without importing the monitored project's code."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import time

from . import __version__
from .recording import LogStageReader, Recorder
from .sampler import CgroupReader, ProcessSampler, ProcessTree


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def _nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative finite number")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("run", "attach"):
        sub = commands.add_parser(mode)
        sub.add_argument("--output", required=True, type=Path, help="new output directory; never overwritten")
        sub.add_argument("--interval", type=_positive, default=1.0, help="RSS sampling seconds (default: 1)")
        sub.add_argument("--report-interval", type=_positive, default=30.0, help="console/summary seconds")
        sub.add_argument("--pss-interval", type=_nonnegative, default=0.0,
                         help="Linux PSS sampling seconds; 0 disables this more expensive reading")
        sub.add_argument("--cgroup", type=Path,
                         help="optional cgroup v2 directory; reports its whole scope, does not move tasks")
        sub.add_argument("--max-stages", type=_positive_int, default=256,
                         help="retained stage buckets plus one overflow bucket")
        sub.add_argument("--stage-regex", help="log start marker regex with named 'stage' or first capture")
        if mode == "run":
            sub.add_argument("--command-log", type=Path, help="new target stdout/stderr file (default: OUTPUT/command.log)")
            sub.add_argument("--cwd", type=Path, help="target working directory (default: current directory)")
            sub.add_argument("command", nargs=argparse.REMAINDER, help="-- command arg ...; no shell evaluation")
        else:
            sub.add_argument("--pid", type=_positive_int, required=True)
            sub.add_argument("--stage-log", type=Path, help="optional existing target log to follow")
    return parser


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _log(message: str, *, error: bool = False) -> None:
    # A disconnected console must not abandon a healthy target process.
    stream = sys.stderr if error else sys.stdout
    try:
        print(f"[{_timestamp()}] {message}", file=stream, flush=True)
    except (OSError, ValueError):
        # Python flushes stdout/stderr again at shutdown. Merely swallowing a
        # BrokenPipeError would otherwise replace the child's exit code by 120.
        try:
            descriptor = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(descriptor, stream.fileno())
            finally:
                os.close(descriptor)
        except Exception:
            replacement = open(os.devnull, "w")
            if error:
                sys.stderr = replacement
            else:
                sys.stdout = replacement


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


class _Signals:
    """Only a command launched by this wrapper gets forwarded signals."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.received: int | None = None
        self.deadline: float | None = None
        self.previous: dict[int, object] = {}

    def __enter__(self):
        for number in (signal.SIGINT, signal.SIGTERM):
            self.previous[number] = signal.signal(number, self._receive)
        return self

    def __exit__(self, *args):
        for number, handler in self.previous.items():
            signal.signal(number, handler)

    def _send(self, number: int) -> None:
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, number)
        except ProcessLookupError:
            pass

    def _receive(self, number, _frame) -> None:
        repeated = self.received is not None
        self.received = int(number)
        self.deadline = time.monotonic() + (0.0 if repeated else 5.0)
        self._send(signal.SIGKILL if repeated else number)

    def enforce_deadline(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self._send(signal.SIGKILL)
            self.deadline = None


def _print_system_memory() -> None:
    """Append host memory counters without making reporting depend on free."""
    try:
        result = subprocess.run(
            ["free", "-g"], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, errors="replace", timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(f"[memory] warning: free -g unavailable: {error}", error=True)
        return
    if result.returncode != 0:
        detail = result.stderr.strip() or "no error output"
        _log(f"[memory] warning: free -g exited with {result.returncode}: {detail}", error=True)
        return
    if not result.stdout.strip():
        _log("[memory] warning: free -g returned no output", error=True)
        return
    _log("[memory] free -g (system memory):")
    for line in result.stdout.splitlines():
        _log(line)


def _print_summary(result: dict) -> None:
    peaks = result["peaks"]
    tree = peaks.get("tree_rss")
    peak = "unknown" if not tree else f"{tree['value_bytes'] / 1024**3:.3f} GiB"
    latest = result.get("latest") or {}
    current = latest.get("tree_rss_bytes")
    current_text = "unknown" if current is None else f"{current / 1024**3:.3f} GiB"
    _log(f"[memory] current_tree_rss={current_text} sampled_peak_tree_rss={peak} "
         f"processes={latest.get('process_count', 0)} samples={result['sample_count']} "
         f"stage={latest.get('stage', 'unattributed')}")
    _print_system_memory()


def _monitor(args, source, recorder, metadata, process, signals, stage_reader, cgroup) -> int:
    started = time.monotonic()
    tree = None
    root_pid = metadata["pid"]
    next_sample = started
    next_report = started
    next_pss = started
    failures = 0
    last_error = None
    storage_ok = True
    root_identity = metadata.get("root_identity")
    if root_identity is not None:
        tree = ProcessTree(root_pid, root_identity)

    def failure(error):
        nonlocal failures, last_error
        failures += 1
        last_error = f"{type(error).__name__}: {error}"
        if failures == 1:
            _log(f"[memory] warning: {last_error}; target keeps running", error=True)

    def save(status):
        metadata.update(status=status, elapsed_seconds=time.monotonic() - started,
                        monitor_error_count=failures, last_monitor_error=last_error,
                        received_signal=signals.received)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        metadata["monitor_cpu_seconds_excluding_helpers"] = usage.ru_utime + usage.ru_stime
        if not storage_ok:
            return
        try:
            recorder.write_summary(metadata)
        except Exception as error:
            failure(error)

    while True:
        signals.enforce_deadline()
        now = time.monotonic()
        returncode = process.poll() if process is not None else None
        stopping = returncode is not None or (process is None and signals.received is not None)
        if now >= next_sample or stopping:
            sample_started = time.monotonic()
            sample = {"timestamp": _timestamp(), "elapsed_seconds": now - started,
                      "root_rss_bytes": None, "tree_rss_bytes": None, "process_count": 0,
                      "monitor_rss_bytes": None, "stage": "unattributed",
                      "sample_complete": False, "error_count": 0}
            try:
                snapshot = source.read()
                root = snapshot.records.get(root_pid)
                if tree is None and root is not None:
                    root_identity = root.identity
                    metadata["root_identity"] = root_identity
                    tree = ProcessTree(root_pid, root_identity)
                members = [] if tree is None else [
                    item for item in tree.select(snapshot) if item.pid != os.getpid()
                ]
                same_root = root is not None and root.identity == root_identity
                sample.update(
                    root_rss_bytes=root.rss_bytes if same_root else None,
                    tree_rss_bytes=sum(item.rss_bytes for item in members) if members else None,
                    process_count=len(members), error_count=snapshot.errors,
                    sample_complete=bool(members) and snapshot.errors == 0,
                )
                own = snapshot.records.get(os.getpid())
                sample["monitor_rss_bytes"] = own.rss_bytes if own else None
                if args.pss_interval and now >= next_pss:
                    value, count = source.pss([item.pid for item in members])
                    sample.update(pss_bytes=value, pss_process_count=count,
                                  pss_complete=bool(members) and count == len(members))
                    if count < len(members):
                        sample["sample_complete"] = False
                        sample["error_count"] += len(members) - count
                    next_pss = now + args.pss_interval
                if process is None and (root is not None and not same_root
                                        or same_root and root.state.startswith("Z")
                                        or root is None and not _pid_exists(root_pid)):
                    stopping = True
                    metadata["attach_stop_reason"] = "root_exited_or_identity_changed"
            except Exception as error:
                sample["error_count"] += 1
                sample["sample_complete"] = False
                failure(error)
                if process is None and not _pid_exists(root_pid):
                    stopping = True
                    metadata["attach_stop_reason"] = "root_exited"
            if stage_reader is not None:
                sample["stage"] = stage_reader.read()
                sample["stage_log_error_count"] = stage_reader.read_error_count
            if cgroup is not None:
                try:
                    sample["cgroup"] = cgroup.read()
                except Exception as error:
                    sample["error_count"] += 1
                    sample["sample_complete"] = False
                    failure(error)
            sample["sample_seconds"] = time.monotonic() - sample_started
            if storage_ok:
                try:
                    recorder.add(sample)
                except Exception as error:
                    storage_ok = False
                    failure(error)
            next_sample = now + args.interval
        if now >= next_report:
            save("running")
            if storage_ok:
                _print_summary(recorder.summary(metadata))
            next_report = now + args.report_interval
        if stopping:
            break
        time.sleep(min(0.2, max(0.001, next_sample - time.monotonic())))

    if process is not None:
        returncode = process.wait()
        # The launched process group is ours. On user cancellation, do not leave
        # uncooperative descendants after their root has already exited.
        if signals.received is not None:
            signals._send(signal.SIGKILL)
        code = _exit_code(returncode)
        metadata.update(returncode=returncode, exit_code=code,
                        termination_signal=-returncode if returncode < 0 else None)
    else:
        code = 128 + signals.received if signals.received is not None else 0
        metadata.update(returncode=None, exit_code=code, termination_signal=None)
    save("finished" if process is not None else "monitoring_stopped")
    if storage_ok:
        _print_summary(recorder.summary(metadata))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if sys.platform not in ("linux", "darwin"):
        parser.error("supported platforms: Linux and macOS")
    if args.pss_interval and sys.platform != "linux":
        parser.error("PSS reading is supported only on Linux")
    if args.stage_regex:
        try:
            expression = re.compile(args.stage_regex)
            if not expression.groups:
                parser.error("--stage-regex requires a named 'stage' or first capture group")
        except re.error as error:
            parser.error(f"invalid --stage-regex: {error}")
    if args.mode == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run requires -- command [arguments]")
        if args.cwd is not None:
            args.cwd = args.cwd.expanduser().resolve()
    else:
        command = None
        if args.pid == os.getpid():
            parser.error("cannot attach to the monitor itself")
    recorder = command_log = None
    metadata = {}
    try:
        source = ProcessSampler()
        initial = source.read()  # Fail before launching if memory access is unavailable.
        if args.cgroup is not None:
            args.cgroup = args.cgroup.expanduser().resolve()
        cgroup = CgroupReader(args.cgroup) if args.cgroup else None
        cgroup_initial = cgroup.read() if cgroup else None
        if cgroup is not None and not cgroup_initial:
            raise ValueError("--cgroup directory contains no supported memory counters")
        root = initial.records.get(args.pid) if args.mode == "attach" else None
        if args.mode == "attach" and root is None:
            raise ValueError(f"PID {args.pid} does not exist or is not readable")
        if args.mode == "attach":
            ancestor = os.getpid()
            visited = set()
            while ancestor in initial.records and ancestor not in visited:
                if ancestor == args.pid:
                    raise ValueError("cannot attach to the monitor or its ancestors")
                visited.add(ancestor)
                ancestor = initial.records[ancestor].ppid
        output = args.output.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=False)
        recorder = Recorder(output, max_stages=args.max_stages)
        metadata = {
            "mode": args.mode, "command": command, "started_at": _timestamp(),
            "pid": args.pid if args.mode == "attach" else None,
            "root_identity": root.identity if root else None, "monitor_pid": os.getpid(),
            "interval_seconds": args.interval, "report_interval_seconds": args.report_interval,
            "pss_interval_seconds": args.pss_interval, "platform": sys.platform,
            "cwd": str(args.cwd.resolve() if args.mode == "run" and args.cwd else Path.cwd()),
            "memory_method": "sampled live process-tree RSS sum; shared pages may be counted repeatedly; short peaks may be missed",
            "cgroup_path": str(args.cgroup.resolve()) if args.cgroup else None,
            "cgroup_initial": cgroup_initial,
            "cgroup_scope": "whole specified cgroup; not necessarily exclusive to this task; kernel peak not reset",
        }
        with _Signals() as signals:
            process = None
            if args.mode == "run":
                log_path = args.command_log.expanduser().resolve() if args.command_log else output / "command.log"
                command_log = log_path.open("xb", buffering=0)
                metadata["command_log"] = str(log_path)
                try:
                    process = subprocess.Popen(command, cwd=args.cwd, stdout=command_log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as error:
                    code = 127 if isinstance(error, FileNotFoundError) else 126
                    metadata.update(status="launch_failed", returncode=None, exit_code=code,
                                    termination_signal=None, elapsed_seconds=0.0, error=str(error))
                    recorder.write_summary(metadata)
                    _log(f"[memory] cannot launch command: {error}", error=True)
                    return code
                signals.process = process
                if signals.received is not None:
                    signals._send(signals.received)
                metadata["pid"] = process.pid
            else:
                log_path = args.stage_log.expanduser().resolve() if args.stage_log else None
            reader = LogStageReader(log_path, pattern=args.stage_regex,
                                    start_from_tail=args.mode == "attach") if log_path else None
            _log(f"[memory] mode={args.mode} pid={metadata['pid']} output={output} interval={args.interval}s")
            observed_started = time.monotonic()
            try:
                return _monitor(args, source, recorder, metadata, process, signals, reader, cgroup)
            except Exception as error:
                # Observability failure must not terminate an otherwise healthy
                # research process or replace its exit status.
                _log(f"[memory] monitor failed: {error}; target is not interrupted", error=True)
                if process is not None:
                    while process.poll() is None:
                        signals.enforce_deadline()
                        time.sleep(0.2)
                    if signals.received is not None:
                        signals._send(signal.SIGKILL)
                    code = _exit_code(process.returncode)
                    returncode = process.returncode
                else:
                    code, returncode = 1, None
                metadata.update(
                    status="monitor_failed_target_finished" if process is not None else "monitor_failed",
                    elapsed_seconds=time.monotonic() - observed_started,
                    returncode=returncode, exit_code=code,
                    termination_signal=-returncode if returncode is not None and returncode < 0 else None,
                    monitor_error_count=metadata.get("monitor_error_count", 0) + 1,
                    last_monitor_error=f"{type(error).__name__}: {error}", received_signal=signals.received,
                )
                try:
                    recorder.write_summary(metadata)
                except Exception as save_error:
                    _log(f"[memory] cannot save final summary: {save_error}", error=True)
                return code
    except (OSError, RuntimeError, ValueError) as error:
        _log(f"[memory] error: {error}", error=True)
        return 2
    finally:
        for stream in (command_log, recorder):
            if stream is not None:
                try:
                    stream.close()
                except Exception as error:
                    _log(f"[memory] cannot close log: {error}", error=True)


if __name__ == "__main__":
    raise SystemExit(main())
