"""Tests for core.selection: range parsing, scanning, expansion."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.selection import (  # noqa: E402
    SelectionError,
    parse_ranges,
    parse_channels,
    scan_medias,
    hour_of,
    expand_selections,
)


# --------------------------------------------------------------------------- #
# Range / list parsing
# --------------------------------------------------------------------------- #
def test_parse_ranges_none_and_empty():
    assert parse_ranges(None) is None
    assert parse_ranges("") is None
    assert parse_ranges("   ") is None


def test_parse_ranges_list():
    assert parse_ranges("1,3,5") == {1, 3, 5}


def test_parse_ranges_range():
    assert parse_ranges("9-12") == {9, 10, 11, 12}


def test_parse_ranges_mixed():
    assert parse_ranges("9-12,15,18-20") == {9, 10, 11, 12, 15, 18, 19, 20}


def test_parse_ranges_whitespace():
    assert parse_ranges(" 1 , 2 - 4 ") == {1, 2, 3, 4}


def test_parse_ranges_reversed_raises():
    with pytest.raises(SelectionError, match="reversed"):
        parse_ranges("18-9")


def test_parse_ranges_nonint_raises():
    with pytest.raises(SelectionError):
        parse_ranges("9-x")
    with pytest.raises(SelectionError):
        parse_ranges("abc")


def test_parse_channels_variants():
    assert parse_channels(None) is None
    assert parse_channels("al-oula,2m") == {"al-oula", "2m"}
    assert parse_channels(["al-oula", "2m,rabat"]) == {"al-oula", "2m", "rabat"}


def test_hour_of():
    assert hour_of("202406010900.mp3") == 9
    assert hour_of("202406012300.mp3") == 23
    assert hour_of("not-a-stamp.mp3") is None


# --------------------------------------------------------------------------- #
# Fixture medias tree
# --------------------------------------------------------------------------- #
@pytest.fixture
def medias(tmp_path):
    """Build a small medias tree:

    al-oula/2024/06/01/{0900,1000,2300}
    al-oula/2024/07/01/{0900}
    2m/2024/06/02/{0900,1800}
    2m/2025/01/15/{1200}
    """
    layout = {
        "al-oula/2024/06/01": ["202406010900", "202406011000", "202406012300"],
        "al-oula/2024/07/01": ["202407010900"],
        "2m/2024/06/02": ["202406020900", "202406021800"],
        "2m/2025/01/15": ["202501151200"],
    }
    for rel, stamps in layout.items():
        d = tmp_path / rel
        d.mkdir(parents=True)
        for stamp in stamps:
            (d / f"{stamp}.mp3").write_bytes(b"\x00")
    # A stray non-numeric dir + non-mp3 file that must be ignored.
    (tmp_path / "al-oula/2024/06/01/notes.txt").write_text("ignore me")
    (tmp_path / "al-oula/misc").mkdir(parents=True)
    return tmp_path


def test_scan_missing_dir_is_empty(tmp_path):
    assert scan_medias(tmp_path / "nope") == {}


def test_scan_structure(medias):
    idx = scan_medias(medias)
    assert set(idx) == {"al-oula", "2m"}
    assert set(idx["al-oula"]) == {2024}
    assert set(idx["al-oula"][2024]) == {6, 7}
    assert idx["al-oula"][2024][6][1] == [
        "202406010900.mp3", "202406011000.mp3", "202406012300.mp3"
    ]
    # stray .txt is ignored
    assert all(f.endswith(".mp3") for f in idx["al-oula"][2024][6][1])


def test_expand_all(medias):
    files = expand_selections(medias)
    assert len(files) == 7
    assert files == sorted(files)
    assert "al-oula/2024/06/01/202406010900.mp3" in files


def test_expand_channel_filter(medias):
    files = expand_selections(medias, channels=["2m"])
    assert all(f.startswith("2m/") for f in files)
    assert len(files) == 3


def test_expand_year_month(medias):
    files = expand_selections(medias, years={2024}, months={6})
    assert len(files) == 5  # 3 al-oula + 2 2m in 2024-06
    assert all("/2024/06/" in f for f in files)


def test_expand_hours_range(medias):
    # hours 9-12 should drop the 18:00 and 23:00 files
    files = expand_selections(medias, hours={9, 10, 11, 12})
    hours = {hour_of(Path(f).name) for f in files}
    assert hours <= {9, 10, 11, 12}
    assert "al-oula/2024/06/01/202406012300.mp3" not in files
    assert "2m/2024/06/02/202406021800.mp3" not in files


def test_expand_combined(medias):
    files = expand_selections(
        medias, channels=["al-oula"], years={2024}, months={6},
        days={1}, hours={9, 10},
    )
    assert files == [
        "al-oula/2024/06/01/202406010900.mp3",
        "al-oula/2024/06/01/202406011000.mp3",
    ]


def test_expand_no_match(medias):
    assert expand_selections(medias, channels=["does-not-exist"]) == []


# --------------------------------------------------------------------------- #
# Video / mixed media extensions
# --------------------------------------------------------------------------- #
def test_hour_of_video_extensions():
    assert hour_of("202406010900.mp4") == 9
    assert hour_of("202406011830.mkv") == 18
    assert hour_of("202406010900.MP4") == 9  # case-insensitive extension


@pytest.fixture
def mixed_media(tmp_path):
    """A day folder mixing audio, video, and a non-media file."""
    d = tmp_path / "tv/2024/06/01"
    d.mkdir(parents=True)
    for name in ["202406010900.mp3", "202406011000.mp4", "202406011100.mkv",
                 "202406011200.ts", "notes.txt", "202406011300.json"]:
        (d / name).write_bytes(b"\x00")
    return tmp_path


def test_scan_includes_video_excludes_non_media(mixed_media):
    idx = scan_medias(mixed_media)
    files = idx["tv"][2024][6][1]
    assert files == [
        "202406010900.mp3",
        "202406011000.mp4",
        "202406011100.mkv",
        "202406011200.ts",
    ]
    assert "notes.txt" not in files
    assert "202406011300.json" not in files  # .json is not a media ext


def test_expand_picks_up_video(mixed_media):
    files = expand_selections(mixed_media)
    assert "tv/2024/06/01/202406011000.mp4" in files
    assert "tv/2024/06/01/202406011100.mkv" in files


def test_expand_hour_filter_across_extensions(mixed_media):
    # 10:00 (mp4) and 11:00 (mkv) only
    files = expand_selections(mixed_media, hours={10, 11})
    assert files == [
        "tv/2024/06/01/202406011000.mp4",
        "tv/2024/06/01/202406011100.mkv",
    ]
