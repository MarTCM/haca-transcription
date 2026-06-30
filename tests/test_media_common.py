"""Tests for transcription/tools/_media_common.py (shared pure helpers)."""

import datetime as dt
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import _media_common as mc  # noqa: E402


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Manarate", "Manarate"),
        ("a/b:c?d", "abcd"),
        ("  spaced   out  ", "spaced out"),
        ("trailing dots...", "trailing dots"),
        ("", "unknown-channel"),
        (None, "unknown-channel"),
    ],
)
def test_slugify_channel(title, expected):
    assert mc.slugify_channel(title) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("My Video", "My Video"),
        ("a/b:c?d", "abcd"),
        ("", "video"),
        (None, "video"),
    ],
)
def test_sanitize_filename(name, expected):
    assert mc.sanitize_filename(name) == expected


def test_sanitize_filename_truncates():
    assert mc.sanitize_filename("x" * 500, max_len=10) == "x" * 10


def test_stamp_from_naive_datetime_is_utc():
    d = dt.datetime(2026, 6, 17, 14, 30, 0)
    assert mc.stamp_from_datetime(d) == "20260617143000"


def test_stamp_from_aware_datetime_converts_to_utc():
    tz = dt.timezone(dt.timedelta(hours=2))
    d = dt.datetime(2026, 6, 17, 16, 30, 0, tzinfo=tz)  # = 14:30 UTC
    assert mc.stamp_from_datetime(d) == "20260617143000"


def test_dest_for_layout():
    out = mc.dest_for(Path("instagram"), "natgeo", "20260617143000", "My Reel", "mp3")
    assert out == Path("instagram/natgeo/2026/06/My Reel.mp3")


def test_dest_for_strips_dotted_ext():
    out = mc.dest_for(Path("/d"), "acct", "20260101000000", "t", ".m4a")
    assert out == Path("/d/acct/2026/01/t.m4a")


def test_archive_roundtrip(tmp_path):
    p = tmp_path / "sub" / "archive.txt"
    assert mc.load_archive(p) == set()
    mc.append_archive(p, "instagram abc")
    mc.append_archive(p, "youtube xyz")
    assert mc.load_archive(p) == {"instagram abc", "youtube xyz"}


def test_make_logger_file_and_stdout(tmp_path, capsys):
    p = tmp_path / "f.log"
    emit, close = mc.make_logger(p)
    emit("hello")
    close()
    assert "hello" in capsys.readouterr().out
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} hello", p.read_text())


def test_make_logger_none_is_noop(capsys):
    emit, close = mc.make_logger(None)
    emit("x")
    close()
    assert "x" in capsys.readouterr().out
