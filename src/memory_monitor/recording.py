"""Bounded summaries and optional, approximate stage attribution from text logs."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def _stage_label(value: Any) -> str:
    return str(value or "unattributed").strip()[:256] or "unattributed"


class Recorder:
    """Flush every sample to disk, retaining only one sample and bounded summaries.

    ``output`` must be an already-created, fresh output directory. The exclusive
    sample file prevents accidentally appending a new run to an old recording.
    These are sampled peaks; a sampling interval can miss transient spikes.
    """

    def __init__(self, output: Path, max_stages: int = 256) -> None:
        if max_stages < 0:
            raise ValueError("max_stages must be nonnegative")
        self.output = Path(output)
        self.max_stages = max_stages
        self._stream = (self.output / "samples.jsonl").open("x", encoding="utf-8")
        self._latest: dict[str, Any] | None = None
        self._sample_count = 0
        self._incomplete = 0
        self._error_count = 0
        self._sampling_seconds_total = 0.0
        self._sampling_seconds_max = 0.0
        self._overflow_samples = 0
        self._stages: dict[str, dict[str, Any]] = {}
        self._peaks: dict[str, dict[str, Any] | None] = {
            "root_rss": None, "tree_rss": None, "monitor_rss": None,
            "pss": None, "process_count": None,
        }

    def add(self, sample: dict[str, Any]) -> None:
        sample = dict(sample)
        sample["stage"] = _stage_label(sample.get("stage"))
        self._stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
        self._stream.flush()
        self._latest = copy.deepcopy(sample)
        self._sample_count += 1
        self._incomplete += int(not sample.get("sample_complete", True))
        self._error_count += int(sample.get("error_count", len(sample.get("errors", []))))
        seconds = float(sample.get("sample_seconds", 0.0))
        self._sampling_seconds_total += seconds
        self._sampling_seconds_max = max(self._sampling_seconds_max, seconds)
        for peak_name, field in (
            ("root_rss", "root_rss_bytes"), ("tree_rss", "tree_rss_bytes"),
            ("monitor_rss", "monitor_rss_bytes"), ("pss", "pss_bytes"),
            ("process_count", "process_count"),
        ):
            value = sample.get(field)
            if value is None:
                continue
            value_key = "value" if peak_name == "process_count" else "value_bytes"
            previous = self._peaks[peak_name]
            if previous is None or value > previous[value_key]:
                peak = {
                    value_key: value, "timestamp": sample.get("timestamp"),
                    "elapsed_seconds": sample.get("elapsed_seconds"),
                    "stage": sample["stage"],
                    "sample_complete": sample.get("sample_complete", True),
                }
                if peak_name == "pss":
                    peak["pss_process_count"] = sample.get("pss_process_count", 0)
                    peak["process_count"] = sample.get("process_count", 0)
                self._peaks[peak_name] = peak

        label = sample["stage"]
        if label not in self._stages and len(self._stages) - int("__other__" in self._stages) >= self.max_stages:
            label = "__other__"
            self._overflow_samples += 1
        stage = self._stages.setdefault(label, {
            "sample_count": 0, "incomplete_sample_count": 0,
            "first_timestamp": sample.get("timestamp"), "last_timestamp": None,
            "peak_root_rss_bytes": None, "peak_tree_rss_bytes": None,
            "peak_monitor_rss_bytes": None, "peak_pss_bytes": None,
            "peak_process_count": 0,
        })
        stage["sample_count"] += 1
        stage["incomplete_sample_count"] += int(not sample.get("sample_complete", True))
        stage["last_timestamp"] = sample.get("timestamp")
        for field in ("root_rss_bytes", "tree_rss_bytes", "monitor_rss_bytes", "pss_bytes", "process_count"):
            value = sample.get(field)
            key = "peak_" + field
            if value is not None and (stage[key] is None or value > stage[key]):
                stage[key] = value

    def summary(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({
            "schema_version": 1, "metadata": metadata,
            "sample_count": self._sample_count,
            "incomplete_sample_count": self._incomplete,
            "sampling_error_count": self._error_count,
            "sampling_seconds_total": self._sampling_seconds_total,
            "sampling_seconds_max": self._sampling_seconds_max,
            "latest": self._latest, "peaks": self._peaks, "stages": self._stages,
            "max_stages": self.max_stages,
            "overflow_stage_sample_count": self._overflow_samples,
            "peak_kind": "sampled; transient peaks between samples may be missed",
            "stage_attribution": "last recognized log marker; approximate",
        })

    def write_summary(self, metadata: dict[str, Any]) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.output,
                prefix=".summary-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(self.summary(metadata), stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.output / "summary.json")
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def close(self) -> None:
        self._stream.close()


_TOP_LEVEL_STARTS = {
    "[0/7] loading and validating data", "[1/7] generating factor family",
    "[2/7] health checks on EIF universe", "[3/7] predictive evaluation and composite",
    "[4/7] temporal stability, persistence and return shape",
    "[5/7] controlled knob attribution", "[6/7] rolling walk-forward member selection",
    "[7/7] building report",
}
_SELECT_GLOBAL_STARTS = {
    "[select] preparing folds and labels",
    "[select] reading and aligning full-period diagnostic keys",
    "[select] preparing manifest and hashing inputs",
    "[select] causal replay reading and preparing reference keys",
    "[select] aggregating and writing final selection artifacts",
}
_SELECT_ACTIONS = {
    "preparing historical data", "writing historical feature scratch parquet",
    "causal replay", "reading Train/Selection feature windows", "training Rank IC",
    "extracting Train/Selection feature windows from memory",
    "selection health and Rank IC", "shape, turnover and parameter stability",
    "gates, bootstrap and quality scoring", "value correlation",
    "IC correlation and marginal selection", "cross-fold stability and publication decision",
    "writing frozen evidence and manifest hashes", "confirmation diagnostics",
    "aligning comparison keys",
}
_SELECT_START = re.compile(
    r"^\[select\] (?:fold_\d+(?: \(\d+/\d+\))?(?: [^:]{1,80})?"
    r"|causal replay cutoff=\d{4}-\d{2}-\d{2}): (?P<action>.+)$"
)
_SELECT_GENERATE = re.compile(r"^\[select\] fold_\d+ \(\d+/\d+\) generating historical features$")
_MEMBER_ACTION = re.compile(r"^(?:training Rank IC \(\d+ members\)|reading and comparing \d+ members)$")
_LOG_TIMESTAMP = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)\] "
)


class LogStageReader:
    """Incrementally read bounded chunks, ignoring long lines and non-start logs.

    At most 256 KiB is consumed per poll (plus 64 bytes for rewrite detection).
    A log backlog can therefore delay attribution. ``start_from_tail`` starts
    the first successfully opened file at its last bounded chunk; use it when
    attaching to an existing run. Older stage starts may then be unavailable.
    Complete lines alone are parsed; completion/progress lines never replace a
    stage by default. Later log rotations are always read from their beginning.
    """

    max_read_bytes = 256 * 1024

    def __init__(self, path: Path, pattern: str | None = None, max_line_bytes: int = 65536,
                 *, start_from_tail: bool = False) -> None:
        if max_line_bytes < 1:
            raise ValueError("max_line_bytes must be positive")
        self.path = Path(path)
        self.max_line_bytes = max_line_bytes
        self.pattern = re.compile(pattern) if pattern is not None else None
        if self.pattern is not None and not self.pattern.groups:
            raise ValueError("stage pattern requires a named 'stage' group or a capture group")
        self.stage = "unattributed"
        self.read_error_count = 0
        self._start_from_tail = start_from_tail
        self._opened_once = False
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._anchor = b""
        self._pending = bytearray()
        self._dropping = False

    def _reset(self, identity: tuple[int, int]) -> None:
        self._identity = identity
        self._offset = 0
        self._anchor = b""
        self._pending.clear()
        self._dropping = False
        self.stage = "unattributed"

    def _line(self, raw: bytearray) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if self.pattern is not None:
            match = self.pattern.search(line)
            if match is not None:
                label = match.group("stage") if "stage" in self.pattern.groupindex else match.group(1)
                if label:
                    self.stage = _stage_label(label)
            return
        # FAW console timestamps are metadata, not part of a stage identity.
        # Custom patterns above still see the original line for compatibility.
        line = _LOG_TIMESTAMP.sub("", line, count=1)
        if line in _TOP_LEVEL_STARTS or line in _SELECT_GLOBAL_STARTS or _SELECT_GENERATE.fullmatch(line):
            self.stage = _stage_label(line)
            return
        match = _SELECT_START.fullmatch(line)
        if match is not None:
            action = match.group("action")
            if action in _SELECT_ACTIONS or _MEMBER_ACTION.fullmatch(action):
                self.stage = _stage_label(line)

    def read(self) -> str:
        try:
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                identity = (info.st_dev, info.st_ino)
                if identity != self._identity or info.st_size < self._offset:
                    self._reset(identity)
                elif self._anchor:
                    stream.seek(self._offset - len(self._anchor))
                    if stream.read(len(self._anchor)) != self._anchor:
                        self._reset(identity)
                if not self._opened_once:
                    if self._start_from_tail and info.st_size > self.max_read_bytes:
                        self._offset = info.st_size - self.max_read_bytes
                        # Starting inside an arbitrary line cannot yield a
                        # trustworthy marker. Discard through its next newline.
                        self._dropping = True
                    self._opened_once = True
                stream.seek(self._offset)
                chunk = stream.read(self.max_read_bytes)
                self._offset += len(chunk)
                if chunk:
                    self._anchor = (self._anchor + chunk[-64:])[-64:]
        except OSError:
            self.read_error_count += 1
            return self.stage
        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            end = len(chunk) if newline < 0 else newline
            if not self._dropping:
                if len(self._pending) + end - cursor > self.max_line_bytes:
                    self._pending.clear()
                    self._dropping = True
                else:
                    self._pending.extend(chunk[cursor:end])
            if newline < 0:
                break
            if not self._dropping:
                self._line(self._pending)
            self._pending.clear()
            self._dropping = False
            cursor = newline + 1
        return self.stage
