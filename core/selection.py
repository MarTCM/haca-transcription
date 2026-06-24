"""
Medias-tree scanning and selection expansion.

The medias hierarchy is::

    medias/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMM}.mp3

This module turns coarse filters (channels, years, months, days, hours — any of
which may be omitted to mean "all") into a concrete, sorted list of relative
media paths (any extension in :data:`MEDIA_EXTS` — ``.mp3``, ``.mp4``, ...). The
same function backs both the CLI's filter flags and the FastAPI backend's
selection expansion, so the UI and CLI always agree on which files a given
selection matches.

Nothing here imports the heavy transcription stack, so it is cheap to call for a
dry-run listing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Union

# Media extensions we batch. Mirrors MEDIA_EXTS in src/transcribe.py and
# src/transcribe_whisperx.py (audio + video; faster-whisper's PyAV / WhisperX's
# ffmpeg extract the audio track from video). Kept as a local copy so this module
# stays free of any heavy imports.
MEDIA_EXTS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".mkv", ".mka", ".mov", ".webm", ".avi", ".ts", ".m4v",
}

# A medias filename is YYYYMMDDHHMM + any media extension, e.g. 202406010900.mp3
# or 202406010900.mp4.
_FILENAME_RE = re.compile(r"^(?P<stamp>\d{12})\.[A-Za-z0-9]+$")

# Nested index type: channel -> year -> month -> day -> [filenames]
MediaIndex = Dict[str, Dict[int, Dict[int, Dict[int, List[str]]]]]


class SelectionError(ValueError):
    """Raised when a range/list spec cannot be parsed."""


# --------------------------------------------------------------------------- #
# Range / list parsing
# --------------------------------------------------------------------------- #
def parse_ranges(spec: Union[str, None]) -> Optional[Set[int]]:
    """Parse a comma/range spec like ``"9-18,21"`` into a set of ints.

    Grammar: comma-separated tokens, each either a single integer (``"21"``) or
    an inclusive range (``"9-18"``). Whitespace is ignored. ``None`` or an empty
    string returns ``None`` meaning "no filter" (i.e. all values).

    Raises:
        SelectionError: on malformed input or reversed ranges.
    """
    if spec is None:
        return None
    spec = spec.strip()
    if not spec:
        return None

    out: Set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, _, hi_s = token.partition("-")
            lo_s, hi_s = lo_s.strip(), hi_s.strip()
            if not lo_s or not hi_s:
                raise SelectionError(f"invalid range {token!r} (expected 'lo-hi')")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise SelectionError(f"non-integer range {token!r}") from exc
            if lo > hi:
                raise SelectionError(f"reversed range {token!r} (lo > hi)")
            out.update(range(lo, hi + 1))
        else:
            try:
                out.add(int(token))
            except ValueError as exc:
                raise SelectionError(f"non-integer value {token!r}") from exc
    return out or None


def parse_channels(spec: Union[str, Iterable[str], None]) -> Optional[Set[str]]:
    """Parse channel filters into a set of names, or ``None`` for all.

    Accepts a comma-separated string, an iterable of strings (possibly each
    containing commas, as with a repeatable ``--channel`` flag), or ``None``.
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        parts = spec.split(",")
    else:
        parts = []
        for item in spec:
            parts.extend(str(item).split(","))
    names = {p.strip() for p in parts if p.strip()}
    return names or None


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def _int_or_none(name: str) -> Optional[int]:
    """Return int(name) if name is all digits, else None (skip stray dirs)."""
    return int(name) if name.isdigit() else None


def scan_medias(root: Union[str, Path]) -> MediaIndex:
    """Walk ``root`` into a nested ``channel -> year -> month -> day -> [files]`` index.

    Non-numeric year/month/day directories and non-media files (extensions
    outside :data:`MEDIA_EXTS`) are ignored.
    A missing ``root`` yields an empty index (graceful, never raises).
    """
    root = Path(root)
    index: MediaIndex = {}
    if not root.is_dir():
        return index

    for channel_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        channel = channel_dir.name
        years: Dict[int, Dict[int, Dict[int, List[str]]]] = {}
        for year_dir in sorted(p for p in channel_dir.iterdir() if p.is_dir()):
            year = _int_or_none(year_dir.name)
            if year is None:
                continue
            months: Dict[int, Dict[int, List[str]]] = {}
            for month_dir in sorted(p for p in year_dir.iterdir() if p.is_dir()):
                month = _int_or_none(month_dir.name)
                if month is None:
                    continue
                days: Dict[int, List[str]] = {}
                for day_dir in sorted(p for p in month_dir.iterdir() if p.is_dir()):
                    day = _int_or_none(day_dir.name)
                    if day is None:
                        continue
                    files = sorted(
                        f.name for f in day_dir.iterdir()
                        if f.is_file() and f.suffix.lower() in MEDIA_EXTS
                    )
                    if files:
                        days[day] = files
                if days:
                    months[month] = days
            if months:
                years[year] = months
        if years:
            index[channel] = years
    return index


def hour_of(filename: str) -> Optional[int]:
    """Extract the broadcast hour (0-23) from a ``YYYYMMDDHHMM.<ext>`` filename.

    Works for any media extension (``.mp3``, ``.mp4``, ...). Returns ``None`` if
    the filename doesn't match the 12-digit stamp pattern.
    """
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    stamp = m.group("stamp")  # YYYYMMDDHHMM
    return int(stamp[8:10])


# --------------------------------------------------------------------------- #
# Selection expansion
# --------------------------------------------------------------------------- #
def expand_selections(
    root: Union[str, Path],
    channels: Optional[Iterable[str]] = None,
    years: Optional[Set[int]] = None,
    months: Optional[Set[int]] = None,
    days: Optional[Set[int]] = None,
    hours: Optional[Set[int]] = None,
    *,
    index: Optional[MediaIndex] = None,
) -> List[str]:
    """Expand coarse filters into a sorted list of relative media paths.

    Includes any file whose extension is in :data:`MEDIA_EXTS` (``.mp3``,
    ``.mp4``, ...).

    Any filter passed as ``None`` (or an empty set) means "all" for that level.
    Paths are returned relative to ``root`` using forward slashes, e.g.
    ``"al-oula/2024/06/01/202406010900.mp3"``.

    Args:
        root: medias root directory.
        channels: channel names to include, or ``None`` for all.
        years/months/days/hours: int sets to include, or ``None`` for all.
        index: a pre-built :func:`scan_medias` index (avoids re-walking disk).
    """
    if index is None:
        index = scan_medias(root)

    channel_filter = set(channels) if channels else None
    results: List[str] = []

    for channel, year_map in index.items():
        if channel_filter is not None and channel not in channel_filter:
            continue
        for year, month_map in year_map.items():
            if years and year not in years:
                continue
            for month, day_map in month_map.items():
                if months and month not in months:
                    continue
                for day, files in day_map.items():
                    if days and day not in days:
                        continue
                    for filename in files:
                        if hours is not None:
                            h = hour_of(filename)
                            if h is None or h not in hours:
                                continue
                        results.append(
                            f"{channel}/{year:04d}/{month:02d}/{day:02d}/{filename}"
                        )
    results.sort()
    return results
