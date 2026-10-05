from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from memory_monitor.recording import LogStageReader, Recorder


def sample(index: int = 0, **overrides):
    result = {
        "timestamp": f"2026-10-05T00:00:{index:02d}Z", "elapsed_seconds": float(index),
        "root_rss_bytes": 10 + index, "tree_rss_bytes": 20 + index,
        "process_count": 2, "monitor_rss_bytes": 5, "stage": "loading",
        "sample_seconds": 0.001, "sample_complete": True,
    }
    result.update(overrides)
    return result


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)

    def recorder(self, **kwargs):
        recorder = Recorder(self.path, **kwargs)
        self.addCleanup(recorder.close)
        return recorder

    def test_flush_peaks_and_complete_atomic_summary(self):
        recorder = self.recorder()
        recorder.add(sample(0))
        self.assertEqual(json.loads((self.path / "samples.jsonl").read_text())["root_rss_bytes"], 10)
        recorder.add(sample(1, stage="generation", pss_bytes=14, pss_process_count=1,
                            sample_complete=False, error_count=2))
        recorder.add(sample(2, tree_rss_bytes=19, root_rss_bytes=None,
                            pss_bytes=12, pss_process_count=2, process_count=3))
        recorder.write_summary({"command": ["python", "run.py"]})
        report = json.loads((self.path / "summary.json").read_text())
        self.assertEqual(report["sample_count"], 3)
        self.assertEqual(report["incomplete_sample_count"], 1)
        self.assertEqual(report["sampling_error_count"], 2)
        self.assertEqual(report["peaks"]["tree_rss"]["value_bytes"], 21)
        self.assertEqual(report["peaks"]["tree_rss"]["stage"], "generation")
        self.assertEqual(report["peaks"]["root_rss"]["value_bytes"], 11)
        self.assertEqual(report["peaks"]["pss"]["pss_process_count"], 1)
        self.assertEqual(report["peaks"]["pss"]["process_count"], 2)
        self.assertEqual(report["peaks"]["process_count"]["value"], 3)
        self.assertAlmostEqual(report["sampling_seconds_total"], 0.003)
        self.assertEqual(report["stages"]["loading"]["sample_count"], 2)
        self.assertEqual(list(self.path.glob(".summary-*.tmp")), [])
        recorder.write_summary({"finished": True})
        self.assertTrue(json.loads((self.path / "summary.json").read_text())["metadata"]["finished"])

    def test_failed_replace_preserves_previous_summary_and_cleans_temp(self):
        recorder = self.recorder()
        recorder.write_summary({"value": "first"})
        before = (self.path / "summary.json").read_bytes()
        with patch("memory_monitor.recording.os.replace", side_effect=OSError("test")):
            with self.assertRaises(OSError):
                recorder.write_summary({"value": "second"})
        self.assertEqual((self.path / "summary.json").read_bytes(), before)
        self.assertEqual(list(self.path.glob(".summary-*.tmp")), [])

    def test_bounded_stage_summaries_and_latest_not_history(self):
        recorder = self.recorder(max_stages=3)
        for index in range(5000):
            recorder.add(sample(index, stage=f"stage-{index}"))
        report = recorder.summary({})
        self.assertEqual(report["sample_count"], 5000)
        self.assertEqual(set(report["stages"]), {"stage-0", "stage-1", "stage-2", "__other__"})
        self.assertEqual(report["overflow_stage_sample_count"], 4997)
        self.assertEqual(report["stages"]["__other__"]["sample_count"], 4997)
        self.assertEqual(report["latest"]["stage"], "stage-4999")
        self.assertLess(len(json.dumps(report)), 10000)

    def test_zero_stage_budget_and_none_peaks(self):
        recorder = self.recorder(max_stages=0)
        recorder.add(sample(root_rss_bytes=None, tree_rss_bytes=None, monitor_rss_bytes=None))
        report = recorder.summary({})
        self.assertEqual(set(report["stages"]), {"__other__"})
        self.assertIsNone(report["peaks"]["tree_rss"])

    def test_summary_and_caller_mutations_do_not_change_retained_state(self):
        recorder = self.recorder()
        value = sample(cgroup={"memory_peak_bytes": 100})
        recorder.add(value)
        value["cgroup"]["memory_peak_bytes"] = 200
        report = recorder.summary({})
        report["latest"]["cgroup"]["memory_peak_bytes"] = 300
        report["peaks"]["tree_rss"]["value_bytes"] = 999
        self.assertEqual(recorder.summary({})["latest"]["cgroup"]["memory_peak_bytes"], 100)
        self.assertEqual(recorder.summary({})["peaks"]["tree_rss"]["value_bytes"], 20)

    def test_samples_exclusive_and_stage_labels_bounded(self):
        recorder = self.recorder()
        recorder.add(sample(stage="x" * 10000))
        self.assertEqual(len(recorder.summary({})["latest"]["stage"]), 256)
        with self.assertRaises(FileExistsError):
            Recorder(self.path)
        with self.assertRaises(ValueError):
            Recorder(self.path, max_stages=-1)


class LogStageReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "run.log"

    def append(self, content: bytes):
        with self.path.open("ab") as stream:
            stream.write(content)

    def test_missing_partial_crlf_and_completion_lines(self):
        reader = LogStageReader(self.path)
        self.assertEqual(reader.read(), "unattributed")
        self.assertEqual(reader.read_error_count, 1)
        self.append(b"[0/7] loading and validating")
        self.assertEqual(reader.read(), "unattributed")
        self.append(b" data\r\n")
        self.assertEqual(reader.read(), "[0/7] loading and validating data")
        self.append(b"[0/7] complete in 12.3s\n[2-5/7] full-period diagnostics skipped; enable with --diagnostics\n")
        self.assertEqual(reader.read(), "[0/7] loading and validating data")

    def test_faw_select_start_markers_only(self):
        reader = LogStageReader(self.path)
        starts = (
            "[6/7] rolling walk-forward member selection",
            "[select] fold_0001 (1/17): preparing historical data",
            "[select] fold_0001 (1/17) generating historical features",
            "[select] fold_0001 pooled: training Rank IC (176 members)",
            "[select] fold_0001 pooled: value correlation",
            "[select] fold_0001 pooled: IC correlation and marginal selection",
            "[select] causal replay cutoff=2026-09-30: reading and comparing 176 members",
        )
        for marker in starts:
            self.append((marker + "\n").encode())
            self.assertEqual(reader.read(), marker)
        self.append(
            b"[select] fold_0001 pooled: value correlation complete in 10.0s\n"
            b"[select] fold_0001 pooled: IC correlation complete in 1.0s\n"
            b"[select] fold_0001 (1/17): historical data ready in 3.0s\n"
            b"[select] fold_0001 (1/17): total complete in 5.0s\n"
            b"[timing] feature result-writeback elapsed=42.0s\n"
        )
        self.assertEqual(reader.read(), starts[-1])

    def test_custom_named_or_first_group(self):
        reader = LogStageReader(self.path, r"^BEGIN (?P<stage>.+)$")
        self.append(b"BEGIN phase one\nEND phase two\n")
        self.assertEqual(reader.read(), "phase one")
        self.assertEqual(LogStageReader(self.path, r"^BEGIN (.+)$").read(), "phase one")
        with self.assertRaises(ValueError):
            LogStageReader(self.path, r"BEGIN .+")
        with self.assertRaises(ValueError):
            LogStageReader(self.path, max_line_bytes=0)

    def test_timestamped_faw_markers_keep_stable_stage_names(self):
        reader = LogStageReader(self.path)
        markers = (
            "[0/7] loading and validating data",
            "[select] preparing folds and labels",
            "[select] fold_0001 (1/9) generating historical features",
            "[select] fold_0001 (1/9): extracting Train/Selection feature windows from memory",
            "[select] fold_0001 pooled: value correlation",
            "[select] causal replay cutoff=2026-09-30: reading and comparing 176 members",
        )
        for prefix in ("", "[2026-10-05T15:04:05+08:00] ", "[2026-10-05T07:04:05Z] "):
            for marker in markers:
                self.append((prefix + marker + "\n").encode())
                self.assertEqual(reader.read(), marker)
        self.append(
            b"[2026-10-05T15:05:05+08:00] [timing] select compute done wall=60.0s\n"
            b"[2026-10-05T15:05:05+08:00] [select] fold_0001 pooled: value correlation complete in 10.0s\n"
        )
        self.assertEqual(reader.read(), markers[-1])

    def test_custom_pattern_still_sees_original_timestamp(self):
        reader = LogStageReader(self.path, r"^\[(?P<stage>2026-[^]]+)\] BEGIN phase$")
        self.append(b"[2026-10-05T15:04:05+08:00] BEGIN phase\n")
        self.assertEqual(reader.read(), "2026-10-05T15:04:05+08:00")

    def test_rotation_and_truncation_reset_partial_lines(self):
        reader = LogStageReader(self.path, r"^BEGIN (.+)$")
        self.append(b"BEGIN phase-one\nBEGIN unfinished")
        self.assertEqual(reader.read(), "phase-one")
        self.path.rename(self.path.with_suffix(".old"))
        self.path.write_bytes(b"other\nBEGIN rotated\n")
        self.assertEqual(reader.read(), "rotated")
        self.path.write_bytes(b"BEGIN short\n")
        self.assertEqual(reader.read(), "short")

    def test_truncate_and_regrow_detected_by_anchor(self):
        reader = LogStageReader(self.path, r"^BEGIN (.+)$")
        self.path.write_bytes(b"BEGIN first\n")
        self.assertEqual(reader.read(), "first")
        self.path.write_bytes(b"unrecognized longer message\nBEGIN replacement\n")
        self.assertEqual(reader.read(), "replacement")

    def test_initial_tail_skips_backlog_and_handles_initial_missing_file(self):
        reader = LogStageReader(self.path, r"^BEGIN (.+)$", start_from_tail=True)
        self.assertEqual(reader.read(), "unattributed")
        with self.path.open("wb") as stream:
            stream.write(b"BEGIN old-stage\n")
            for _ in range(8):
                stream.write(b"x" * reader.max_read_bytes)
            stream.write(b"\nBEGIN current-stage\n")
        self.assertEqual(reader.read(), "current-stage")
        self.assertEqual(reader._offset, self.path.stat().st_size)
        self.assertEqual(len(reader._pending), 0)
        self.append(b"BEGIN next-stage\n")
        self.assertEqual(reader.read(), "next-stage")

    def test_initial_tail_does_not_parse_partial_marker_and_rotations_start_at_zero(self):
        reader = LogStageReader(self.path, r"^BEGIN (.+)$", start_from_tail=True)
        # Position the tail exactly at a marker-looking suffix of an old line.
        suffix = b"BEGIN false-stage\n"
        self.path.write_bytes(b"x" * 100 + suffix + b"z" * (reader.max_read_bytes - len(suffix)))
        self.assertEqual(reader.read(), "unattributed")
        self.path.rename(self.path.with_suffix(".old"))
        with self.path.open("wb") as stream:
            stream.write(b"BEGIN rotation-start\n")
            stream.write(b"z" * (reader.max_read_bytes * 2))
            stream.write(b"\nBEGIN rotation-tail\n")
        self.assertEqual(reader.read(), "rotation-start")
        self.assertEqual(reader._offset, reader.max_read_bytes)
        self.assertEqual(reader.read(), "rotation-start")
        self.assertEqual(reader.read(), "rotation-tail")

    def test_huge_unterminated_line_memory_and_read_budget_are_bounded(self):
        # Construct on disk without allocating a huge Python string.
        with self.path.open("wb") as stream:
            for _ in range(48):
                stream.write(b"x" * (256 * 1024))
        reader = LogStageReader(self.path, r"^BEGIN (.+)$", max_line_bytes=1024)
        tracemalloc.start()
        try:
            for poll in range(48):
                self.assertEqual(reader.read(), "unattributed")
                self.assertEqual(reader._offset, (poll + 1) * reader.max_read_bytes)
                self.assertLessEqual(len(reader._pending), 1024)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        self.append(b"\nBEGIN recovered\n")
        self.assertEqual(reader.read(), "recovered")

    def test_line_length_limit_and_utf8_across_chunks(self):
        reader = LogStageReader(self.path, r"^BEGIN (.+)$", max_line_bytes=16)
        self.append(b"BEGIN " + b"z" * 15)
        self.assertEqual(reader.read(), "unattributed")
        self.append(b"\nBEGIN valid\n")
        self.assertEqual(reader.read(), "valid")
        self.append("BEGIN 中文".encode("utf-8")[:-1])
        self.assertEqual(reader.read(), "valid")
        self.append("文".encode("utf-8")[-1:] + b"\n")
        self.assertEqual(reader.read(), "中文")


if __name__ == "__main__":
    unittest.main()
