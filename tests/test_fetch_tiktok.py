"""Tests for transcription/tools/fetch_tiktok.py.

Pure helpers tested directly; orchestration tested via a fake YoutubeDL —
no network, no yt-dlp, no ffmpeg.
"""

from __future__ import annotations

import sys
import datetime as dt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fetch_tiktok as ft  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers / fake YDL
# --------------------------------------------------------------------------- #


def _entry(vid_id, title=None, timestamp=None, upload_date=None, uploader="acct"):
    """Build a minimal flat-listing entry dict."""
    e = {"id": vid_id, "ie_key": "TikTok", "uploader": uploader}
    if title:
        e["title"] = title
    if timestamp is not None:
        e["timestamp"] = timestamp
    if upload_date is not None:
        e["upload_date"] = upload_date
    return e


class FakeTikTokYDL:
    """Minimal stand-in for yt_dlp.YoutubeDL."""

    entries: list = []
    downloaded: list = []
    produce_file: bool = True

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return {"entries": list(FakeTikTokYDL.entries)}

    def download(self, urls):
        FakeTikTokYDL.downloaded.extend(urls)
        if self.produce_file and "paths" in self.opts:
            d = Path(self.opts["paths"]["home"])
            d.mkdir(parents=True, exist_ok=True)
            stem = self.opts["outtmpl"]["default"].replace("%(ext)s", "mp4")
            (d / stem).write_text("audio")


@pytest.fixture
def fake_ydl():
    FakeTikTokYDL.entries = []
    FakeTikTokYDL.downloaded = []
    FakeTikTokYDL.produce_file = True
    return FakeTikTokYDL


@pytest.fixture(autouse=True)
def mock_ffmpeg(monkeypatch):
    def fake_extract(video_path, audio_path, audio_format):
        audio_path.write_text("mock audio")
    monkeypatch.setattr(ft, "_ffmpeg_extract_audio", fake_extract)


def _cfg(tmp_path, **kw):
    return ft.TikConfig(accounts=["acct"], out=tmp_path, **kw)


# --------------------------------------------------------------------------- #
# normalize_handle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,expected", [
    ("@2mmaroc", "2mmaroc"),
    ("2mmaroc", "2mmaroc"),
    ("@2MMaroc", "2mmaroc"),
    ("handle.with.dots", "handle.with.dots"),
])
def test_normalize_handle(raw, expected):
    assert ft.normalize_handle(raw) == expected


def test_normalize_handle_rejects_garbage():
    with pytest.raises(ValueError):
        ft.normalize_handle("not/valid handle!")


# --------------------------------------------------------------------------- #
# account_url
# --------------------------------------------------------------------------- #


def test_account_url():
    assert ft.account_url("2mmaroc") == "https://www.tiktok.com/@2mmaroc"


# --------------------------------------------------------------------------- #
# stamp_from_entry
# --------------------------------------------------------------------------- #


def test_stamp_from_entry_prefers_timestamp():
    # 2026-06-17 14:30:00 UTC
    e = _entry("v1", timestamp=1781706600)
    assert ft.stamp_from_entry(e) == "20260617143000"


def test_stamp_from_entry_falls_back_to_upload_date():
    e = _entry("v1", upload_date="20260617")
    assert ft.stamp_from_entry(e) == "20260617000000"


def test_stamp_from_entry_raises_without_either():
    with pytest.raises(ValueError):
        ft.stamp_from_entry({"id": "v1", "title": "no date"})


# --------------------------------------------------------------------------- #
# archive_key
# --------------------------------------------------------------------------- #


def test_archive_key():
    assert ft.archive_key(_entry("abc123")) == "tiktok abc123"


def test_archive_key_lowercases_extractor():
    e = _entry("abc"); e["ie_key"] = "TikTok"
    assert ft.archive_key(e) == "tiktok abc"


# --------------------------------------------------------------------------- #
# title_from_entry / tiktok_dest_path
# --------------------------------------------------------------------------- #


def test_title_from_entry_uses_title():
    e = _entry("v1", title="My TikTok", timestamp=1781706600)
    assert ft.title_from_entry(e, "fallback") == "My TikTok"


def test_title_from_entry_fallback():
    e = _entry("v1", timestamp=1781706600)  # no title
    assert ft.title_from_entry(e, "20260617143000") == "20260617143000"


def test_tiktok_dest_path_with_title():
    e = _entry("v1", title="Cool Video", timestamp=1781706600)
    out = ft.tiktok_dest_path(Path("tiktok"), "2mmaroc", e, "mp3")
    assert out == Path("tiktok/2mmaroc/2026/06/Cool Video.mp3")


def test_tiktok_dest_path_fallback_to_stamp():
    e = _entry("v1", timestamp=1781706600)  # no title
    out = ft.tiktok_dest_path(Path("tiktok"), "acct", e, "mp3")
    assert out == Path("tiktok/acct/2026/06/20260617143000.mp3")


# --------------------------------------------------------------------------- #
# entry_url
# --------------------------------------------------------------------------- #


def test_entry_url_from_id_and_uploader():
    e = _entry("12345", uploader="2mmaroc")
    assert ft.entry_url(e) == "https://www.tiktok.com/@2mmaroc/video/12345"


def test_entry_url_fallback_to_webpage_url():
    assert ft.entry_url({"webpage_url": "https://x/y"}) == "https://x/y"


# --------------------------------------------------------------------------- #
# build_ydl_list_opts / build_ydl_audio_opts
# --------------------------------------------------------------------------- #


def test_build_ydl_list_opts_defaults():
    opts = ft.build_ydl_list_opts()
    assert opts["extract_flat"] == "in_playlist"
    assert opts["skip_download"] is True
    assert "playlistend" not in opts
    assert "cookiefile" not in opts


def test_build_ydl_list_opts_with_playlistend_and_cookies(tmp_path):
    cf = tmp_path / "cookies.txt"
    cf.write_text("")
    opts = ft.build_ydl_list_opts(playlistend=10, cookies_file=cf)
    assert opts["playlistend"] == 10
    assert opts["cookiefile"] == str(cf)


def test_build_ydl_video_opts_escapes_percent():
    opts = ft.build_ydl_video_opts(Path("/tmp"), "100% real")
    assert opts["outtmpl"] == {"default": "100%% real.%(ext)s"}


def test_build_ydl_video_opts_format():
    opts = ft.build_ydl_video_opts(Path("/tmp"), "t")
    assert opts["format"] == "play/worst[vcodec^=h264]/worst[vcodec!=none]/bestaudio/best"
    assert "postprocessors" not in opts


# --------------------------------------------------------------------------- #
# TikConfig
# --------------------------------------------------------------------------- #


def test_tikconfig_listing_end_default():
    cfg = ft.TikConfig(accounts=["a"])
    assert cfg.listing_end() == ft.DEFAULT_SCAN_LIMIT


def test_tikconfig_listing_end_never_below_max_downloads():
    cfg = ft.TikConfig(accounts=["a"], scan_limit=10, max_downloads=25)
    assert cfg.listing_end() == 25


def test_tikconfig_listing_end_zero_is_unbounded():
    assert ft.TikConfig(accounts=["a"], scan_limit=0).listing_end() is None


def test_tikconfig_archive_path():
    cfg = ft.TikConfig(accounts=["a"], out=Path("tiktok"))
    assert cfg.archive_path == Path("tiktok/.download-archive.txt")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def test_download_account_downloads_new(tmp_path, fake_ydl):
    fake_ydl.entries = [_entry("v1", title="One", timestamp=1781706600)]
    msgs = []
    stats = ft.download_account("acct", _cfg(tmp_path), fake_ydl, log=msgs.append)
    assert stats.downloaded == 1
    assert (tmp_path / "acct" / "2026" / "06" / "One.mp3").exists()
    assert ft.load_archive(_cfg(tmp_path).archive_path) == {"tiktok v1"}
    assert any("[acct 1/1]" in m and "[ok]" in m for m in msgs)


def test_download_account_skips_archived(tmp_path, fake_ydl):
    fake_ydl.entries = [_entry("v1", title="x", timestamp=1781706600)]
    cfg = _cfg(tmp_path)
    ft.append_archive(cfg.archive_path, "tiktok v1")
    stats = ft.download_account("acct", cfg, fake_ydl, log=lambda m: None)
    assert stats.skipped_archive == 1 and stats.downloaded == 0


def test_download_account_backfills_on_disk(tmp_path, fake_ydl):
    e = _entry("v1", title="T", timestamp=1781706600)
    fake_ydl.entries = [e]
    cfg = _cfg(tmp_path)
    dest = ft.tiktok_dest_path(tmp_path, "acct", e, "mp3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("already")
    stats = ft.download_account("acct", cfg, fake_ydl, log=lambda m: None)
    assert stats.skipped_disk == 1 and stats.downloaded == 0
    assert "tiktok v1" in ft.load_archive(cfg.archive_path)


def test_download_account_idempotent(tmp_path, fake_ydl):
    fake_ydl.entries = [_entry("v1", title="T", timestamp=1781706600)]
    cfg = _cfg(tmp_path)
    first = ft.download_account("acct", cfg, fake_ydl, log=lambda m: None)
    second = ft.download_account("acct", cfg, fake_ydl, log=lambda m: None)
    assert first.downloaded == 1 and second.downloaded == 0
    assert second.skipped_archive == 1


def test_download_account_max_downloads(tmp_path, fake_ydl):
    fake_ydl.entries = [_entry(f"v{i}", title=str(i), timestamp=1781706600 + i)
                        for i in range(5)]
    stats = ft.download_account("acct", _cfg(tmp_path, max_downloads=2),
                                fake_ydl, log=lambda m: None)
    assert stats.downloaded == 2


def test_download_account_since_filter(tmp_path, fake_ydl):
    old = _entry("old", title="old", upload_date="20250101")
    new = _entry("new", title="new", upload_date="20260601")
    fake_ydl.entries = [old, new]
    stats = ft.download_account("acct", _cfg(tmp_path, since="20260101"),
                                fake_ydl, log=lambda m: None)
    assert stats.downloaded == 1 and stats.skipped_old == 1


def test_download_account_dry_run(tmp_path, fake_ydl):
    fake_ydl.entries = [_entry("v1", title="T", timestamp=1781706600)]
    cfg = _cfg(tmp_path, dry_run=True)
    stats = ft.download_account("acct", cfg, fake_ydl, log=lambda m: None)
    assert stats.downloaded == 0 and len(stats.planned) == 1
    assert not cfg.archive_path.exists()


def test_download_account_error_isolation(tmp_path, fake_ydl):
    fake_ydl.entries = [_entry("v1", title="T", timestamp=1781706600)]
    fake_ydl.produce_file = False  # simulate failed download
    FakeTikTokYDL.produce_file = False

    class BrokenYDL(FakeTikTokYDL):
        def download(self, urls):
            raise RuntimeError("network failure")

    stats = ft.download_account("acct", _cfg(tmp_path), BrokenYDL,
                                log=lambda m: None)
    assert stats.errors == 1 and stats.downloaded == 0


def test_download_all_aggregates(tmp_path, fake_ydl):
    cfg = ft.TikConfig(accounts=["a", "b"], out=tmp_path)
    # Give each account a distinct video id so the second isn't archived by the first.
    results = {"a": [_entry("va1", title="A1", timestamp=1781706600)],
               "b": [_entry("vb1", title="B1", timestamp=1781706601)]}
    fake_ydl.entries = results["a"]  # start with a's entries

    def prov_factory(opts):
        # We need the URL to determine which account's entries to use.
        class PerAccountYDL(FakeTikTokYDL):
            def __init__(self, o):
                super().__init__(o)
            def extract_info(self, url, download=False):
                handle = url.split("@")[-1]
                return {"entries": list(results.get(handle, []))}
        return PerAccountYDL(opts)

    stats = ft.download_all(cfg, prov_factory, log=lambda m: None)
    assert stats.downloaded == 2


# --------------------------------------------------------------------------- #
# CLI parser
# --------------------------------------------------------------------------- #


def test_parser_normalises_handles():
    args = ft.build_parser().parse_args(["--account", "@2MMaroc"])
    assert args.account == ["2mmaroc"]


def test_parser_defaults():
    args = ft.build_parser().parse_args([])
    assert args.account is None
    assert args.out == Path("tiktok")
    assert args.audio_format == "mp3"
    assert args.scan_limit == ft.DEFAULT_SCAN_LIMIT
    assert args.dry_run is False
    assert args.cookies_file is None
    assert args.log is None


def test_parser_since_validation():
    with pytest.raises(SystemExit):
        ft.build_parser().parse_args(["--account", "x", "--since", "2026"])


def test_parser_full():
    args = ft.build_parser().parse_args([
        "--account", "@2mmaroc", "--account", "medi1tv",
        "--out", "/d", "--audio-format", "m4a",
        "--max-downloads", "3", "--scan-limit", "20",
        "--since", "20260101", "--dry-run",
        "--cookies-file", "/c.txt", "--log", "/t.log",
    ])
    assert args.account == ["2mmaroc", "medi1tv"]
    assert args.out == Path("/d")
    assert args.audio_format == "m4a"
    assert args.max_downloads == 3
    assert args.scan_limit == 20
    assert args.since == "20260101"
    assert args.dry_run is True
    assert args.cookies_file == Path("/c.txt")
    assert args.log == Path("/t.log")
