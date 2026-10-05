"""Console reporting checks without invoking platform process utilities."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import subprocess
import unittest
from unittest.mock import patch

from memory_monitor import cli


TABLE = (
    "               total        used        free      shared  buff/cache   available\n"
    "Mem:             251          92          17           0         141         156\n"
    "Swap:              0           0           0\n"
)


def summary() -> dict:
    return {
        "peaks": {"tree_rss": {"value_bytes": 1104 * 1024**3}},
        "latest": {
            "tree_rss_bytes": 588 * 1024**3,
            "process_count": 34,
            "stage": "[select] generating historical features",
        },
        "sample_count": 6931,
    }


class ConsoleReportTests(unittest.TestCase):
    def assert_timestamped(self, lines: list[str]) -> None:
        for line in lines:
            self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00\] ")
            datetime.fromisoformat(line[1:line.index("]")])

    def test_each_summary_fetches_fresh_table_after_rss_and_keeps_alignment(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        changed = TABLE.replace("          92", "          93")
        calls = []

        def run(command, **kwargs):
            # The corresponding process-tree reading must precede the host
            # snapshot, including when the same summary is printed again.
            calls.append((command, kwargs))
            self.assertEqual(stdout.getvalue().count("current_tree_rss="), len(calls))
            return subprocess.CompletedProcess(command, 0, TABLE if len(calls) == 1 else changed, "")

        with patch.object(cli.subprocess, "run", side_effect=run), redirect_stdout(stdout), redirect_stderr(stderr):
            cli._print_summary(summary())
            cli._print_summary(summary())

        self.assertEqual(len(calls), 2)
        for command, kwargs in calls:
            self.assertEqual(command, ["free", "-g"])
            self.assertTrue(kwargs.get("capture_output"))
            self.assertTrue(kwargs.get("text"))
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(kwargs.get("timeout"), 2)
        self.assertEqual(stderr.getvalue(), "")
        lines = stdout.getvalue().splitlines()
        self.assert_timestamped(lines)
        summaries = [index for index, line in enumerate(lines) if "current_tree_rss=" in line]
        self.assertEqual(len(summaries), 2)
        for index, table in enumerate((TABLE, changed)):
            end = summaries[index + 1] if index + 1 < len(summaries) else len(lines)
            block = lines[summaries[index] + 1:end]
            positions = []
            for row in table.splitlines():
                matches = [position for position, line in enumerate(block) if line.endswith(row)]
                self.assertEqual(len(matches), 1, (row, block))
                positions.append(matches[0])
            self.assertEqual(positions, sorted(positions))

    def test_unavailable_timed_out_and_failed_free_do_not_prevent_later_reports(self) -> None:
        failures = (
            FileNotFoundError("free is unavailable"),
            subprocess.TimeoutExpired(["free", "-g"], 2),
            subprocess.CompletedProcess(["free", "-g"], 1, "", "cannot read memory information"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, value=str(failure)):
                stdout = io.StringIO()
                stderr = io.StringIO()
                success = subprocess.CompletedProcess(["free", "-g"], 0, TABLE, "")
                with patch.object(cli.subprocess, "run", side_effect=[failure, success]) as run, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    cli._print_summary(summary())
                    cli._print_summary(summary())

                self.assertEqual(run.call_count, 2)
                self.assertEqual(stdout.getvalue().count("current_tree_rss="), 2)
                self.assertIn("[memory] warning", stderr.getvalue())
                self.assertIn("free -g", stderr.getvalue())
                self.assert_timestamped(stderr.getvalue().splitlines())
                self.assert_timestamped(stdout.getvalue().splitlines())
                for row in TABLE.splitlines():
                    self.assertTrue(any(line.endswith(row) for line in stdout.getvalue().splitlines()))


if __name__ == "__main__":
    unittest.main()
