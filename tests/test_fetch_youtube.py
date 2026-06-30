"""Tests for transcription/tools/fetch_youtube.py.

Pure helpers are tested directly; the orchestration loop is tested against a
fake YoutubeDL so nothing touches the network or ffmpeg.
"""

import sys
import re
from pathlib import Path

import pytest

# fetch_youtube.py lives in transcription/tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fetch_youtube as fy  # noqa: E402


# --------------------------------------------------------------------------- #
# slugify_channel
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Manarate", "Manarate"),
        ("HACA / قناة", "HACA قناة"),  # slash removed, the two spaces collapse to one
        ("a:b*c?d", "abcd"),
        ("  spaced   out  ", "spaced out"),
        ("trailing dots...", "trailing dots"),
        ("", "unknown-channel"),
        (None, "unknown-channel"),
        ("///", "unknown-channel"),
    ],
)
def test_slugify_channel(title, expected):
    assert fy.slugify_channel(title) == expected


def test_slugify_channel_collapses_internal_whitespace():
    # The slash is removed leaving two spaces, which collapse to one.
    assert fy.slugify_channel("HACA / قناة") == "HACA قناة"


# --------------------------------------------------------------------------- #
# stamp_from_info
# --------------------------------------------------------------------------- #


def test_stamp_prefers_timestamp_utc():
    # 2026-06-17 14:30:00 UTC
    ts = 1781706600
    assert fy.stamp_from_info({"timestamp": ts}) == "20260617143000"


def test_stamp_falls_back_to_upload_date():
    assert fy.stamp_from_info({"upload_date": "20260613"}) == "20260613000000"


def test_stamp_timestamp_wins_over_upload_date():
    out = fy.stamp_from_info({"timestamp": 1781706600, "upload_date": "20200101"})
    assert out == "20260617143000"


def test_stamp_raises_without_either():
    with pytest.raises(ValueError):
        fy.stamp_from_info({"title": "no dates here"})


def test_stamp_rejects_malformed_upload_date():
    with pytest.raises(ValueError):
        fy.stamp_from_info({"upload_date": "2026-06"})


# --------------------------------------------------------------------------- #
# sanitize_filename / dest_path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title,expected",
    [
        ("My Video", "My Video"),
        ("a/b:c?d", "abcd"),
        ("  spaced   out  ", "spaced out"),
        ("trailing dots...", "trailing dots"),
        ("", "video"),
        (None, "video"),
        ("///", "video"),
    ],
)
def test_sanitize_filename(title, expected):
    assert fy.sanitize_filename(title) == expected


def test_sanitize_filename_truncates():
    out = fy.sanitize_filename("x" * 500, max_len=10)
    assert out == "x" * 10


def test_sanitize_filename_custom_fallback():
    assert fy.sanitize_filename(None, fallback="20260101000000") == "20260101000000"


def test_dest_path_layout_uses_title():
    info = {"timestamp": 1781706600, "title": "Episode One"}
    out = fy.dest_path(Path("youtube"), "Manarate", info, "mp3")
    assert out == Path("youtube/Manarate/2026/06/Episode One.mp3")


def test_dest_path_falls_back_to_stamp_without_title():
    info = {"timestamp": 1781706600}
    out = fy.dest_path(Path("youtube"), "Manarate", info, "mp3")
    assert out == Path("youtube/Manarate/2026/06/20260617143000.mp3")


def test_dest_path_strips_dotted_ext_and_sanitizes_title():
    info = {"upload_date": "20260101", "title": "Q1: results?"}
    out = fy.dest_path(Path("/data/youtube"), "Chan", info, ".m4a")
    assert out == Path("/data/youtube/Chan/2026/01/Q1 results.m4a")


# --------------------------------------------------------------------------- #
# normalize_channel_url
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/@Chan", "https://www.youtube.com/@Chan/videos"),
        ("https://www.youtube.com/@Chan/", "https://www.youtube.com/@Chan/videos"),
        (
            "https://www.youtube.com/channel/UC123",
            "https://www.youtube.com/channel/UC123/videos",
        ),
        # already a tab -> unchanged
        ("https://www.youtube.com/@Chan/videos", "https://www.youtube.com/@Chan/videos"),
        ("https://www.youtube.com/@Chan/streams", "https://www.youtube.com/@Chan/streams"),
        # single video -> unchanged
        ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
    ],
)
def test_normalize_channel_url(url, expected):
    assert fy.normalize_channel_url(url) == expected


# --------------------------------------------------------------------------- #
# archive_key / entry_url
# --------------------------------------------------------------------------- #


def test_archive_key_format():
    assert fy.archive_key({"ie_key": "Youtube", "id": "abc123"}) == "youtube abc123"


def test_archive_key_defaults_extractor():
    assert fy.archive_key({"id": "abc123"}) == "youtube abc123"


def test_entry_url_from_id():
    assert fy.entry_url({"id": "abc"}) == "https://www.youtube.com/watch?v=abc"


def test_entry_url_fallback():
    assert fy.entry_url({"url": "https://x/y"}) == "https://x/y"


# --------------------------------------------------------------------------- #
# archive read/write
# --------------------------------------------------------------------------- #


def test_load_archive_missing(tmp_path):
    assert fy.load_archive(tmp_path / "nope.txt") == set()


def test_append_then_load_archive(tmp_path):
    p = tmp_path / "sub" / "archive.txt"
    fy.append_archive(p, "youtube a")
    fy.append_archive(p, "youtube b")
    assert fy.load_archive(p) == {"youtube a", "youtube b"}


# --------------------------------------------------------------------------- #
# build_ydl_opts
# --------------------------------------------------------------------------- #


def test_build_ydl_opts():
    opts = fy.build_ydl_opts(Path("/tmp/out"), "My Title", "mp3")
    assert opts["paths"] == {"home": "/tmp/out"}
    assert opts["outtmpl"] == {"default": "My Title.%(ext)s"}
    assert opts["format"] == "bestaudio/best"
    pp = opts["postprocessors"][0]
    assert pp["key"] == "FFmpegExtractAudio"
    assert pp["preferredcodec"] == "mp3"
    assert "js_runtimes" not in opts  # omitted when not requested


def test_build_ydl_opts_escapes_percent_in_title():
    opts = fy.build_ydl_opts(Path("/tmp"), "100% real", "mp3")
    assert opts["outtmpl"] == {"default": "100%% real.%(ext)s"}


def test_build_ydl_opts_includes_js_runtimes():
    opts = fy.build_ydl_opts(Path("/tmp"), "t", "mp3", {"node": {"path": None}})
    assert opts["js_runtimes"] == {"node": {"path": None}}


# --------------------------------------------------------------------------- #
# parse_js_runtimes
# --------------------------------------------------------------------------- #


def test_parse_js_runtimes_none():
    assert fy.parse_js_runtimes(None) is None
    assert fy.parse_js_runtimes([]) is None


def test_parse_js_runtimes_name_only():
    assert fy.parse_js_runtimes(["node"]) == {"node": {"path": None}}


def test_parse_js_runtimes_with_path():
    assert fy.parse_js_runtimes(["deno:/opt/deno"]) == {"deno": {"path": "/opt/deno"}}


def test_parse_js_runtimes_multiple_and_lowercased():
    out = fy.parse_js_runtimes(["Node", "deno:/x"])
    assert out == {"node": {"path": None}, "deno": {"path": "/x"}}


# --------------------------------------------------------------------------- #
# Orchestration with a fake YoutubeDL
# --------------------------------------------------------------------------- #


class FakeYDL:
    """Minimal stand-in for yt_dlp.YoutubeDL.

    Configured at the class level with a flat channel listing and a per-video
    info map. Records which video URLs got "downloaded" and writes a dummy file
    so dest.exists() behaves realistically.
    """

    entries: list = []
    info_map: dict = {}
    downloaded: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if self.opts.get("extract_flat"):
            return {"entries": list(FakeYDL.entries)}
        # full info extraction for a single video
        vid = url.rsplit("=", 1)[-1]
        return FakeYDL.info_map[vid]

    def download(self, urls):
        for u in urls:
            FakeYDL.downloaded.append(u)
            # Simulate ffmpeg writing the final file.
            home = Path(self.opts["paths"]["home"])
            name = self.opts["outtmpl"]["default"].replace("%(ext)s", "mp3")
            home.mkdir(parents=True, exist_ok=True)
            (home / name).write_text("audio")


@pytest.fixture
def fake_ydl():
    FakeYDL.entries = []
    FakeYDL.info_map = {}
    FakeYDL.downloaded = []
    return FakeYDL


def test_download_new_downloads_all_new(tmp_path, fake_ydl):
    fake_ydl.entries = [
        {"ie_key": "Youtube", "id": "v1"},
        {"ie_key": "Youtube", "id": "v2"},
    ]
    fake_ydl.info_map = {
        "v1": {"timestamp": 1781706600, "channel": "Manarate", "title": "One"},
        "v2": {"timestamp": 1781793000, "channel": "Manarate", "title": "Two"},
    }
    cfg = fy.FetchConfig(url="https://youtube.com/@Manarate", out=tmp_path)

    msgs = []
    stats = fy.download_new(cfg, fake_ydl, log=msgs.append)

    assert stats.downloaded == 2
    assert (tmp_path / "Manarate" / "2026" / "06" / "One.mp3").exists()
    # Archive now holds both ids.
    assert fy.load_archive(cfg.archive_path) == {"youtube v1", "youtube v2"}
    # Per-video lines carry an [i/total] counter.
    assert any("[1/2]" in m and "[ok]" in m for m in msgs)
    assert any("[2/2]" in m and "[ok]" in m for m in msgs)


def test_download_new_skips_archived(tmp_path, fake_ydl):
    fake_ydl.entries = [{"ie_key": "Youtube", "id": "v1"}]
    fake_ydl.info_map = {"v1": {"timestamp": 1781706600, "channel": "C"}}
    cfg = fy.FetchConfig(url="u", out=tmp_path)
    fy.append_archive(cfg.archive_path, "youtube v1")

    stats = fy.download_new(cfg, fake_ydl, log=lambda m: None)

    assert stats.downloaded == 0
    assert stats.skipped_archive == 1
    assert fake_ydl.downloaded == []


def test_download_new_backfills_when_file_exists(tmp_path, fake_ydl):
    fake_ydl.entries = [{"ie_key": "Youtube", "id": "v1"}]
    fake_ydl.info_map = {"v1": {"timestamp": 1781706600, "channel": "C", "title": "T"}}
    cfg = fy.FetchConfig(url="u", out=tmp_path)
    # Pre-create the destination file but leave the archive empty.
    dest = fy.dest_path(tmp_path, "C", fake_ydl.info_map["v1"], "mp3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("already here")

    stats = fy.download_new(cfg, fake_ydl, log=lambda m: None)

    assert stats.downloaded == 0
    assert stats.skipped_disk == 1
    assert fake_ydl.downloaded == []
    assert "youtube v1" in fy.load_archive(cfg.archive_path)  # backfilled


def test_download_new_idempotent_second_run(tmp_path, fake_ydl):
    fake_ydl.entries = [{"ie_key": "Youtube", "id": "v1"}]
    fake_ydl.info_map = {"v1": {"timestamp": 1781706600, "channel": "C", "title": "T"}}
    cfg = fy.FetchConfig(url="u", out=tmp_path)

    first = fy.download_new(cfg, fake_ydl, log=lambda m: None)
    fake_ydl.downloaded = []  # reset recorder
    second = fy.download_new(cfg, fake_ydl, log=lambda m: None)

    assert first.downloaded == 1
    assert second.downloaded == 0
    assert second.skipped_archive == 1
    assert fake_ydl.downloaded == []


def test_download_new_respects_max_downloads(tmp_path, fake_ydl):
    fake_ydl.entries = [
        {"ie_key": "Youtube", "id": f"v{i}"} for i in range(5)
    ]
    fake_ydl.info_map = {
        f"v{i}": {"timestamp": 1781706600 + i * 86400, "channel": "C", "title": str(i)}
        for i in range(5)
    }
    cfg = fy.FetchConfig(url="u", out=tmp_path, max_downloads=2)

    stats = fy.download_new(cfg, fake_ydl, log=lambda m: None)

    assert stats.downloaded == 2


def test_download_new_since_filter(tmp_path, fake_ydl):
    fake_ydl.entries = [
        {"ie_key": "Youtube", "id": "old"},
        {"ie_key": "Youtube", "id": "new"},
    ]
    fake_ydl.info_map = {
        "old": {"upload_date": "20250101", "channel": "C", "title": "old"},
        "new": {"upload_date": "20260601", "channel": "C", "title": "new"},
    }
    cfg = fy.FetchConfig(url="u", out=tmp_path, since="20260101")

    stats = fy.download_new(cfg, fake_ydl, log=lambda m: None)

    assert stats.downloaded == 1
    assert stats.skipped_old == 1


def test_download_new_dry_run(tmp_path, fake_ydl):
    fake_ydl.entries = [{"ie_key": "Youtube", "id": "v1"}]
    fake_ydl.info_map = {"v1": {"timestamp": 1781706600, "channel": "C", "title": "T"}}
    cfg = fy.FetchConfig(url="u", out=tmp_path, dry_run=True)

    stats = fy.download_new(cfg, fake_ydl, log=lambda m: None)

    assert stats.downloaded == 0
    assert len(stats.planned) == 1
    assert fake_ydl.downloaded == []
    assert not cfg.archive_path.exists()  # dry run writes nothing


def test_download_new_counts_errors(tmp_path, fake_ydl):
    fake_ydl.entries = [{"ie_key": "Youtube", "id": "bad"}]
    fake_ydl.info_map = {"bad": {"title": "no date fields"}}  # stamp_from_info raises
    cfg = fy.FetchConfig(url="u", out=tmp_path)

    stats = fy.download_new(cfg, fake_ydl, log=lambda m: None)

    assert stats.errors == 1
    assert stats.downloaded == 0


# --------------------------------------------------------------------------- #
# make_logger
# --------------------------------------------------------------------------- #


def test_make_logger_writes_timestamped_file(tmp_path, capsys):
    p = tmp_path / "logs" / "fetch.log"
    emit, close = fy.make_logger(p)
    emit("hello world")
    close()
    # printed to stdout without a timestamp
    assert "hello world" in capsys.readouterr().out
    # written to file with a timestamp prefix
    content = p.read_text(encoding="utf-8")
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} hello world", content)


def test_make_logger_appends(tmp_path):
    p = tmp_path / "fetch.log"
    emit, close = fy.make_logger(p)
    emit("first")
    close()
    emit2, close2 = fy.make_logger(p)
    emit2("second")
    close2()
    content = p.read_text(encoding="utf-8")
    assert "first" in content and "second" in content


def test_make_logger_none_is_noop(capsys):
    emit, close = fy.make_logger(None)
    emit("just stdout")  # must not raise
    close()
    assert "just stdout" in capsys.readouterr().out


def test_list_entries_passes_playlistend():
    captured = {}

    class CapturingYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"entries": []}

    fy.list_entries("https://youtube.com/@c", CapturingYDL, playlistend=7)
    assert captured.get("playlistend") == 7
    assert captured.get("extract_flat") == "in_playlist"


# --------------------------------------------------------------------------- #
# FetchConfig.listing_end
# --------------------------------------------------------------------------- #


def test_listing_end_default():
    assert fy.FetchConfig(url="u").listing_end() == fy.DEFAULT_SCAN_LIMIT


def test_listing_end_explicit_scan_limit():
    assert fy.FetchConfig(url="u", scan_limit=10).listing_end() == 10


def test_listing_end_never_below_max_downloads():
    cfg = fy.FetchConfig(url="u", scan_limit=10, max_downloads=25)
    assert cfg.listing_end() == 25


def test_listing_end_zero_means_unbounded():
    assert fy.FetchConfig(url="u", scan_limit=0).listing_end() is None


# --------------------------------------------------------------------------- #
# CLI parser
# --------------------------------------------------------------------------- #


def test_parser_requires_url():
    with pytest.raises(SystemExit):
        fy.build_parser().parse_args([])


def test_parser_defaults():
    args = fy.build_parser().parse_args(["--url", "u"])
    assert args.out == Path("youtube")
    assert args.audio_format == "mp3"
    assert args.max_downloads is None
    assert args.dry_run is False
    assert args.log is None
    assert args.scan_limit == fy.DEFAULT_SCAN_LIMIT


def test_parser_since_validation():
    with pytest.raises(SystemExit):
        fy.build_parser().parse_args(["--url", "u", "--since", "2026"])


def test_parser_js_runtimes_repeatable():
    args = fy.build_parser().parse_args(
        ["--url", "u", "--js-runtimes", "node", "--js-runtimes", "deno:/x"]
    )
    assert args.js_runtimes == ["node", "deno:/x"]
    assert fy.parse_js_runtimes(args.js_runtimes) == {
        "node": {"path": None},
        "deno": {"path": "/x"},
    }
    args = fy.build_parser().parse_args(
        ["--url", "u", "--out", "/d", "--audio-format", "m4a",
         "--max-downloads", "3", "--since", "20260101", "--dry-run",
         "--log", "/tmp/f.log", "--scan-limit", "12"]
    )
    assert args.out == Path("/d")
    assert args.audio_format == "m4a"
    assert args.max_downloads == 3
    assert args.since == "20260101"
    assert args.dry_run is True
    assert args.log == Path("/tmp/f.log")
    assert args.scan_limit == 12
