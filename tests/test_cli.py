"""Real-process checks for the standalone memory-monitor command.

These tests deliberately use tiny children, not a FAW installation or its data.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "run_monitor.py"


def _can_inspect_processes() -> tuple[bool, str]:
    if sys.platform.startswith("linux"):
        try:
            Path(f"/proc/{os.getpid()}/stat").read_text()
            Path(f"/proc/{os.getpid()}/status").read_text()
        except OSError as exc:
            return False, f"process inspection unavailable: {exc}"
        return True, ""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-p", str(os.getpid()), "-o", "pid="],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except OSError as exc:
            return False, f"process inspection unavailable: {exc}"
        if result.returncode:
            return False, f"process inspection unavailable: {result.stderr.strip()}"
        return True, ""
    return False, f"no tested process-inspection backend on {sys.platform}"


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            # The command name in stat may contain spaces or parentheses.
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return False
        return state != "Z"
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip()) and "Z" not in result.stdout
    return True


class CliValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-monitor-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_help_documents_both_modes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "--help"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run", result.stdout)
        self.assertIn("attach", result.stdout)

    def test_real_broken_pipe_does_not_change_interpreter_exit_status(self) -> None:
        code = (
            f"import os, sys; sys.path.insert(0, {str(PROJECT / 'src')!r}); "
            "from memory_monitor.cli import _log; "
            "reader, writer = os.pipe(); os.close(reader); "
            "sys.stdout = os.fdopen(writer, 'w'); "
            "_log('closed output'); raise SystemExit(7)"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)

    def test_existing_output_is_rejected_before_launch(self) -> None:
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("keep this file\n")
        launched = self.root / "launched.txt"
        child = f"from pathlib import Path; Path({str(launched)!r}).write_text('started')"
        result = subprocess.run(
            [sys.executable, str(CLI), "run", "--output", str(output), "--",
             sys.executable, "-c", child],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(launched.exists())
        self.assertEqual(sentinel.read_text(), "keep this file\n")
        self.assertEqual(set(output.iterdir()), {sentinel})

    def test_invalid_intervals_do_not_launch_target(self) -> None:
        for option, value in [
            ("--interval", "0"), ("--interval", "-1"),
            ("--interval", "nan"), ("--interval", "inf"),
            ("--report-interval", "0"), ("--report-interval", "nan"),
            ("--pss-interval", "-1"), ("--pss-interval", "nan"),
        ]:
            with self.subTest(option=option, value=value):
                output = self.root / f"invalid-{option[2:]}-{value}"
                launched = self.root / "launched.txt"
                child = f"from pathlib import Path; Path({str(launched)!r}).touch()"
                result = subprocess.run(
                    [sys.executable, str(CLI), "run", "--output", str(output),
                     option, value, "--", sys.executable, "-c", child],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(launched.exists())


class CliProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        available, reason = _can_inspect_processes()
        if not available:
            raise unittest.SkipTest(reason)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-monitor-process-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.processes: list[subprocess.Popen] = []
        self.owned_groups: list[int] = []
        self.addCleanup(self._stop_processes)

    def _stop_processes(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError):
                pass
        for pgid in self.owned_groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _args(self, mode: str, output: Path) -> list[str]:
        return [
            sys.executable, str(CLI), mode, "--output", str(output),
            "--interval", "0.05", "--report-interval", "0.1", "--pss-interval", "0",
        ]

    def _run(self, code: str, *, name: str = "run") -> tuple[subprocess.CompletedProcess, dict, Path]:
        output = self.root / name
        result = subprocess.run(
            [*self._args("run", output), "--", sys.executable, "-u", "-c", code],
            capture_output=True, text=True, timeout=20,
        )
        self.assertTrue((output / "summary.json").exists(), result.stderr + result.stdout)
        summary = json.loads((output / "summary.json").read_text())
        return result, summary, output

    def _run_injected(self, patch: str, *, name: str, default_pss: bool = False):
        """Inject monitor failures in a separate interpreter, never in the child."""
        output = self.root / name
        wrapper = (
            "import sys\n"
            f"sys.path.insert(0,{str(PROJECT / 'src')!r})\n"
            "from memory_monitor import cli\n"
            + patch + "\nraise SystemExit(cli.main(sys.argv[1:]))\n"
        )
        options = self._args("run", output)[2:]
        if default_pss:
            options = options[:-2]  # Exercise parser default, not an explicit 0.
        result = subprocess.run(
            [sys.executable, "-c", wrapper, *options, "--", sys.executable, "-u", "-c",
             "import sys,time; time.sleep(.25); print('TARGET-FINISHED'); sys.exit(7)"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("TARGET-FINISHED", (output / "command.log").read_text())
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["metadata"]["returncode"], 7)
        self.assertEqual(summary["metadata"]["exit_code"], 7)
        return result, summary, output

    def _wait_for(self, predicate, *, seconds: float = 10) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("timed out waiting for child/monitor state")

    def test_exit_code_and_separate_command_log(self) -> None:
        result, summary, output = self._run(
            "import sys,time; print('CHILD-STDOUT'); print('CHILD-STDERR',file=sys.stderr); "
            "time.sleep(.2); sys.exit(7)"
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(summary["metadata"]["returncode"], 7)
        self.assertEqual(summary["metadata"]["exit_code"], 7)
        command_log = (output / "command.log").read_text()
        self.assertIn("CHILD-STDOUT", command_log)
        self.assertIn("CHILD-STDERR", command_log)
        self.assertNotIn("CHILD-STDOUT", result.stdout)
        self.assertEqual(summary["metadata"]["mode"], "run")
        self.assertEqual(summary["metadata"]["interval_seconds"], 0.05)

    def test_unknown_command_returns_127_and_finalizes_summary(self) -> None:
        output = self.root / "missing-command"
        result = subprocess.run(
            [*self._args("run", output), "--", str(self.root / "does-not-exist")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 127, result.stderr)
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["metadata"]["exit_code"], 127)

    def test_short_lived_success_writes_valid_summary(self) -> None:
        result, summary, output = self._run("pass", name="short")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary["metadata"]["exit_code"], 0)
        self.assertTrue(math.isfinite(summary["metadata"]["elapsed_seconds"]))
        self.assertGreaterEqual(summary["metadata"]["elapsed_seconds"], 0)
        self.assertTrue((output / "samples.jsonl").exists())
        for line in (output / "samples.jsonl").read_text().splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_custom_cwd_log_and_literal_command_argument(self) -> None:
        output = self.root / "literal-command"
        workdir = self.root / "directory with spaces"
        workdir.mkdir()
        log = self.root / "external command.log"
        marker = self.root / "shell-must-not-create"
        literal = f"a; touch {marker}; $(echo substituted)"
        result = subprocess.run(
            [*self._args("run", output), "--cwd", str(workdir), "--command-log", str(log),
             "--", sys.executable, "-u", "-c",
             "import json,os,sys; print(json.dumps([os.getcwd(),sys.argv[1]]))", literal],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(log.read_text()), [str(workdir.resolve()), literal])
        self.assertFalse(marker.exists())

    def test_process_tree_allocation_is_observed(self) -> None:
        code = (
            "import subprocess,sys,time\n"
            "memory=bytearray(8*1024*1024)\n"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import time; memory=bytearray(12*1024*1024); time.sleep(.8)'])\n"
            "time.sleep(.65)\n"
            "child.wait()\n"
        )
        result, summary, output = self._run(code, name="allocation")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(summary["sample_count"], 3)
        peak = summary["peaks"]["tree_rss"]
        self.assertGreater(peak["value_bytes"], 20 * 1024 * 1024)
        self.assertGreaterEqual(summary["peaks"]["process_count"]["value"], 2)
        self.assertGreaterEqual(peak["elapsed_seconds"], 0)
        self.assertTrue(peak["timestamp"])
        self.assertGreaterEqual(len((output / "samples.jsonl").read_text().splitlines()), 3)

    def test_sampler_failure_preserves_target_execution_and_exit_status(self) -> None:
        output = self.root / "sampler-failure"
        injected_wrapper = (
            "import sys\n"
            f"sys.path.insert(0,{str(PROJECT / 'src')!r})\n"
            "from memory_monitor import cli\n"
            "original=cli.ProcessSampler.read\n"
            "calls=0\n"
            "def failing_read(self):\n"
            "    global calls\n"
            "    calls+=1\n"
            "    if calls>1: raise RuntimeError('injected sampler failure')\n"
            "    return original(self)\n"
            "cli.ProcessSampler.read=failing_read\n"
            "raise SystemExit(cli.main(sys.argv[1:]))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", injected_wrapper, *self._args("run", output)[2:],
             "--", sys.executable, "-u", "-c",
             "import sys,time; time.sleep(.2); print('TARGET-FINISHED'); sys.exit(7)"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("TARGET-FINISHED", (output / "command.log").read_text())
        self.assertIn("injected sampler failure", result.stderr)
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["metadata"]["exit_code"], 7)
        self.assertGreater(summary["metadata"]["monitor_error_count"], 0)
        samples = [json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()]
        self.assertTrue(samples)
        self.assertTrue(all(not sample["sample_complete"] for sample in samples))
        self.assertTrue(all(sample["error_count"] > 0 for sample in samples))
        self.assertEqual(summary["incomplete_sample_count"], summary["sample_count"])
        self.assertIsNone(summary["peaks"]["tree_rss"])

    def test_broken_console_pipe_preserves_target_and_summary(self) -> None:
        result, summary, _ = self._run_injected(
            "import builtins\n"
            "real_print=builtins.print\n"
            "def broken_stdout(*args,**kwargs):\n"
            "    if kwargs.get('file',sys.stdout) is sys.stdout:\n"
            "        raise BrokenPipeError('injected closed console')\n"
            "    return real_print(*args,**kwargs)\n"
            "builtins.print=broken_stdout\n",
            name="broken-console",
        )
        self.assertEqual(result.stdout, "")
        self.assertEqual(summary["metadata"]["status"], "finished")
        self.assertGreater(summary["sample_count"], 0)

    def test_recorder_close_error_preserves_target_exit_status(self) -> None:
        result, summary, _ = self._run_injected(
            "real_close=cli.Recorder.close\n"
            "def broken_close(self):\n"
            "    real_close(self)\n"
            "    raise OSError('injected recorder close error')\n"
            "cli.Recorder.close=broken_close\n",
            name="broken-close",
        )
        self.assertIn("injected recorder close error", result.stderr)
        self.assertEqual(summary["metadata"]["status"], "finished")

    def test_unexpected_stage_reader_error_waits_and_finalizes_summary(self) -> None:
        result, summary, _ = self._run_injected(
            "def broken_stage_read(self):\n"
            "    raise RuntimeError('injected unexpected stage-reader error')\n"
            "cli.LogStageReader.read=broken_stage_read\n",
            name="broken-stage-reader",
        )
        self.assertIn("injected unexpected stage-reader error", result.stderr)
        metadata = summary["metadata"]
        self.assertEqual(metadata["status"], "monitor_failed_target_finished")
        self.assertGreater(metadata["monitor_error_count"], 0)
        self.assertIn("injected unexpected stage-reader error", metadata["last_monitor_error"])
        self.assertIsNone(metadata["termination_signal"])
        self.assertGreaterEqual(metadata["elapsed_seconds"], 0)

    def test_pss_is_disabled_by_default(self) -> None:
        result, summary, output = self._run_injected(
            "def unexpected_pss(self,pids):\n"
            "    raise AssertionError('PSS must not be read by default')\n"
            "cli.ProcessSampler.pss=unexpected_pss\n",
            name="default-pss", default_pss=True,
        )
        self.assertNotIn("PSS must not be read", result.stderr)
        self.assertEqual(summary["metadata"]["pss_interval_seconds"], 0)
        self.assertEqual(summary["metadata"]["monitor_error_count"], 0)
        self.assertIsNone(summary["peaks"]["pss"])
        for line in (output / "samples.jsonl").read_text().splitlines():
            self.assertNotIn("pss_bytes", json.loads(line))

    @unittest.skipUnless(os.name == "posix", "POSIX process-group signal semantics")
    def test_sigterm_forwards_to_run_process_group(self) -> None:
        output = self.root / "terminated"
        code = (
            "import json,os,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            "print(json.dumps([os.getpid(),child.pid]),flush=True)\n"
            "time.sleep(60)\n"
        )
        monitor = subprocess.Popen(
            [*self._args("run", output), "--", sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(monitor)
        log = output / "command.log"
        self._wait_for(lambda: log.exists() and "[" in log.read_text())
        pids = json.loads(log.read_text().splitlines()[0])
        self.owned_groups.append(pids[0])
        monitor.send_signal(signal.SIGTERM)
        stdout, stderr = monitor.communicate(timeout=15)
        self.assertEqual(monitor.returncode, 128 + signal.SIGTERM, stdout + stderr)
        self._wait_for(lambda: all(not _is_running(pid) for pid in pids), seconds=5)
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["metadata"]["exit_code"], 128 + signal.SIGTERM)
        self.assertEqual(summary["metadata"]["termination_signal"], signal.SIGTERM)

    @unittest.skipUnless(os.name == "posix", "POSIX signal semantics")
    def test_attach_detach_does_not_terminate_target(self) -> None:
        target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.processes.append(target)
        output = self.root / "attached"
        monitor = subprocess.Popen(
            [*self._args("attach", output), "--pid", str(target.pid)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(monitor)
        samples = output / "samples.jsonl"
        self._wait_for(lambda: samples.exists() and len(samples.read_text().splitlines()) >= 2)
        monitor.send_signal(signal.SIGTERM)
        monitor.communicate(timeout=10)
        self.assertIsNone(target.poll(), "detaching the monitor killed the existing target")
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["metadata"]["mode"], "attach")
        self.assertEqual(summary["metadata"]["pid"], target.pid)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group signal semantics")
    def test_sigterm_reaps_ignoring_descendant_after_root_exits(self) -> None:
        output = self.root / "ignoring-descendant"
        ready = self.root / "descendant-ready"
        grandchild = (
            "import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"Path({str(ready)!r}).touch(); time.sleep(60)"
        )
        code = (
            "import json,os,subprocess,sys,time\n"
            f"child=subprocess.Popen([sys.executable,'-c',{grandchild!r}])\n"
            "print(json.dumps([os.getpid(),child.pid]),flush=True)\n"
            "time.sleep(60)\n"
        )
        monitor = subprocess.Popen(
            [*self._args("run", output), "--", sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(monitor)
        log = output / "command.log"
        self._wait_for(lambda: ready.exists() and log.exists() and "[" in log.read_text())
        pids = json.loads(log.read_text().splitlines()[0])
        self.owned_groups.append(pids[0])
        monitor.send_signal(signal.SIGTERM)
        stdout, stderr = monitor.communicate(timeout=15)
        self.assertEqual(monitor.returncode, 128 + signal.SIGTERM, stdout + stderr)
        self._wait_for(lambda: all(not _is_running(pid) for pid in pids), seconds=5)


if __name__ == "__main__":
    unittest.main()
