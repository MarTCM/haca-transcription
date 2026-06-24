"""
Structured run summary + log formatting.

Produces the exact log-line format shared with the Transcription UI's per-job
logs, so a CLI run and a UI job log look identical::

    [JOB START]  2026-06-23T14:30:00 | 120 files | pipeline=whisperx | ...
    [OK]         2026-06-23T14:30:05 | al-oula/2024/06/01/20240601000000.mp3 | 4.2s
    [FAIL]       2026-06-23T15:45:12 | 2m/2024/06/01/20240601050000.mp3 | CUDA out of memory
    [JOB END]    2026-06-23T15:46:00 | completed | 120/120 | 118 ok, 2 failed

The :class:`RunSummary` object accumulates per-file results so a caller can emit
log lines incrementally *and* print a final aggregate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# File-result statuses.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def _now_iso() -> str:
    """Local timestamp in ISO-8601 seconds precision."""
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class FileResult:
    """Outcome of transcribing a single file."""

    rel_path: str
    status: str
    srt_path: Optional[str] = None
    error: Optional[str] = None
    audio_seconds: Optional[float] = None
    processing_seconds: Optional[float] = None


# --------------------------------------------------------------------------- #
# Log-line formatting
# --------------------------------------------------------------------------- #
def format_job_start(total: int, config_summary: str, ts: Optional[str] = None) -> str:
    ts = ts or _now_iso()
    return f"[JOB START]  {ts} | {total} files | {config_summary}"


def format_ok(rel_path: str, processing_seconds: Optional[float],
              ts: Optional[str] = None) -> str:
    ts = ts or _now_iso()
    dur = f"{processing_seconds:.1f}s" if processing_seconds is not None else "-"
    return f"[OK]         {ts} | {rel_path} | {dur}"


def format_skip(rel_path: str, ts: Optional[str] = None) -> str:
    ts = ts or _now_iso()
    return f"[SKIP]       {ts} | {rel_path} | exists (use --overwrite)"


def format_fail(rel_path: str, error: str, ts: Optional[str] = None) -> str:
    ts = ts or _now_iso()
    return f"[FAIL]       {ts} | {rel_path} | {error}"


def format_job_end(status: str, done: int, total: int, ok: int, failed: int,
                   ts: Optional[str] = None) -> str:
    ts = ts or _now_iso()
    return (f"[JOB END]    {ts} | {status} | {done}/{total} | "
            f"{ok} ok, {failed} failed")


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
@dataclass
class RunSummary:
    """Accumulates :class:`FileResult` objects and reports aggregate counts."""

    total: int = 0
    results: List[FileResult] = field(default_factory=list)
    _t0: float = field(default_factory=time.monotonic)

    def add(self, result: FileResult) -> FileResult:
        self.results.append(result)
        return result

    @property
    def ok(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_COMPLETED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_SKIPPED)

    @property
    def done(self) -> int:
        """Files actually attempted (completed + failed), excluding skips."""
        return self.ok + self.failed

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._t0

    @property
    def status(self) -> str:
        """Overall run status: 'failed' if any file failed, else 'completed'."""
        return STATUS_FAILED if self.failed else STATUS_COMPLETED

    def end_line(self, ts: Optional[str] = None) -> str:
        return format_job_end(
            self.status, self.done, self.total, self.ok, self.failed, ts=ts
        )

    def oneline(self) -> str:
        """Compact human summary for stdout."""
        parts = [f"{self.ok} ok", f"{self.failed} failed"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return (f"{', '.join(parts)} of {self.total} "
                f"in {self.elapsed_seconds:.1f}s")
