#!/usr/bin/env python3
"""
Shared, dependency-free helpers for the media downloader tools
(``fetch_youtube.py`` and ``fetch_instagram.py``).

Everything here is pure (no network, no third-party libs) and unit-tested, so
both tools can rely on identical filename/stamp/archive/logging behaviour and the
same on-disk layout: ``{out}/{account}/{year}/{month}/{title}.{ext}``.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Optional

# Characters illegal in file/folder names on common filesystems (Windows is the
# strictest), plus ASCII control characters. Everything else — including
# non-Latin letters and emoji — is preserved so titles stay readable.
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Default number of an account's most-recent items to examine per run. Bounds
# listing so huge accounts don't stall before --max-downloads applies.
DEFAULT_SCAN_LIMIT = 50


def slugify_channel(title: Optional[str], fallback: str = "unknown-channel") -> str:
    """Turn a channel/account name into a safe single path segment.

    Strips illegal characters, collapses whitespace to single spaces, and trims
    leading/trailing spaces and dots (trailing dots/spaces are invalid on
    Windows). Returns ``fallback`` when the input is missing or empties out.
    """
    if not title:
        return fallback
    cleaned = _ILLEGAL_FS.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(". ")
    return cleaned or fallback


def sanitize_filename(name: Optional[str], max_len: int = 150, fallback: str = "video") -> str:
    """Turn a title/caption into a safe filename stem (no extension).

    Same illegal-character / whitespace rules as :func:`slugify_channel`, plus a
    length cap so we stay well under the 255-byte filesystem limit. Returns
    ``fallback`` if the input is missing or sanitises to nothing.
    """
    if not name:
        return fallback
    cleaned = _ILLEGAL_FS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(". ")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(". ")
    return cleaned or fallback


def stamp_from_datetime(d: dt.datetime) -> str:
    """Format a datetime as a 14-digit ``YYYYMMDDHHMMSS`` stamp in UTC.

    Timezone-aware datetimes are converted to UTC; naive datetimes are assumed
    to already be UTC (instaloader's ``Post.date_utc`` is naive UTC).
    """
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc)
    return d.strftime("%Y%m%d%H%M%S")


def dest_for(out_root: Path, account: str, stamp: str, title: str, ext: str) -> Path:
    """Build ``out/{account}/{YYYY}/{MM}/{title}.{ext}`` from a stamp + title.

    ``title`` is expected to be already sanitised by the caller; ``ext`` may be
    given with or without a leading dot.
    """
    year, month = stamp[:4], stamp[4:6]
    return Path(out_root) / account / year / month / f"{title}.{ext.lstrip('.')}"


def load_archive(archive_path: Path) -> set:
    """Read a download archive into a set of ``'<source> <id>'`` lines."""
    if not archive_path.exists():
        return set()
    lines = archive_path.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip()}


def append_archive(archive_path: Path, key: str) -> None:
    """Append one archive key, creating the file/parent dir if needed."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as fh:
        fh.write(key + "\n")


def make_logger(log_path: Optional[Path]):
    """Return ``(emit, close)``: ``emit(msg)`` prints to stdout and, when a log
    path is given, also appends a timestamped copy to that file.

    The console stays clean (no timestamps); file lines are prefixed with an
    ``YYYY-MM-DD HH:MM:SS`` timestamp and flushed immediately (so ``tail -f``
    works). ``close()`` closes the file handle (a no-op without a log file).
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
