#!/usr/bin/env python3
"""
Incrementally download the audio of every *new* video post / reel from one or
more Instagram accounts, using instaloader for listing + downloading.

For each account this tool walks the profile's posts (newest first), keeps only
**videos**, skips anything already grabbed (tracked in a download archive *and*
double-checked on disk), downloads the video to a staging area, and extracts the
audio with ffmpeg into::

    {out}/{account}/{year}/{month}/{title}.{ext}

where ``{year}/{month}`` is the post's **upload month**, ``{title}`` is the
sanitized first line of the caption (falling back to the post shortcode), and
``{ext}`` is the audio codec (default ``mp3``). This mirrors the layout produced
by ``fetch_youtube.py`` so both feed the same transcription pipeline.

Authentication: Instagram blocks/rate-limits almost everything without a login.
Log in once to create a reusable session file::

    python fetch_instagram.py --user YOUR_LOGIN --login          # prompts password (+2FA)

then fetch (the saved session is reused, no prompts)::

    python fetch_instagram.py --user YOUR_LOGIN --account natgeo --account 2m.ma

Dependencies: ``pip install -r transcription/tools/requirements-instagram.txt``
and a system ffmpeg (e.g. ``sudo apt install ffmpeg``).

NOTE: Instagram's Terms of Service restrict automated downloading and rate-limit
aggressively; use this for accounts you own or have the right to archive.
"""

from __future__ import annotations

import argparse
import getpass
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from _media_common import (
    DEFAULT_SCAN_LIMIT,
    append_archive,
    dest_for,
    load_archive,
    make_logger,
    sanitize_filename,
    stamp_from_datetime,
)

# ffmpeg audio codec for a given container/format.
_CODECS = {
    "mp3": "libmp3lame",
    "m4a": "aac",
    "aac": "aac",
    "opus": "libopus",
    "ogg": "libvorbis",
    "wav": "pcm_s16le",
    "flac": "flac",
}


# ----------------------------------------------------------------------------- #
# Pure helpers (no network, no instaloader) — the unit-tested core.
# ----------------------------------------------------------------------------- #


def caption_title(post) -> Optional[str]:
    """First non-empty line of a post's caption, or ``None`` if there is none."""
    cap = getattr(post, "caption", None)
    if not cap:
        return None
    for line in cap.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def stamp_from_post(post) -> str:
    """14-digit ``YYYYMMDDHHMMSS`` stamp from a post's UTC upload time."""
    return stamp_from_datetime(post.date_utc)


def archive_key(post) -> str:
    """Archive line for a post: ``'instagram <shortcode>'``."""
    return f"instagram {post.shortcode}"


def instagram_dest_path(out_root: Path, account: str, post, ext: str) -> Path:
    """Final audio path ``out/{account}/{YYYY}/{MM}/{title}.{ext}`` for a post.

    Title is the sanitized first caption line, falling back to the shortcode so
    every file has a stable, unique-ish name.
    """
    stamp = stamp_from_post(post)
    title = sanitize_filename(caption_title(post), fallback=post.shortcode)
    return dest_for(out_root, account, stamp, title, ext)


def ffmpeg_extract_cmd(src: Path, dest: Path, audio_format: str) -> List[str]:
    """Build the ffmpeg argv that extracts audio from ``src`` into ``dest``."""
    codec = _CODECS.get(audio_format, audio_format)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vn", "-acodec", codec,
    ]
    if audio_format == "mp3":
        cmd += ["-q:a", "0"]  # best VBR
    cmd.append(str(dest))
    return cmd


# ----------------------------------------------------------------------------- #
# Configuration + stats
# ----------------------------------------------------------------------------- #


@dataclass
class InstaConfig:
    """All knobs for one Instagram downloader run."""

    accounts: List[str]
    user: Optional[str] = None  # login account (may differ from targets)
    session_file: Optional[Path] = None
    out: Path = Path("instagram")
    audio_format: str = "mp3"
    max_downloads: Optional[int] = None
    scan_limit: int = DEFAULT_SCAN_LIMIT
    since: Optional[str] = None  # 'YYYYMMDD' inclusive lower bound
    dry_run: bool = False

    @property
    def archive_path(self) -> Path:
        return self.out / ".download-archive.txt"

    @property
    def staging_dir(self) -> Path:
        return self.out / ".staging"

    def default_session_file(self) -> Optional[Path]:
        """Where the session lives if --session-file wasn't given."""
        if self.session_file:
            return self.session_file
        if self.user:
            return self.out / ".sessions" / f"{self.user}.session"
        return None

    def post_limit(self) -> Optional[int]:
        """How many of the newest posts to examine per account (``None`` = all)."""
        if self.scan_limit and self.scan_limit > 0:
            end = self.scan_limit
            if self.max_downloads:
                end = max(end, self.max_downloads)
            return end
        return None


@dataclass
class RunStats:
    downloaded: int = 0
    skipped_archive: int = 0
    skipped_disk: int = 0
    skipped_old: int = 0
    errors: int = 0
    planned: List[Tuple[str, Path]] = field(default_factory=list)

    def add(self, other: "RunStats") -> None:
        self.downloaded += other.downloaded
        self.skipped_archive += other.skipped_archive
        self.skipped_disk += other.skipped_disk
        self.skipped_old += other.skipped_old
        self.errors += other.errors
        self.planned.extend(other.planned)

    def summary(self) -> str:
        return (
            f"downloaded={self.downloaded} | skipped(archive)={self.skipped_archive} | "
            f"skipped(on-disk)={self.skipped_disk} | skipped(too-old)={self.skipped_old} | "
            f"errors={self.errors}"
        )


# ----------------------------------------------------------------------------- #
# instaloader integration (injectable so the orchestration stays testable)
# ----------------------------------------------------------------------------- #


def make_loader(loader_cls: Callable, staging_dir: Path):
    """Construct an Instaloader configured to stage *videos only* into one dir.

    Filenames embed the shortcode so we can locate the produced mp4 afterwards;
    thumbnails, metadata sidecars and caption txt files are all disabled.
    """
    return loader_cls(
        dirname_pattern=str(staging_dir),
        filename_pattern="{date_utc:%Y%m%d_%H%M%S}_{shortcode}",
        download_pictures=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )


def load_or_login(
    loader,
    user: Optional[str],
    session_file: Optional[Path],
    *,
    do_login: bool = False,
    password_prompt: Callable[[str], str] = getpass.getpass,
    twofa_prompt: Optional[Callable[[], str]] = None,
    two_factor_exc: tuple = (),
    log: Callable[[str], None] = print,
):
    """Authenticate ``loader``: load a saved session, or (``--login``) log in.

    On ``--login`` the password is read via ``password_prompt`` (never logged),
    2FA is handled through ``twofa_prompt`` if Instagram requests it, and the
    session is saved for reuse. Otherwise an existing session file is loaded.
    """
    if not user:
        raise SystemExit("error: --user (the Instagram login account) is required")
    sf = str(session_file) if session_file else None

    if do_login:
        password = password_prompt(f"Instagram password for {user}: ")
        try:
            loader.login(user, password)
        except two_factor_exc:
            if twofa_prompt is None:
                raise
            loader.two_factor_login(twofa_prompt())
        loader.save_session_to_file(sf)
        log(f"session saved for {user}")
        return loader

    try:
        loader.load_session_from_file(user, sf)
    except FileNotFoundError:
        raise SystemExit(
            f"error: no saved session for {user!r}. Run once with --login first."
        )
    return loader


def _default_posts_provider(loader, account: str):
    """Yield a profile's posts (newest first) using instaloader."""
    import instaloader

    profile = instaloader.Profile.from_username(loader.context, account)
    return profile.get_posts()


def stage_download(loader, post, staging_dir: Path, target: str = "staging") -> Path:
    """Download one post's video into ``staging_dir`` and return the mp4 path."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    loader.download_post(post, target=target)
    matches = sorted(staging_dir.glob(f"*{post.shortcode}*.mp4"))
    if not matches:
        raise RuntimeError(f"no .mp4 produced for shortcode {post.shortcode}")
    return matches[-1]


def extract_audio(
    src: Path, dest: Path, audio_format: str = "mp3",
    runner: Callable = subprocess.run,
) -> None:
    """Extract audio from a local video file into ``dest`` using ffmpeg."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ffmpeg_extract_cmd(src, dest, audio_format)
    result = runner(cmd, capture_output=True, text=True)
    if getattr(result, "returncode", 1) != 0:
        err = (getattr(result, "stderr", "") or "")[-300:]
        raise RuntimeError(f"ffmpeg failed (rc={result.returncode}): {err.strip()}")


def _cleanup_staging(staging_dir: Path, shortcode: str) -> None:
    """Remove all staged files for a shortcode (mp4 + any strays)."""
    for f in staging_dir.glob(f"*{shortcode}*"):
        try:
            f.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------------- #
# Orchestration
# ----------------------------------------------------------------------------- #


def download_account(
    account: str,
    cfg: InstaConfig,
    loader,
    *,
    posts_provider: Callable = _default_posts_provider,
    extract: Callable = extract_audio,
    log: Callable[[str], None] = print,
) -> RunStats:
    """Download every not-yet-seen video from one account. Returns run stats."""
    stats = RunStats()
    archive_ids = load_archive(cfg.archive_path)
    end = cfg.post_limit()

    log(f"[{account}] listing posts (scanning {end if end is not None else 'all'} most recent)...")
    posts = posts_provider(loader, account)

    examined = 0
    for post in posts:
        if end is not None and examined >= end:
            break
        examined += 1
        pos = f"[{account} {examined}/{end if end is not None else '?'}]"

        if cfg.max_downloads is not None and stats.downloaded >= cfg.max_downloads:
            log(f"[{account}] reached --max-downloads={cfg.max_downloads}, stopping")
            break

        if not getattr(post, "is_video", False):
            continue  # photos / non-video posts are skipped silently

        key = archive_key(post)
        if key in archive_ids:
            stats.skipped_archive += 1
            continue

        try:
            stamp = stamp_from_post(post)
        except Exception as exc:  # noqa: BLE001
            stats.errors += 1
            log(f"  {pos} [error] {getattr(post, 'shortcode', '?')}: {exc}")
            continue

        if cfg.since and stamp[:8] < cfg.since:
            stats.skipped_old += 1
            continue

        dest = instagram_dest_path(cfg.out, account, post, cfg.audio_format)
        if dest.exists():
            append_archive(cfg.archive_path, key)
            archive_ids.add(key)
            stats.skipped_disk += 1
            continue

        title = caption_title(post) or post.shortcode
        if cfg.dry_run:
            stats.planned.append((title, dest))
            log(f"  {pos} [plan] {title} -> {dest}")
            continue

        log(f"  {pos} [..] downloading {title}")
        try:
            mp4 = stage_download(loader, post, cfg.staging_dir)
            extract(mp4, dest, cfg.audio_format)
        except Exception as exc:  # noqa: BLE001
            stats.errors += 1
            log(f"  {pos} [error] {post.shortcode}: {exc}")
            continue
        finally:
            _cleanup_staging(cfg.staging_dir, getattr(post, "shortcode", ""))

        append_archive(cfg.archive_path, key)
        archive_ids.add(key)
        stats.downloaded += 1
        log(f"  {pos} [ok] {dest}")

    return stats


def download_all(
    cfg: InstaConfig,
    loader,
    *,
    posts_provider: Callable = _default_posts_provider,
    extract: Callable = extract_audio,
    log: Callable[[str], None] = print,
) -> RunStats:
    """Run :func:`download_account` for every account, returning combined stats."""
    total = RunStats()
    for account in cfg.accounts:
        try:
            stats = download_account(
                account, cfg, loader,
                posts_provider=posts_provider, extract=extract, log=log,
            )
        except Exception as exc:  # noqa: BLE001 — one bad account shouldn't abort the rest
            total.errors += 1
            log(f"[{account}] [error] {exc}")
            continue
        total.add(stats)
    return total


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #


def _valid_since(value: str) -> str:
    if not re.fullmatch(r"\d{8}", value):
        raise argparse.ArgumentTypeError("--since must be YYYYMMDD, e.g. 20260101")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--account", action="append", default=None, metavar="USERNAME",
        help="Instagram account to download (repeatable).",
    )
    p.add_argument("--user", default=None, help="Your Instagram login username.")
    p.add_argument(
        "--session-file", type=Path, default=None,
        help="Path to the instaloader session file (default: <out>/.sessions/<user>.session).",
    )
    p.add_argument(
        "--login", action="store_true",
        help="Log in interactively (prompts password + 2FA) and save the session.",
    )
    p.add_argument(
        "--out", type=Path, default=Path("instagram"),
        help="Output root directory (default: ./instagram).",
    )
    p.add_argument(
        "--audio-format", default="mp3",
        help="Audio codec to extract to (mp3, m4a, opus, ...). Default: mp3.",
    )
    p.add_argument(
        "--max-downloads", type=int, default=None,
        help="Cap the number of videos downloaded per account this run.",
    )
    p.add_argument(
        "--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT,
        help=(
            f"How many of each account's most-recent posts to examine (default: "
            f"{DEFAULT_SCAN_LIMIT}). Use 0 to scan the whole profile (slow)."
        ),
    )
    p.add_argument(
        "--since", type=_valid_since, default=None,
        help="Only download posts uploaded on/after this date (YYYYMMDD).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="List what would be downloaded; download nothing.",
    )
    p.add_argument(
        "--log", type=Path, default=None, metavar="FILE",
        help="Also append the tool's output (timestamped) to this log file.",
    )
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        import instaloader
    except ImportError:
        print(
            "error: instaloader is not installed.\n"
            "  pip install -r transcription/tools/requirements-instagram.txt",
            file=sys.stderr,
        )
        return 2

    cfg = InstaConfig(
        accounts=args.account or [],
        user=args.user,
        session_file=args.session_file,
        out=args.out,
        audio_format=args.audio_format,
        max_downloads=args.max_downloads,
        scan_limit=args.scan_limit,
        since=args.since,
        dry_run=args.dry_run,
    )

    emit, close_log = make_logger(args.log)

    loader = make_loader(instaloader.Instaloader, cfg.staging_dir)
    try:
        load_or_login(
            loader, cfg.user, cfg.default_session_file(),
            do_login=args.login,
            twofa_prompt=lambda: input("Enter the 2FA code: ").strip(),
            two_factor_exc=(instaloader.TwoFactorAuthRequiredException,),
            log=emit,
        )
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        close_log()
        return 2

    if args.login and not cfg.accounts:
        emit("login complete; no --account given, nothing to download.")
        close_log()
        return 0

    if not cfg.accounts:
        print("error: at least one --account is required", file=sys.stderr)
        close_log()
        return 2

    emit(f"accounts: {', '.join(cfg.accounts)}")
    emit(f"output  : {cfg.out}  (archive: {cfg.archive_path})")
    if cfg.dry_run:
        emit("mode    : DRY RUN (no downloads)")

    try:
        stats = download_all(cfg, loader, log=emit)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        close_log()
        return 1

    emit("\n" + stats.summary())
    close_log()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
