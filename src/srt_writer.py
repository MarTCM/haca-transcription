"""
Self-contained SRT writer.

Produces standard SubRip (.srt) files:
  - 1-based integer index
  - timestamps formatted as ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` (comma decimal)
  - blocks separated by a blank line
  - UTF-8 encoded

This format is exactly what a downstream parser (e.g. the HACA benchmark's
``srt_utils.parse_srt``) expects, so the two projects stay decoupled while
remaining interoperable through the file format alone.
"""

from pathlib import Path
from typing import Dict, List, Union


def _fmt_ts(seconds: float) -> str:
    """Format a time in seconds as an SRT timestamp ``HH:MM:SS,mmm``."""
    if seconds < 0:
        seconds = 0.0
    # Round to milliseconds first so 59.9996 doesn't print as 60 seconds.
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clean_text(text: str) -> str:
    """Collapse internal newlines/whitespace; an SRT cue keeps its own block layout."""
    return " ".join(str(text).split()).strip()


def render_srt(segments: List[Dict]) -> str:
    """
    Render segments to an SRT string.

    Each segment is a dict with at least ``start`` and ``end`` (seconds, float)
    and ``text``. Empty-text segments are skipped. Indices are assigned 1-based
    in input order.
    """
    blocks: List[str] = []
    idx = 1
    for seg in segments:
        text = _clean_text(seg.get("text", ""))
        if not text:
            continue
        start = float(seg["start"])
        end = float(seg["end"])
        if end < start:
            end = start
        blocks.append(
            f"{idx}\n{_fmt_ts(start)} --> {_fmt_ts(end)}\n{text}"
        )
        idx += 1
    # Trailing newline; blocks separated by a blank line.
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_srt(segments: List[Dict], path: Union[str, Path]) -> Path:
    """Write segments to ``path`` as a UTF-8 SRT file. Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_srt(segments), encoding="utf-8")
    return path
