"""
Round-trip / format tests for srt_writer.

No ASR model or audio is required: we render segments to SRT, parse the file
back with a standalone regex, and assert indices, timestamps, and text survive
exactly. The parse mirrors the standard SRT contract that downstream consumers
(e.g. the HACA benchmark's srt_utils.parse_srt) rely on.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from srt_writer import _fmt_ts, render_srt, write_srt  # noqa: E402

BLOCK_RE = re.compile(
    r"(\d+)\n"
    r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n"
    r"(.+?)(?=\n\n|\Z)",
    re.DOTALL,
)


def parse(srt_text):
    out = []
    for m in BLOCK_RE.finditer(srt_text):
        out.append({
            "index": int(m.group(1)),
            "start": m.group(2),
            "end": m.group(3),
            "text": m.group(4).strip(),
        })
    return out


def test_fmt_ts_basic():
    assert _fmt_ts(0) == "00:00:00,000"
    assert _fmt_ts(1.5) == "00:00:01,500"
    assert _fmt_ts(61.25) == "00:01:01,250"
    assert _fmt_ts(3723.004) == "01:02:03,004"


def test_fmt_ts_rounds_to_ms_without_overflow():
    # 59.9996 s must not print as 60 seconds.
    assert _fmt_ts(59.9996) == "00:01:00,000"
    assert _fmt_ts(-5) == "00:00:00,000"


def test_round_trip_preserves_fields():
    segments = [
        {"start": 0.0, "end": 2.5, "text": "السلام عليكم"},
        {"start": 2.5, "end": 5.0, "text": "Bonjour tout le monde"},
        {"start": 5.0, "end": 7.25, "text": "كيف داير"},
    ]
    parsed = parse(render_srt(segments))
    assert [p["index"] for p in parsed] == [1, 2, 3]
    assert [p["text"] for p in parsed] == [s["text"] for s in segments]
    assert parsed[0]["start"] == "00:00:00,000"
    assert parsed[2]["end"] == "00:00:07,250"


def test_empty_text_segments_skipped_and_reindexed():
    segments = [
        {"start": 0.0, "end": 1.0, "text": "first"},
        {"start": 1.0, "end": 2.0, "text": "   "},
        {"start": 2.0, "end": 3.0, "text": "second"},
    ]
    parsed = parse(render_srt(segments))
    assert [p["index"] for p in parsed] == [1, 2]
    assert [p["text"] for p in parsed] == ["first", "second"]


def test_internal_newlines_collapsed():
    parsed = parse(render_srt([{"start": 0, "end": 1, "text": "line one\nline two"}]))
    assert parsed[0]["text"] == "line one line two"


def test_write_srt_creates_utf8_file(tmp_path):
    path = write_srt([{"start": 0, "end": 1, "text": "café شكرا"}], tmp_path / "x.srt")
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("1\n00:00:00,000 --> 00:00:01,000")


def test_empty_segments_yield_empty_file(tmp_path):
    path = write_srt([], tmp_path / "empty.srt")
    assert path.read_text(encoding="utf-8") == ""
