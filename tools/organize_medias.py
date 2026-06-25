#!/usr/bin/env python3
"""
Organize a flat medias tree into the channel/year/month/day layout the
transcription CLI expects.

Input layout (what you have)::

    medias/{channel}/{YYYYMMDDHHMMSS}.<ext>      # all files flat under each channel

Output layout (what the CLI wants)::

    medias/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.<ext>

The date is read from the 14-digit ``YYYYMMDDHHMMSS`` filename stamp (the older
12-digit ``YYYYMMDDHHMM`` form is also accepted). Files whose names don't match,
and files already sitting in subfolders, are left untouched -- so the script is
safe to re-run.

Usage::

    python organize_medias.py --medias /path/to/medias --dry-run   # preview only
    python organize_medias.py --medias /path/to/medias             # move into place
    python organize_medias.py --medias /path/to/medias --copy      # copy instead of move
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# 14-digit YYYYMMDDHHMMSS (preferred) or 12-digit YYYYMMDDHHMM (legacy).
STAMP_RE = re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})\d{4}(?:\d{2})?$")

# Media extensions we relocate. Mirrors MEDIA_EXTS in transcription/core/selection.py.
MEDIA_EXTS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".mkv", ".mka", ".mov", ".webm", ".avi", ".ts", ".m4v",
}


def date_parts(stem: str):
    """Return (year, month, day) strings from a stamp stem, or None if invalid."""
    m = STAMP_RE.match(stem)
    if not m:
        return None
    y, mo, d = m.group("y"), m.group("m"), m.group("d")
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return None
    return y, mo, d


def organize(medias: Path, do_copy: bool, dry_run: bool):
    """Move/copy flat files into channel/year/month/day. Returns (done, skipped, bad)."""
    if not medias.is_dir():
        sys.exit(f"error: medias dir not found: {medias}")

    done = skipped = bad = 0
    action = "copy" if do_copy else "move"

    for channel_dir in sorted(p for p in medias.iterdir() if p.is_dir()):
        # Only files sitting directly in the channel folder (the flat ones).
        # sorted() materializes the list, so moving files mid-loop is safe.
        for f in sorted(p for p in channel_dir.iterdir() if p.is_file()):
            if f.suffix.lower() not in MEDIA_EXTS:
                continue
            parts = date_parts(f.stem)
            if parts is None:
                print(f"  [skip] unrecognised name: {f.relative_to(medias)}")
                bad += 1
                continue
            y, mo, d = parts
            dest_dir = channel_dir / y / mo / d
            dest = dest_dir / f.name
            if dest.exists():
                print(f"  [skip] target exists: {dest.relative_to(medias)}")
                skipped += 1
                continue
            print(f"  [{action}] {f.relative_to(medias)} -> {dest.relative_to(medias)}")
            if not dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if do_copy:
                    shutil.copy2(f, dest)
                else:
                    shutil.move(str(f), str(dest))
            done += 1
    return done, skipped, bad


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--medias", required=True,
                    help="Path to the medias root (the folder containing channel folders).")
    ap.add_argument("--copy", action="store_true",
                    help="Copy files instead of moving them (safer; needs 2x disk).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would happen; change nothing.")
    args = ap.parse_args()

    done, skipped, bad = organize(Path(args.medias), args.copy, args.dry_run)
    verb = "would " if args.dry_run else ""
    word = "copied" if args.copy else "moved"
    print(f"\n{verb}{word}: {done} | skipped (exists): {skipped} | unrecognised: {bad}")


if __name__ == "__main__":
    raise SystemExit(main())
