from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from memory_monitor.sampler import CgroupReader, ProcessInfo, ProcessSampler, ProcessTree, Snapshot


def write_stat(root, pid, *, ppid=1, start=100, pages=5, name="worker"):
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    # The fields after comm begin at stat field 3; RSS is field 24.
    tail = ["S", str(ppid)] + ["0"] * 17 + [str(start), "4096", str(pages)]
    (directory / "stat").write_text(f"{pid} ({name}) {' '.join(tail)}\n")


def proc(pid, ppid=1, identity=None, rss=1024):
    return ProcessInfo(pid, ppid, identity or f"start:{pid}", rss)


def snapshot(*records):
    return Snapshot({record.pid: record for record in records})


class LinuxSamplerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.sampler = ProcessSampler(self.root, platform="linux")

    def test_spaces_and_closing_parentheses_in_comm(self):
        write_stat(self.root, 13, ppid=2, start=321, pages=7, name="a ) (space) )")
        (self.root / "meminfo").write_text("Not a process")
        result = self.sampler.read()
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.records[13], ProcessInfo(13, 2, "linux:321", 7 * self.sampler.page_size, "S"))

    def test_missing_and_malformed_records_are_omitted_and_counted(self):
        write_stat(self.root, 13)
        (self.root / "14").mkdir()
        (self.root / "15").mkdir()
        (self.root / "15" / "stat").write_text("15 (broken) x")
        write_stat(self.root, 16, pages=-1)
        result = self.sampler.read()
        self.assertEqual(set(result.records), {13})
        self.assertEqual(result.errors, 3)

    def test_non_utf8_process_name_does_not_hide_valid_numeric_counters(self):
        write_stat(self.root, 13)
        path = self.root / "13" / "stat"
        path.write_bytes(path.read_bytes().replace(b"worker", b"bad\xffname"))
        self.assertEqual(self.sampler.read().records[13].identity, "linux:100")

    def test_wrong_pid_is_not_attributed_to_another_process(self):
        write_stat(self.root, 13)
        write_stat(self.root, 14)
        (self.root / "14" / "stat").write_text((self.root / "13" / "stat").read_text())
        self.assertEqual(self.sampler.read().errors, 1)

    def test_empty_or_unavailable_process_source_is_explicit_failure(self):
        with self.assertRaises(RuntimeError):
            self.sampler.read()
        with self.assertRaises(RuntimeError):
            ProcessSampler(self.root / "absent", platform="linux").read()

    def test_pss_counts_readable_unique_processes_and_labels_partial_result(self):
        for pid in range(10, 14):
            (self.root / str(pid)).mkdir()
        (self.root / "10" / "smaps_rollup").write_text("Rss: 99 kB\nPss: 42 kB\nPss_Anon: 12 kB\n")
        (self.root / "11" / "smaps_rollup").write_text("Pss: 10 kB\n")
        (self.root / "12" / "smaps_rollup").write_text("Pss: -1 kB\n")
        self.assertEqual(self.sampler.pss([10, 11, 12, 13, 10]), (52 * 1024, 2))
        self.assertEqual(self.sampler.pss([12, 13]), (None, 0))
        self.assertEqual(self.sampler.pss([]), (None, 0))


class MacSamplerTests(unittest.TestCase):
    def test_ps_records_creation_identity_rss_and_parse_errors(self):
        result = subprocess.CompletedProcess([], 0,
            "  42  1  12 S Mon Oct  5 09:10:11 2026\n  44 42 0 Z Mon Oct 5 09:10:12 2026\nbroken\n", "")
        sampler = ProcessSampler(platform="darwin")
        with patch("memory_monitor.sampler.subprocess.run", return_value=result) as run:
            value = sampler.read()
        self.assertEqual(value.records[42].rss_bytes, 12 * 1024)
        self.assertEqual(value.records[44].ppid, 42)
        self.assertEqual(value.records[44].state, "Z")
        self.assertEqual(value.records[42].identity, "ps:Mon Oct  5 09:10:11 2026")
        self.assertEqual(value.errors, 1)
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertEqual(sampler.pss([42]), (None, 0))

    def test_ps_failure_empty_output_and_timeout_are_explicit_failures(self):
        for result in (subprocess.CompletedProcess([], 1, "", "denied"), subprocess.CompletedProcess([], 0, "", "")):
            with self.subTest(result=result), patch("memory_monitor.sampler.subprocess.run", return_value=result):
                with self.assertRaises(RuntimeError):
                    ProcessSampler(platform="darwin").read()
        with patch("memory_monitor.sampler.subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 10)):
            with self.assertRaises(RuntimeError):
                ProcessSampler(platform="darwin").read()


class ProcessTreeTests(unittest.TestCase):
    def test_discovers_full_tree_independent_of_input_order(self):
        tree = ProcessTree(10, "start:10")
        result = tree.select(snapshot(proc(13, 12), proc(99), proc(12, 11), proc(11, 10), proc(10)))
        self.assertEqual([p.pid for p in result], [10, 11, 12, 13])

    def test_observed_descendants_remain_after_reparenting_and_root_exit(self):
        tree = ProcessTree(10, "start:10")
        tree.select(snapshot(proc(10), proc(11, 10), proc(12, 11)))
        result = tree.select(snapshot(proc(11, 1), proc(12, 11), proc(13, 12), proc(99)))
        self.assertEqual([p.pid for p in result], [11, 12, 13])
        self.assertEqual(tree.select(snapshot(proc(99))), [])

    def test_root_and_orphan_pid_reuse_are_not_inherited(self):
        tree = ProcessTree(10, "start:10")
        tree.select(snapshot(proc(10), proc(11, 10), proc(12, 10)))
        result = tree.select(snapshot(proc(10, identity="new"), proc(20, 10),
                                      proc(11, identity="new"), proc(12, 1)))
        self.assertEqual([p.pid for p in result], [12])
        # Even if reused root is now a child of an observed descendant, exclude it.
        self.assertEqual([p.pid for p in tree.select(snapshot(proc(10, 12, "new"), proc(12)))], [12])

    def test_initial_wrong_root_identity_is_never_discovered(self):
        tree = ProcessTree(10, "expected")
        self.assertEqual(tree.select(snapshot(proc(10), proc(11, 10))), [])

    def test_disappearance_discards_tracked_identity(self):
        tree = ProcessTree(10, "start:10")
        tree.select(snapshot(proc(10), proc(11, 10)))
        tree.select(snapshot(proc(10)))
        self.assertEqual([p.pid for p in tree.select(snapshot(proc(10), proc(11, 1)))], [10])


class CgroupReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_reads_optional_counters_without_mutating_files(self):
        contents = {"memory.current": "123\n", "memory.peak": "456\n",
                    "memory.events": "low 0\nhigh 2\nmax 3\noom 4\noom_kill 1\noom_group_kill 0\n"}
        for name, text in contents.items():
            (self.root / name).write_text(text)
        self.assertEqual(CgroupReader(self.root).read(), {
            "current_bytes": 123, "peak_bytes": 456, "events_oom": 4, "events_oom_kill": 1})
        self.assertEqual({name: (self.root / name).read_text() for name in contents}, contents)

    def test_absent_optional_files_are_absent_not_zero(self):
        self.assertEqual(CgroupReader(self.root).read(), {})
        (self.root / "memory.current").write_text("0")
        self.assertEqual(CgroupReader(self.root).read(), {"current_bytes": 0})

    def test_present_invalid_counters_fail_explicitly(self):
        for text in ("-1", "max", "", "not an integer"):
            with self.subTest(text=text):
                (self.root / "memory.current").write_text(text)
                with self.assertRaises(RuntimeError):
                    CgroupReader(self.root).read()
        (self.root / "memory.current").unlink()
        (self.root / "memory.events").write_text("oom invalid\n")
        with self.assertRaises(RuntimeError):
            CgroupReader(self.root).read()

    def test_missing_directory_and_permission_errors_fail_explicitly(self):
        with self.assertRaises(ValueError):
            CgroupReader(self.root / "absent")
        reader = CgroupReader(self.root)
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(RuntimeError):
                reader.read()
        self.root.rmdir()
        with self.assertRaises(RuntimeError):
            reader.read()


if __name__ == "__main__":
    unittest.main()
