#!/usr/bin/env python3
"""
Incrementally download the audio of every *new* video from a YouTube channel.

Given a channel URL, this tool lists the channel's uploads, skips anything it has
already grabbed (tracked in a yt-dlp-style download archive *and* double-checked
against files already on disk), and downloads the audio of the rest. Files land
in::

    {out}/{channel}/{year}/{month}/{video_title}.{ext}

where ``{channel}`` is derived from the channel *title*, ``{year}/{month}`` is
the video's **upload month** (not the download date), ``{video_title}`` is the
sanitized video title, and ``{ext}`` is the audio codec. Audio is extracted with
ffmpeg, so ffmpeg must be installed and on ``PATH``.

Usage::

    # download every video uploaded since the last run
    python fetch_youtube.py --url https://www.youtube.com/@SomeChannel

    # first run, but only the 5 most recent and only from 2026 onwards
    python fetch_youtube.py --url https://www.youtube.com/@SomeChannel \
        --max-downloads 5 --since 20260101

    # see what *would* be downloaded without touching the network/disk much
    python fetch_youtube.py --url https://www.youtube.com/@SomeChannel --dry-run

Dependencies: ``pip install -r transcription/tools/requirements-youtube.txt`` and
a system ffmpeg (e.g. ``sudo apt install ffmpeg``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

# ----------------------------------------------------------------------------- #
# Pure helpers (no network, no yt-dlp) — these are the unit-tested core.
# ----------------------------------------------------------------------------- #

# Characters that are illegal in file/folder names on common filesystems
# (Windows is the strictest), plus ASCII control characters. We keep everything
# else, including non-Latin letters, so an Arabic channel title stays readable.
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# A bare channel URL whose path is just the channel identifier (no /videos,
# /streams, /playlists tab). We append /videos to these so yt-dlp returns the
# uploads list rather than the channel's multi-tab home page.
_CHANNEL_ROOT = re.compile(
    r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)$", re.IGNORECASE
)

# How many of a channel's most-recent uploads to examine per run by default.
# Bounds the listing so huge channels don't take forever before --max-downloads
# applies. Override with --scan-limit (0 = no bound).
DEFAULT_SCAN_LIMIT = 50


def slugify_channel(title: Optional[str], fallback: str = "unknown-channel") -> str:
    """Turn a channel title into a safe single-path-segment folder name.

    Strips characters that are illegal in filenames, collapses runs of
    whitespace to a single space, and trims leading/trailing spaces and dots
    (trailing dots/spaces are invalid on Windows). Returns ``fallback`` when the
    title is missing or reduces to nothing.
    """
    if not title:
        return fallback
    cleaned = _ILLEGAL_FS.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(". ")
    return cleaned or fallback


def stamp_from_info(info: dict) -> str:
    """Return the upload time as a 14-digit ``YYYYMMDDHHMMSS`` string.

    Prefers the ``timestamp`` field (a Unix epoch in UTC that yt-dlp fills in
    when YouTube exposes it), which gives full time-of-day precision. Falls back
    to the date-only ``upload_date`` (``YYYYMMDD``) padded with ``000000``.
    Raises :class:`ValueError` if neither field is usable.
    """
    ts = info.get("timestamp")
    if ts is not None:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime(
            "%Y%m%d%H%M%S"
        )
    upload_date = info.get("upload_date")
    if upload_date and re.fullmatch(r"\d{8}", str(upload_date)):
        return f"{upload_date}000000"
    raise ValueError(
        "info dict has neither a usable 'timestamp' nor an 8-digit 'upload_date'"
    )


def sanitize_filename(name: Optional[str], max_len: int = 150, fallback: str = "video") -> str:
    """Turn a video title into a safe filename stem (no extension).

    Same illegal-character / whitespace rules as :func:`slugify_channel`, plus a
    length cap so we stay well under the 255-byte filesystem limit. Non-Latin
    letters and emoji are preserved. Returns ``fallback`` if the title is missing
    or sanitises to nothing.
    """
    if not name:
        return fallback
    cleaned = _ILLEGAL_FS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(". ")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(". ")
    return cleaned or fallback


def dest_path(out_root: Path, channel: str, info: dict, ext: str) -> Path:
    """Build the final output path ``out/{channel}/{year}/{month}/{title}.{ext}``.

    ``year`` and ``month`` come from the upload date; the filename is the
    sanitized video title (falling back to the timestamp stamp if there is no
    title).
    """
    stamp = stamp_from_info(info)
    year, month = stamp[:4], stamp[4:6]
    title = sanitize_filename(info.get("title"), fallback=stamp)
    return Path(out_root) / channel / year / month / f"{title}.{ext.lstrip('.')}"


def normalize_channel_url(url: str) -> str:
    """Append ``/videos`` to a bare channel URL so we get the uploads list.

    URLs that already point at a tab, playlist or single video are returned
    unchanged.
    """
    trimmed = url.rstrip("/")
    if _CHANNEL_ROOT.search(trimmed):
        return trimmed + "/videos"
    return url


def archive_key(entry: dict) -> str:
    """yt-dlp-compatible archive line for an entry: ``'<extractor> <id>'``.

    Mirrors yt-dlp's own ``--download-archive`` format (lowercased extractor key
    + space + video id) so the archive file we manage stays interchangeable with
    yt-dlp's.
    """
    extractor = (
        entry.get("ie_key") or entry.get("extractor_key") or entry.get("extractor") or "youtube"
    )
    return f"{str(extractor).lower()} {entry.get('id')}"


def entry_url(entry: dict) -> str:
    """Resolve a flat-playlist entry to a watch URL we can hand back to yt-dlp."""
    vid = entry.get("id")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    # Fall back to whatever URL the flat extractor gave us.
    return entry.get("url") or ""


def load_archive(archive_path: Path) -> set:
    """Read the download archive into a set of ``'<extractor> <id>'`` lines."""
    if not archive_path.exists():
        return set()
    lines = archive_path.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip()}


def append_archive(archive_path: Path, key: str) -> None:
    """Append one archive key, creating the file/parent dir if needed."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as fh:
        fh.write(key + "\n")


def parse_js_runtimes(values: Optional[Iterable[str]]) -> Optional[dict]:
    """Convert CLI ``RUNTIME[:PATH]`` strings into yt-dlp's ``js_runtimes`` dict.

    ``['node']`` -> ``{'node': {'path': None}}``;
    ``['deno:/usr/bin/deno']`` -> ``{'deno': {'path': '/usr/bin/deno'}}``.
    Returns ``None`` for empty input so yt-dlp keeps its built-in default
    (``deno``). Mirrors yt-dlp's own CLI parsing.
    """
    values = list(values or [])
    if not values:
        return None
    return {
        runtime.lower(): {"path": path}
        for runtime, path in ([*arg.split(":", 1), None][:2] for arg in values)
    }


def build_ydl_opts(
    dest_dir: Path, filename_stem: str, audio_format: str, js_runtimes: Optional[dict] = None
) -> dict:
    """yt-dlp options for downloading one video's audio to ``dest_dir/stem.ext``.

    ``paths.home`` sets the output directory *literally* (it is not run through
    template expansion, so an exotic channel-title folder name can't break the
    template), while ``outtmpl`` is the title-based filename. Any literal ``%``
    in the title is escaped to ``%%`` so it isn't mistaken for a template field.
    The ``FFmpegExtractAudio`` postprocessor re-encodes the bestaudio stream to
    the requested codec. ``js_runtimes``, when given, selects the JavaScript
    runtime yt-dlp uses for YouTube extraction.
    """
    opts = {
        "quiet": True,
        "noprogress": True,
        "paths": {"home": str(dest_dir)},
        "outtmpl": {"default": f"{filename_stem.replace('%', '%%')}.%(ext)s"},
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",  # best VBR for lossy codecs
            }
        ],
    }
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    return opts


# ----------------------------------------------------------------------------- #
# Configuration + run statistics
# ----------------------------------------------------------------------------- #


@dataclass
class FetchConfig:
    """All knobs for one downloader run."""

    url: str
    out: Path = Path("youtube")
    audio_format: str = "mp3"
    max_downloads: Optional[int] = None
    since: Optional[str] = None  # 'YYYYMMDD' inclusive lower bound on upload date
    dry_run: bool = False
    js_runtimes: Optional[dict] = None  # yt-dlp js_runtimes dict, or None for default
    scan_limit: int = DEFAULT_SCAN_LIMIT  # how many recent uploads to examine; 0 = all

    @property
    def archive_path(self) -> Path:
        """Where the dedup archive lives (hidden file at the output root)."""
        return self.out / ".download-archive.txt"

    def listing_end(self) -> Optional[int]:
        """How many of the newest uploads to fetch from the channel (``None`` = all).

        Bounds the (potentially enormous) channel listing so we don't page through
        every upload before ``--max-downloads`` can take effect. ``scan_limit`` is
        the window; it is never allowed to be smaller than ``max_downloads`` so a
        large explicit cap still works. ``scan_limit == 0`` means "no bound".
        """
        if self.scan_limit and self.scan_limit > 0:
            end = self.scan_limit
            if self.max_downloads:
                end = max(end, self.max_downloads)
            return end
        return None  # unbounded (may be very slow on large channels)


@dataclass
class RunStats:
    """Tally of what happened, for the end-of-run summary."""

    downloaded: int = 0
    skipped_archive: int = 0
    skipped_disk: int = 0
    skipped_old: int = 0
    errors: int = 0
    planned: List[Tuple[str, Path]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"downloaded={self.downloaded} | skipped(archive)={self.skipped_archive} | "
            f"skipped(on-disk)={self.skipped_disk} | skipped(too-old)={self.skipped_old} | "
            f"errors={self.errors}"
        )


# ----------------------------------------------------------------------------- #
# Orchestration (talks to yt-dlp via an injectable factory so it stays testable)
# ----------------------------------------------------------------------------- #


def list_entries(
    channel_url: str,
    ydl_factory: Callable,
    js_runtimes: Optional[dict] = None,
    playlistend: Optional[int] = None,
) -> List[dict]:
    """Return the channel's newest uploads as flat entries (id/title only, fast).

    ``playlistend`` bounds how many of the most-recent uploads yt-dlp fetches, so
    we don't page through an entire (possibly huge) channel before filtering.
    """
    opts = {
        "quiet": True,
        "noprogress": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    if playlistend is not None:
        opts["playlistend"] = playlistend
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(normalize_channel_url(channel_url), download=False)
    return list(info.get("entries") or [])


def fetch_info(video_url: str, ydl_factory: Callable, js_runtimes: Optional[dict] = None) -> dict:
    """Full metadata extraction for a single video (no download)."""
    opts = {"quiet": True, "noprogress": True, "skip_download": True}
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    with ydl_factory(opts) as ydl:
        return ydl.extract_info(video_url, download=False)


def download_audio(
    video_url: str,
    dest: Path,
    audio_format: str,
    ydl_factory: Callable,
    js_runtimes: Optional[dict] = None,
) -> None:
    """Download + extract audio for one video into ``dest`` (a full file path)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ext = audio_format.lstrip(".")
    stem = dest.name[: -(len(ext) + 1)]  # strip the trailing ".{ext}"
    opts = build_ydl_opts(dest.parent, stem, audio_format, js_runtimes)
    with ydl_factory(opts) as ydl:
        ydl.download([video_url])


def download_new(
    cfg: FetchConfig,
    ydl_factory: Callable,
    *,
    log: Callable[[str], None] = print,
) -> RunStats:
    """Download every not-yet-seen video from the channel. Returns run stats.

    The loop is the heart of the tool:

    1. List the channel's uploads (flat/fast).
    2. Skip ids already in the download archive (the source of truth).
    3. Pull full metadata to learn the upload time + channel title.
    4. Apply the optional ``--since`` lower bound.
    5. If the target file already exists on disk, backfill the archive and skip
       (the filesystem safety net).
    6. Otherwise download the audio and record the id in the archive.
    """
    stats = RunStats()
    archive_ids = load_archive(cfg.archive_path)

    end = cfg.listing_end()
    log(f"listing uploads (scanning {end if end is not None else 'all'} most recent)...")
    entries = list_entries(cfg.url, ydl_factory, cfg.js_runtimes, end)
    total = len(entries)
    log(f"examining {total} upload(s)")
    for idx, entry in enumerate(entries, start=1):
        pos = f"[{idx}/{total}]"
        if cfg.max_downloads is not None and stats.downloaded >= cfg.max_downloads:
            log(f"reached --max-downloads={cfg.max_downloads}, stopping")
            break

        key = archive_key(entry)
        if key in archive_ids:
            stats.skipped_archive += 1
            continue

        try:
            info = fetch_info(entry_url(entry), ydl_factory, cfg.js_runtimes)
            stamp = stamp_from_info(info)
        except Exception as exc:  # noqa: BLE001 — keep going on a single bad video
            stats.errors += 1
            log(f"  {pos} [error] {entry.get('id')}: {exc}")
            continue

        if cfg.since and stamp[:8] < cfg.since:
            stats.skipped_old += 1
            continue

        channel = slugify_channel(info.get("channel") or info.get("uploader"))
        dest = dest_path(cfg.out, channel, info, cfg.audio_format)

        if dest.exists():
            # Already have the file but the archive didn't know — backfill it.
            append_archive(cfg.archive_path, key)
            archive_ids.add(key)
            stats.skipped_disk += 1
            continue

        if cfg.dry_run:
            title = info.get("title") or entry.get("id")
            stats.planned.append((title, dest))
            log(f"  {pos} [plan] {title} -> {dest}")
            continue

        log(f"  {pos} [..] downloading {info.get('title') or entry.get('id')}")
        try:
            download_audio(
                entry_url(entry), dest, cfg.audio_format, ydl_factory, cfg.js_runtimes
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


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #


def _valid_since(value: str) -> str:
    """argparse type: accept only an 8-digit YYYYMMDD date."""
    if not re.fullmatch(r"\d{8}", value):
        raise argparse.ArgumentTypeError("--since must be YYYYMMDD, e.g. 20260101")
    return value


def make_logger(log_path: Optional[Path]):
    """Return ``(emit, close)``: ``emit(msg)`` prints to stdout and, when a log
    path is given, also appends a timestamped copy to that file.

    The console stays clean (no timestamps); the file lines are prefixed with an
    ISO-ish ``YYYY-MM-DD HH:MM:SS`` for later auditing. ``close()`` closes the
    file handle (a no-op when no log file is used). The log directory is created
    if needed.
    """
    fh = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = log_path.open("a", encoding="utf-8")

    def emit(msg: str) -> None:
        print(msg)
        if fh is not None:
            ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"{ts} {msg}\n")
            fh.flush()

    def close() -> None:
        if fh is not None:
            fh.close()

    return emit, close


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", required=True, help="YouTube channel URL.")
    p.add_argument(
        "--out",
        type=Path,
        default=Path("youtube"),
        help="Output root directory (default: ./youtube).",
    )
    p.add_argument(
        "--audio-format",
        default="mp3",
        help="Audio codec to extract to (mp3, m4a, opus, ...). Default: mp3.",
    )
    p.add_argument(
        "--max-downloads",
        type=int,
        default=None,
        help="Cap the number of videos downloaded this run (useful on first run).",
    )
    p.add_argument(
        "--scan-limit",
        type=int,
        default=DEFAULT_SCAN_LIMIT,
        help=(
            "How many of the channel's most-recent uploads to examine each run "
            f"(default: {DEFAULT_SCAN_LIMIT}). Use 0 to scan the whole channel "
            "(slow on large channels)."
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
        "--js-runtimes",
        action="append",
        metavar="RUNTIME[:PATH]",
        help=(
            "JavaScript runtime for YouTube extraction, e.g. 'node', 'bun', or "
            "'deno:/opt/deno'. Repeatable. Default: yt-dlp's built-in deno."
        ),
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
            "  pip install -r transcription/tools/requirements-youtube.txt",
            file=sys.stderr,
        )
        return 2

    def ydl_factory(opts: dict):
        return YoutubeDL(opts)

    cfg = FetchConfig(
        url=args.url,
        out=args.out,
        audio_format=args.audio_format,
        max_downloads=args.max_downloads,
        since=args.since,
        dry_run=args.dry_run,
        js_runtimes=parse_js_runtimes(args.js_runtimes),
        scan_limit=args.scan_limit,
    )

    emit, close_log = make_logger(args.log)
    emit(f"channel: {cfg.url}")
    emit(f"output : {cfg.out}  (archive: {cfg.archive_path})")
    if cfg.dry_run:
        emit("mode   : DRY RUN (no downloads)")

    try:
        stats = download_new(cfg, ydl_factory, log=emit)
    except Exception as exc:  # noqa: BLE001 — surface a clean message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        close_log()
        return 1

    emit("\n" + stats.summary())
    close_log()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
