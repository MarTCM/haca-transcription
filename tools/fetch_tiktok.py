#!/usr/bin/env python3
"""
Incrementally download the audio of every *new* video from one or more TikTok
accounts, using yt-dlp (no separate library required beyond the YouTube tool).

Given one or more account handles, the tool lists each profile's videos (newest
first, bounded by ``--scan-limit``), skips anything already downloaded (tracked
in a yt-dlp-style download archive *and* double-checked on disk), and downloads
the audio of the rest. Files land in::

    {out}/{account}/{year}/{month}/{title}.{ext}

where ``{account}`` is the TikTok handle (``@`` stripped), ``{year}/{month}`` is
the video's **upload month** in UTC, ``{title}`` is the sanitized video
description (first 150 chars), and ``{ext}`` is the audio codec (default
``mp3``). This mirrors the layout of ``fetch_youtube.py`` and
``fetch_instagram.py`` so all three feed the same transcription pipeline.

No login is required for public accounts. For private or geo-restricted accounts,
pass ``--cookies-file`` (a Netscape-format cookies file exported from a browser).

Usage::

    # all new videos from one account since last run
    python fetch_tiktok.py --account @2mmaroc

    # multiple accounts, bounded first run
    python fetch_tiktok.py --account @2mmaroc --account @medi1tv \\
        --max-downloads 10 --since 20260101

    # preview without downloading
    python fetch_tiktok.py --account @2mmaroc --dry-run

Dependencies: ``pip install -r transcription/tools/requirements-tiktok.txt``
and a system ffmpeg (e.g. ``sudo apt install ffmpeg``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
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
    slugify_channel,
    stamp_from_datetime,
)

# ----------------------------------------------------------------------------- #
# Pure helpers (no network, no yt-dlp) — unit-tested core.
# ----------------------------------------------------------------------------- #

# TikTok @handle pattern — used to normalise user input.
_HANDLE_RE = re.compile(r"^@?(?P<handle>[\w.\-]+)$")


def normalize_handle(raw: str) -> str:
    """Strip a leading ``@`` and lowercase the handle.

    ``'@2MMaroc'`` → ``'2mmaroc'``. Raises :class:`ValueError` on garbage input.
    """
    m = _HANDLE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"invalid TikTok handle: {raw!r}")
    return m.group("handle").lower()


def account_url(handle: str) -> str:
    """Build the canonical TikTok user URL for *yt-dlp*'s ``tiktok:user``
    extractor.  ``handle`` must already be normalised (no ``@``).
    """
    return f"https://www.tiktok.com/@{handle}"


def stamp_from_entry(entry: dict) -> str:
    """14-digit ``YYYYMMDDHHMMSS`` stamp from a yt-dlp TikTok entry.

    Prefers the ``timestamp`` field (Unix epoch, UTC); falls back to
    ``upload_date`` (``YYYYMMDD``) padded with ``000000``; raises
    :class:`ValueError` if neither is usable.
    """
    ts = entry.get("timestamp")
    if ts is not None:
        return stamp_from_datetime(
            dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
        )
    upload_date = entry.get("upload_date")
    if upload_date and re.fullmatch(r"\d{8}", str(upload_date)):
        return f"{upload_date}000000"
    raise ValueError(
        f"entry {entry.get('id')!r} has neither 'timestamp' nor 'upload_date'"
    )


def archive_key(entry: dict) -> str:
    """yt-dlp-compatible archive key: ``'tiktok <video_id>'``."""
    extractor = (
        entry.get("ie_key") or entry.get("extractor_key") or entry.get("extractor")
        or "tiktok"
    )
    return f"{str(extractor).lower()} {entry.get('id')}"


def title_from_entry(entry: dict, fallback_stamp: str) -> str:
    """Sanitized video title (description), falling back to the stamp."""
    raw = entry.get("title") or entry.get("description")
    return sanitize_filename(raw, max_len=150, fallback=fallback_stamp)


def tiktok_dest_path(out_root: Path, account: str, entry: dict, ext: str) -> Path:
    """Final path ``out/{account}/{year}/{month}/{title}.{ext}`` for a TikTok video."""
    stamp = stamp_from_entry(entry)
    title = title_from_entry(entry, stamp)
    return dest_for(out_root, account, stamp, title, ext)


def build_ydl_list_opts(
    playlistend: Optional[int] = None,
    cookies_file: Optional[Path] = None,
) -> dict:
    """yt-dlp options for listing a TikTok user's videos (flat, no download)."""
    opts: dict = {
        "quiet": True,
        "noprogress": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    if playlistend is not None:
        opts["playlistend"] = playlistend
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    return opts


def build_ydl_video_opts(
    dest_dir: Path,
    filename_stem: str,
    cookies_file: Optional[Path] = None,
) -> dict:
    """yt-dlp options for downloading one TikTok video (no postprocessors).

    We download the raw video file and handle audio extraction separately
    with ffmpeg, because TikTok often serves HEVC streams without an audio
    track when browser impersonation (``curl_cffi``) is unavailable.  The
    built-in ``FFmpegExtractAudio`` postprocessor crashes on those files.

    ``paths.home`` is set literally (not run through outtmpl expansion) so exotic
    account names cannot break the template.  Any ``%`` in the title stem is
    escaped to ``%%``.
    """
    opts: dict = {
        "quiet": True,
        "noprogress": True,
        "paths": {"home": str(dest_dir)},
        "outtmpl": {"default": f"{filename_stem.replace('%', '%%')}.%(ext)s"},
        "format": "play/worst[vcodec^=h264]/worst[vcodec!=none]/bestaudio/best",
        # No postprocessors — we run ffmpeg ourselves after download.
    }
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    return opts


def _ffmpeg_extract_audio(video_path: Path, audio_path: Path, audio_format: str) -> None:
    """Extract audio from *video_path* into *audio_path* using ffmpeg.

    Raises :class:`RuntimeError` if the file contains no audio stream or if
    ffmpeg fails for another reason.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    cmd = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(video_path),
        "-vn",                  # drop video
        "-acodec", _ffmpeg_codec(audio_format),
        "-q:a", "0",            # best quality
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr or ""
        if "does not contain any stream" in stderr or "Output file is empty" in stderr:
            raise RuntimeError(
                f"video has no audio stream (TikTok may require curl_cffi "
                f"for proper impersonation — pip install curl_cffi)"
            )
        raise RuntimeError(f"ffmpeg failed (rc={result.returncode}): {stderr[-300:]}")


def _ffmpeg_codec(audio_format: str) -> str:
    """Map user-facing audio format name to the ffmpeg encoder name."""
    mapping = {
        "mp3": "libmp3lame",
        "m4a": "aac",
        "aac": "aac",
        "opus": "libopus",
        "vorbis": "libvorbis",
        "ogg": "libvorbis",
        "flac": "flac",
        "wav": "pcm_s16le",
    }
    return mapping.get(audio_format.lower(), audio_format)


# ----------------------------------------------------------------------------- #
# Configuration + run statistics
# ----------------------------------------------------------------------------- #


@dataclass
class TikConfig:
    """All knobs for one TikTok downloader run."""

    accounts: List[str]                    # normalised handles (no @)
    out: Path = Path("tiktok")
    audio_format: str = "mp3"
    max_downloads: Optional[int] = None
    scan_limit: int = DEFAULT_SCAN_LIMIT
    since: Optional[str] = None            # 'YYYYMMDD' lower bound on upload date
    dry_run: bool = False
    cookies_file: Optional[Path] = None

    @property
    def archive_path(self) -> Path:
        return self.out / ".download-archive.txt"

    def listing_end(self) -> Optional[int]:
        """Bound the per-account listing (never below ``--max-downloads``).

        Returns ``None`` when ``scan_limit == 0`` (unbounded — may be slow on
        prolific creators).
        """
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
# Orchestration (injectable ydl_factory for testability)
# ----------------------------------------------------------------------------- #


def list_entries(
    handle: str,
    ydl_factory: Callable,
    playlistend: Optional[int] = None,
    cookies_file: Optional[Path] = None,
) -> List[dict]:
    """Return the account's newest videos as flat entries (cheap, no download).

    TikTok's ``tiktok:user`` extractor already returns entries newest-first
    (``type=1`` in the API query), so no re-sorting is needed.
    """
    opts = build_ydl_list_opts(playlistend=playlistend, cookies_file=cookies_file)
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(account_url(handle), download=False)
    return list(info.get("entries") or [])


def download_audio(
    video_url: str,
    dest: Path,
    audio_format: str,
    ydl_factory: Callable,
    cookies_file: Optional[Path] = None,
) -> None:
    """Download a TikTok video then extract its audio with ffmpeg.

    Two-step process that avoids yt-dlp's ``FFmpegExtractAudio`` postprocessor
    which crashes when TikTok serves HEVC streams without an audio track.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    ext = audio_format.lstrip(".")
    stem = dest.name[: -(len(ext) + 1)]
    opts = build_ydl_video_opts(dest.parent, stem, cookies_file)

    # Step 1: download raw video
    with ydl_factory(opts) as ydl:
        ydl.download([video_url])

    # Step 2: find the downloaded video file (yt-dlp picks the extension)
    video_file = _find_downloaded_video(dest.parent, stem)
    if video_file is None:
        raise RuntimeError(f"yt-dlp did not produce a file matching {stem!r}")

    # Step 3: extract audio with ffmpeg
    try:
        _ffmpeg_extract_audio(video_file, dest, audio_format)
    finally:
        # Clean up the intermediate video file
        if video_file.exists() and video_file != dest:
            video_file.unlink(missing_ok=True)


def _find_downloaded_video(directory: Path, stem: str) -> Optional[Path]:
    """Locate the video file yt-dlp just wrote (extension varies)."""
    for candidate in directory.iterdir():
        if candidate.is_file() and candidate.stem == stem:
            return candidate
    # Fallback: stem matching may fail with yt-dlp's % escaping
    for candidate in directory.iterdir():
        if candidate.is_file() and candidate.name.startswith(stem[:20]):
            return candidate
    return None


def entry_url(entry: dict) -> str:
    """Resolve a flat entry to a watch URL."""
    vid = entry.get("id")
    uploader = entry.get("uploader") or entry.get("channel_id") or "user"
    if vid:
        return f"https://www.tiktok.com/@{uploader}/video/{vid}"
    return entry.get("url") or entry.get("webpage_url") or ""


def download_account(
    handle: str,
    cfg: TikConfig,
    ydl_factory: Callable,
    *,
    log: Callable[[str], None] = print,
) -> RunStats:
    """Download every not-yet-seen video from one TikTok account.

    Unlike the YouTube tool there is no separate Phase-2 metadata fetch: the
    flat listing for TikTok already includes ``timestamp``, ``title``, and
    ``uploader``, so we build the destination path directly from the flat entry.
    This makes the per-video cost a single yt-dlp call (the download) rather
    than two.
    """
    stats = RunStats()
    archive_ids = load_archive(cfg.archive_path)
    end = cfg.listing_end()

    log(f"[{handle}] listing videos "
        f"(scanning {end if end is not None else 'all'} most recent)...")
    entries = list_entries(handle, ydl_factory, playlistend=end,
                           cookies_file=cfg.cookies_file)
    total = len(entries)
    log(f"[{handle}] examining {total} video(s)")

    # The denominator for the progress counter: --max-downloads when given,
    # otherwise the total number of entries being examined.
    progress_total = cfg.max_downloads if cfg.max_downloads is not None else total

    for idx, entry in enumerate(entries, start=1):

        if cfg.max_downloads is not None and stats.downloaded >= cfg.max_downloads:
            log(f"[{handle}] reached --max-downloads={cfg.max_downloads}, stopping")
            break

        key = archive_key(entry)
        if key in archive_ids:
            stats.skipped_archive += 1
            continue

        try:
            stamp = stamp_from_entry(entry)
        except Exception as exc:  # noqa: BLE001
            stats.errors += 1
            pos = f"[{handle} {stats.downloaded + stats.errors}/{progress_total}]"
            log(f"  {pos} [error] {entry.get('id')}: {exc}")
            continue

        if cfg.since and stamp[:8] < cfg.since:
            stats.skipped_old += 1
            continue

        dest = tiktok_dest_path(cfg.out, handle, entry, cfg.audio_format)

        if dest.exists():
            append_archive(cfg.archive_path, key)
            archive_ids.add(key)
            stats.skipped_disk += 1
            continue

        title = title_from_entry(entry, stamp)
        pos = f"[{handle} {stats.downloaded + 1}/{progress_total}]"
        if cfg.dry_run:
            stats.planned.append((title, dest))
            log(f"  {pos} [plan] {title} -> {dest}")
            continue

        log(f"  {pos} [..] downloading {title}")
        try:
            download_audio(
                entry_url(entry), dest, cfg.audio_format, ydl_factory,
                cfg.cookies_file,
            )
        except Exception as exc:  # noqa: BLE001
            stats.errors += 1
            log(f"  {pos} [error] downloading {entry.get('id')}: {exc}")
            continue

        append_archive(cfg.archive_path, key)
        archive_ids.add(key)
        stats.downloaded += 1
        log(f"  {pos} [ok] {dest}")

    return stats


def download_all(
    cfg: TikConfig,
    ydl_factory: Callable,
    *,
    log: Callable[[str], None] = print,
) -> RunStats:
    """Run :func:`download_account` for every account; return combined stats."""
    total = RunStats()
    for handle in cfg.accounts:
        try:
            stats = download_account(handle, cfg, ydl_factory, log=log)
        except Exception as exc:  # noqa: BLE001
            total.errors += 1
            log(f"[{handle}] [error] {exc}")
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


def _valid_handle(value: str) -> str:
    """argparse type: accept and normalise a TikTok handle."""
    try:
        return normalize_handle(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--account",
        action="append",
        default=None,
        type=_valid_handle,
        metavar="HANDLE",
        help="TikTok account handle (with or without @). Repeatable.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("tiktok"),
        help="Output root directory (default: ./tiktok).",
    )
    p.add_argument(
        "--audio-format",
        default="mp3",
        help="Audio codec to extract to (mp3, m4a, opus, …). Default: mp3.",
    )
    p.add_argument(
        "--max-downloads",
        type=int,
        default=None,
        help="Cap the number of videos downloaded per account this run.",
    )
    p.add_argument(
        "--scan-limit",
        type=int,
        default=DEFAULT_SCAN_LIMIT,
        help=(
            f"How many of each account's most-recent videos to examine "
            f"(default: {DEFAULT_SCAN_LIMIT}). Use 0 to scan the whole profile "
            f"(slow on prolific creators)."
        ),
    )
    p.add_argument(
        "--since",
        type=_valid_since,
        default=None,
        help="Only download videos uploaded on/after this date (YYYYMMDD).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be downloaded; download nothing.",
    )
    p.add_argument(
        "--cookies-file",
        type=Path,
        default=None,
        metavar="FILE",
        help="Netscape-format cookies file for private/geo-restricted accounts.",
    )
    p.add_argument(
        "--log",
        type=Path,
        default=None,
        metavar="FILE",
        help="Also append the tool's output (timestamped) to this log file.",
    )
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        print(
            "error: yt-dlp is not installed.\n"
            "  pip install -r transcription/tools/requirements-tiktok.txt",
            file=sys.stderr,
        )
        return 2

    if not args.account:
        print("error: at least one --account is required", file=sys.stderr)
        return 2

    cfg = TikConfig(
        accounts=args.account,
        out=args.out,
        audio_format=args.audio_format,
        max_downloads=args.max_downloads,
        scan_limit=args.scan_limit,
        since=args.since,
        dry_run=args.dry_run,
        cookies_file=args.cookies_file,
    )

    def ydl_factory(opts: dict):
        return YoutubeDL(opts)

    emit, close_log = make_logger(args.log)
    emit(f"accounts: {', '.join('@' + h for h in cfg.accounts)}")
    emit(f"output  : {cfg.out}  (archive: {cfg.archive_path})")
    if cfg.dry_run:
        emit("mode    : DRY RUN (no downloads)")

    try:
        stats = download_all(cfg, ydl_factory, log=emit)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        close_log()
        return 1

    emit("\n" + stats.summary())
    close_log()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
