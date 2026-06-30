"""Tests for transcription/tools/fetch_instagram.py.

Pure helpers tested directly; orchestration tested via a fake Instaloader,
fake posts provider, and fake ffmpeg — no network, no instaloader, no ffmpeg.
"""

import datetime as dt
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fetch_instagram as fi  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakePost:
    def __init__(self, shortcode, is_video=True, date=None, caption=None):
        self.shortcode = shortcode
        self.is_video = is_video
        self.date_utc = date or dt.datetime(2026, 6, 17, 14, 30, 0)
        self.caption = caption


class FakeLoader:
    """Mimics the slice of instaloader.Instaloader the tool uses."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.dirname_pattern = kwargs.get("dirname_pattern")
        self.context = object()
        self.logged_in = None
        self.twofa_code = None
        self.session_saved = "UNSET"
        self.loaded = None
        self.downloaded = []
        self.produce_file = True  # set False to simulate a missing mp4

    def login(self, user, password):
        self.logged_in = (user, password)

    def two_factor_login(self, code):
        self.twofa_code = code

    def save_session_to_file(self, filename=None):
        self.session_saved = filename

    def load_session_from_file(self, user, filename=None):
        self.loaded = (user, filename)

    def download_post(self, post, target=None):
        self.downloaded.append(post.shortcode)
        if self.produce_file:
            d = Path(self.dirname_pattern)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"20260617_143000_{post.shortcode}.mp4").write_text("video")
        return True


def fake_extract(src, dest, audio_format="mp3"):
    """Stand-in for extract_audio: just writes the destination file."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_text("audio")


def provider(posts):
    """Build a posts_provider returning the given posts for any account."""
    return lambda loader, account: list(posts)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_caption_title_first_nonempty_line():
    assert fi.caption_title(FakePost("a", caption="  \nHello world\nmore")) == "Hello world"


def test_caption_title_none_when_empty():
    assert fi.caption_title(FakePost("a", caption=None)) is None
    assert fi.caption_title(FakePost("a", caption="   ")) is None


def test_stamp_from_post():
    assert fi.stamp_from_post(FakePost("a")) == "20260617143000"


def test_archive_key():
    assert fi.archive_key(FakePost("XYZ")) == "instagram XYZ"


def test_instagram_dest_path_uses_caption():
    post = FakePost("sc1", caption="My Reel\nline2")
    out = fi.instagram_dest_path(Path("instagram"), "natgeo", post, "mp3")
    assert out == Path("instagram/natgeo/2026/06/My Reel.mp3")


def test_instagram_dest_path_falls_back_to_shortcode():
    post = FakePost("sc1", caption=None)
    out = fi.instagram_dest_path(Path("instagram"), "natgeo", post, "mp3")
    assert out == Path("instagram/natgeo/2026/06/sc1.mp3")


def test_ffmpeg_cmd_mp3_has_quality():
    cmd = fi.ffmpeg_extract_cmd(Path("a.mp4"), Path("b.mp3"), "mp3")
    assert "libmp3lame" in cmd and "-q:a" in cmd
    assert cmd[-1] == "b.mp3" and "-vn" in cmd


def test_ffmpeg_cmd_m4a_no_quality():
    cmd = fi.ffmpeg_extract_cmd(Path("a.mp4"), Path("b.m4a"), "m4a")
    assert "aac" in cmd and "-q:a" not in cmd


def test_extract_audio_success(tmp_path):
    calls = {}

    def runner(cmd, capture_output, text):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stderr="")

    fi.extract_audio(tmp_path / "in.mp4", tmp_path / "out.mp3", "mp3", runner=runner)
    assert calls["cmd"][0] == "ffmpeg"


def test_extract_audio_raises_on_failure(tmp_path):
    def runner(cmd, capture_output, text):
        return types.SimpleNamespace(returncode=1, stderr="boom")

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        fi.extract_audio(tmp_path / "in.mp4", tmp_path / "out.mp3", "mp3", runner=runner)


# --------------------------------------------------------------------------- #
# make_loader
# --------------------------------------------------------------------------- #


def test_make_loader_configures_videos_only(tmp_path):
    loader = fi.make_loader(FakeLoader, tmp_path / ".staging")
    k = loader.kwargs
    assert k["dirname_pattern"] == str(tmp_path / ".staging")
    assert "{shortcode}" in k["filename_pattern"]
    assert k["download_pictures"] is False
    assert k["download_video_thumbnails"] is False
    assert k["save_metadata"] is False


# --------------------------------------------------------------------------- #
# InstaConfig
# --------------------------------------------------------------------------- #


def test_post_limit_default():
    assert fi.InstaConfig(accounts=["a"]).post_limit() == fi.DEFAULT_SCAN_LIMIT


def test_post_limit_never_below_max_downloads():
    cfg = fi.InstaConfig(accounts=["a"], scan_limit=10, max_downloads=25)
    assert cfg.post_limit() == 25


def test_post_limit_zero_unbounded():
    assert fi.InstaConfig(accounts=["a"], scan_limit=0).post_limit() is None


def test_default_session_file():
    cfg = fi.InstaConfig(accounts=["a"], user="me", out=Path("ig"))
    assert cfg.default_session_file() == Path("ig/.sessions/me.session")


def test_default_session_file_explicit():
    cfg = fi.InstaConfig(accounts=["a"], user="me", session_file=Path("/s.session"))
    assert cfg.default_session_file() == Path("/s.session")


# --------------------------------------------------------------------------- #
# load_or_login
# --------------------------------------------------------------------------- #


def test_load_or_login_requires_user():
    with pytest.raises(SystemExit):
        fi.load_or_login(FakeLoader(), None, None)


def test_load_or_login_loads_session():
    loader = FakeLoader()
    fi.load_or_login(loader, "me", Path("/x.session"), log=lambda m: None)
    assert loader.loaded == ("me", "/x.session")


def test_load_or_login_missing_session_errors():
    loader = FakeLoader()
    loader.load_session_from_file = lambda u, f=None: (_ for _ in ()).throw(FileNotFoundError())
    with pytest.raises(SystemExit, match="--login"):
        fi.load_or_login(loader, "me", Path("/x.session"), log=lambda m: None)


def test_load_or_login_does_login_and_saves():
    loader = FakeLoader()
    fi.load_or_login(
        loader, "me", Path("/x.session"), do_login=True,
        password_prompt=lambda prompt: "secret", log=lambda m: None,
    )
    assert loader.logged_in == ("me", "secret")
    assert loader.session_saved == "/x.session"


def test_load_or_login_handles_2fa():
    class Need2FA(Exception):
        pass

    loader = FakeLoader()
    orig_login = loader.login

    def login_raises(u, p):
        orig_login(u, p)
        raise Need2FA()

    loader.login = login_raises
    fi.load_or_login(
        loader, "me", None, do_login=True,
        password_prompt=lambda prompt: "secret",
        twofa_prompt=lambda: "123456",
        two_factor_exc=(Need2FA,), log=lambda m: None,
    )
    assert loader.twofa_code == "123456"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _cfg(tmp_path, **kw):
    return fi.InstaConfig(accounts=["acct"], out=tmp_path, **kw)


def test_download_account_downloads_new_videos(tmp_path):
    loader = fi.make_loader(FakeLoader, (tmp_path / ".staging"))
    posts = [FakePost("v1", caption="One"), FakePost("v2", caption="Two")]
    cfg = _cfg(tmp_path)
    msgs = []

    stats = fi.download_account(
        "acct", cfg, loader,
        posts_provider=provider(posts), extract=fake_extract, log=msgs.append,
    )

    assert stats.downloaded == 2
    assert (tmp_path / "acct" / "2026" / "06" / "One.mp3").exists()
    assert fi.load_archive(cfg.archive_path) == {"instagram v1", "instagram v2"}
    assert any("[acct 1/" in m and "[ok]" in m for m in msgs)
    # staging cleaned up
    assert not list((tmp_path / ".staging").glob("*.mp4"))


def test_download_account_skips_non_video(tmp_path):
    loader = fi.make_loader(FakeLoader, (tmp_path / ".staging"))
    posts = [FakePost("v1", caption="vid"), FakePost("p1", is_video=False, caption="photo")]
    stats = fi.download_account(
        "acct", _cfg(tmp_path), loader,
        posts_provider=provider(posts), extract=fake_extract, log=lambda m: None,
    )
    assert stats.downloaded == 1
    assert loader.downloaded == ["v1"]


def test_download_account_skips_archived(tmp_path):
    loader = fi.make_loader(FakeLoader, (tmp_path / ".staging"))
    cfg = _cfg(tmp_path)
    fi.append_archive(cfg.archive_path, "instagram v1")
    stats = fi.download_account(
        "acct", cfg, loader,
        posts_provider=provider([FakePost("v1", caption="x")]),
        extract=fake_extract, log=lambda m: None,
    )
    assert stats.downloaded == 0 and stats.skipped_archive == 1
    assert loader.downloaded == []


def test_download_account_backfills_on_disk(tmp_path):
    loader = fi.make_loader(FakeLoader, (tmp_path / ".staging"))
    cfg = _cfg(tmp_path)
    post = FakePost("v1", caption="Title")
    dest = fi.instagram_dest_path(tmp_path, "acct", post, "mp3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("already")
    stats = fi.download_account(
        "acct", cfg, loader,
        posts_provider=provider([post]), extract=fake_extract, log=lambda m: None,
    )
    assert stats.downloaded == 0 and stats.skipped_disk == 1
    assert "instagram v1" in fi.load_archive(cfg.archive_path)


def test_download_account_idempotent(tmp_path):
    staging = tmp_path / ".staging"
    posts = [FakePost("v1", caption="One")]
    cfg = _cfg(tmp_path)
    first = fi.download_account("acct", cfg, fi.make_loader(FakeLoader, staging),
                                posts_provider=provider(posts), extract=fake_extract,
                                log=lambda m: None)
    second = fi.download_account("acct", cfg, fi.make_loader(FakeLoader, staging),
                                 posts_provider=provider(posts), extract=fake_extract,
                                 log=lambda m: None)
    assert first.downloaded == 1 and second.downloaded == 0
    assert second.skipped_archive == 1


def test_download_account_max_downloads(tmp_path):
    posts = [FakePost(f"v{i}", caption=str(i)) for i in range(5)]
    stats = fi.download_account(
        "acct", _cfg(tmp_path, max_downloads=2), fi.make_loader(FakeLoader, tmp_path / ".staging"),
        posts_provider=provider(posts), extract=fake_extract, log=lambda m: None,
    )
    assert stats.downloaded == 2


def test_download_account_since_filter(tmp_path):
    old = FakePost("old", caption="old", date=dt.datetime(2025, 1, 1))
    new = FakePost("new", caption="new", date=dt.datetime(2026, 6, 1))
    stats = fi.download_account(
        "acct", _cfg(tmp_path, since="20260101"), fi.make_loader(FakeLoader, tmp_path / ".staging"),
        posts_provider=provider([old, new]), extract=fake_extract, log=lambda m: None,
    )
    assert stats.downloaded == 1 and stats.skipped_old == 1


def test_download_account_dry_run(tmp_path):
    cfg = _cfg(tmp_path, dry_run=True)
    stats = fi.download_account(
        "acct", cfg, fi.make_loader(FakeLoader, tmp_path / ".staging"),
        posts_provider=provider([FakePost("v1", caption="x")]),
        extract=fake_extract, log=lambda m: None,
    )
    assert stats.downloaded == 0 and len(stats.planned) == 1
    assert not cfg.archive_path.exists()


def test_download_account_error_isolation(tmp_path):
    loader = fi.make_loader(FakeLoader, tmp_path / ".staging")
    loader.produce_file = False  # download_post won't create the mp4 -> stage error
    stats = fi.download_account(
        "acct", _cfg(tmp_path), loader,
        posts_provider=provider([FakePost("v1", caption="x")]),
        extract=fake_extract, log=lambda m: None,
    )
    assert stats.errors == 1 and stats.downloaded == 0


def test_download_all_aggregates(tmp_path):
    cfg = fi.InstaConfig(accounts=["a", "b"], out=tmp_path)
    posts = {"a": [FakePost("a1", caption="x")], "b": [FakePost("b1", caption="y")]}
    prov = lambda loader, account: posts[account]
    stats = fi.download_all(cfg, fi.make_loader(FakeLoader, cfg.staging_dir),
                            posts_provider=prov, extract=fake_extract, log=lambda m: None)
    assert stats.downloaded == 2


# --------------------------------------------------------------------------- #
# CLI parser
# --------------------------------------------------------------------------- #


def test_parser_defaults():
    args = fi.build_parser().parse_args([])
    assert args.account is None
    assert args.out == Path("instagram")
    assert args.audio_format == "mp3"
    assert args.scan_limit == fi.DEFAULT_SCAN_LIMIT
    assert args.login is False


def test_parser_repeatable_accounts():
    args = fi.build_parser().parse_args(["--account", "a", "--account", "b"])
    assert args.account == ["a", "b"]


def test_parser_since_validation():
    with pytest.raises(SystemExit):
        fi.build_parser().parse_args(["--since", "2026"])


def test_parser_full():
    args = fi.build_parser().parse_args(
        ["--account", "natgeo", "--user", "me", "--login",
         "--out", "/d", "--audio-format", "m4a", "--max-downloads", "3",
         "--scan-limit", "20", "--since", "20260101", "--dry-run",
         "--log", "/tmp/ig.log", "--session-file", "/s.session"]
    )
    assert args.account == ["natgeo"] and args.user == "me" and args.login is True
    assert args.out == Path("/d") and args.audio_format == "m4a"
    assert args.max_downloads == 3 and args.scan_limit == 20
    assert args.since == "20260101" and args.dry_run is True
    assert args.log == Path("/tmp/ig.log") and args.session_file == Path("/s.session")
