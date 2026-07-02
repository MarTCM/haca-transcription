#!/usr/bin/env python3
"""
Batch transcription CLI.

Transcribes a filtered subset of a ``medias/`` tree to mirrored ``.srt`` files,
sharing all selection + transcription logic with the FastAPI backend via
``transcription/core``.

Examples::

    # Dry-run: list what would be transcribed, don't run models.
    python cli.py --channel al-oula --year 2024 --month 6 --hours 9-18 --dry-run

    # Transcribe two channels, June 2024, all hours, with the defaults.
    python cli.py --channel al-oula,2m --year 2024 --month 6

    # Speaker annotation (WhisperX + diarization); needs an HF token.
    python cli.py --channel 2m --year 2024 --month 6 --day 1 \\
        --speaker-annotation --hf-token hf_xxx

See ``CLI_PLAN.md`` for the full design.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

# Allow running both as ``python cli.py`` (script) and ``python -m cli``.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import (  # noqa: E402
    ConfigError,
    TranscribeConfig,
    PIPELINE_FASTER_WHISPER,
    PIPELINE_WHISPERX,
    DEFAULT_ALLOWED,
    DEFAULT_OUT_DIR,
)
from core.selection import (  # noqa: E402
    SelectionError,
    parse_channels,
    parse_ranges,
    expand_selections,
)
from core import runner  # noqa: E402
from core import summary  # noqa: E402

# Exit codes.
EXIT_OK = 0
EXIT_SOME_FAILED = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (kept separate so tests can introspect it)."""
    ap = argparse.ArgumentParser(
        prog="cli.py",
        description="Batch-transcribe a medias/ tree to mirrored .srt files "
                    "(Darija/Arabic/French, per-chunk language detection).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    ap.add_argument("--mode", choices=["medias", "youtube", "tiktok"], default="medias",
                    help="Ingestion mode structure.")
    ap.add_argument("--medias", default=None,
                    help="Root of the media directory tree (defaults to the value of --mode).")

    # --- Filters (omit any = all) ---
    g = ap.add_argument_group("filters (omit any flag to mean 'all')")
    g.add_argument("--channel", action="append", default=None,
                   help="Channel name(s); repeatable and/or comma-separated.")
    g.add_argument("--year", default=None,
                   help="Year filter: list and/or range, e.g. '2024' or '2024-2025'.")
    g.add_argument("--month", default=None,
                   help="Month filter (1-12): list and/or range, e.g. '1-6'.")
    g.add_argument("--day", default=None,
                   help="Day filter (1-31): list and/or range, e.g. '1,15,30'.")
    g.add_argument("--hours", "--hour", dest="hours", default=None,
                   help="Hour filter (0-23): list and/or range, e.g. '9-18,21'.")

    # --- Headline option ---
    ap.add_argument("--speaker-annotation", action="store_true",
                    help="Enable speaker diarization (implies --pipeline whisperx; "
                         "requires --hf-token or $HF_TOKEN). Off by default.")

    # --- Defaulted-but-overridable model options ---
    m = ap.add_argument_group("model options (sensible defaults)")
    m.add_argument("--pipeline", choices=[PIPELINE_FASTER_WHISPER, PIPELINE_WHISPERX],
                   default=PIPELINE_FASTER_WHISPER, help="Transcription backend.")
    m.add_argument("--model", default="large-v3",
                   help="faster-whisper model size or local path.")
    m.add_argument("--darija-model", choices=["large", "small"], default="large",
                   help="Select the Darija model type for Arabic chunks: 'large' (large-v3-turbo LoRA) "
                        "or 'small' (ychafiqui/whisper-small-darija).")
    lora = m.add_mutually_exclusive_group()
    lora.add_argument("--darija-lora", dest="darija_lora", action="store_true",
                      default=True,
                      help="Route Arabic chunks through the anaszil Darija LoRA (default).")
    lora.add_argument("--no-darija-lora", dest="darija_lora", action="store_false",
                      help="Disable the Darija LoRA; use the base model for all chunks.")
    m.add_argument("--lang", default="auto",
                   help="'auto' for per-chunk detection, or a forced code (ar/fr/en).")
    m.add_argument("--allowed", default=",".join(DEFAULT_ALLOWED),
                   help="Comma-separated allow-list for auto language detection.")
    m.add_argument("--max-chunk-s", type=float, default=25.0,
                   help="Max VAD chunk length in seconds.")
    m.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto",
                   help="Compute device.")
    m.add_argument("--overwrite", action="store_true",
                   help="Re-transcribe even if the .srt already exists.")
    m.add_argument("--hf-token", default=None,
                   help="Hugging Face token for diarization (or set $HF_TOKEN).")

    # --- Output / behavior ---
    o = ap.add_argument_group("output & behavior")
    o.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                   help="Output root; .srt files mirror the medias arborescence.")
    o.add_argument("--log-file", default=None,
                   help="Run log path (default: out/logs/cli-<timestamp>.log).")
    o.add_argument("--dry-run", action="store_true",
                   help="List the matched files and exit without transcribing.")
    o.add_argument("-v", "--verbose", action="store_true",
                   help="Print each per-file log line to stderr as it happens.")

    return ap


def config_from_args(args: argparse.Namespace) -> TranscribeConfig:
    """Build (and validate) a TranscribeConfig from parsed args.

    Turning on ``--speaker-annotation`` auto-selects the WhisperX pipeline and
    falls back to ``$HF_TOKEN`` when ``--hf-token`` is not given.
    """
    pipeline = args.pipeline
    hf_token = args.hf_token or os.environ.get("HF_TOKEN")

    if args.speaker_annotation:
        # Annotation only exists on the WhisperX path; upgrade transparently.
        pipeline = PIPELINE_WHISPERX

    cfg = TranscribeConfig(
        pipeline=pipeline,
        speaker_annotation=args.speaker_annotation,
        hf_token=hf_token,
        model=args.model,
        darija_lora=args.darija_lora,
        darija_model=args.darija_model,
        language=args.lang,
        allowed_langs=args.allowed,
        max_chunk_s=args.max_chunk_s,
        device=args.device,
        overwrite=args.overwrite,
        out_dir=args.out_dir,
    )
    return cfg.validate()


def _default_log_path(out_dir: str) -> Path:
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return Path(out_dir).parent / "logs" / f"cli-{stamp}.log"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    # 1. Parse filters.
    try:
        channels = parse_channels(args.channel)
        years = parse_ranges(args.year)
        months = parse_ranges(args.month)
        days = parse_ranges(args.day)
        hours = parse_ranges(args.hours)

        if args.mode in ("youtube", "tiktok"):
            if args.day is not None:
                raise SelectionError(f"--day filter is not supported in {args.mode} mode")
            if args.hours is not None:
                raise SelectionError(f"--hours filter is not supported in {args.mode} mode")
    except SelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # 2. Build + validate config (covers annotation/token rules).
    try:
        config = config_from_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # 3. Expand the selection against the medias tree.
    mode = args.mode
    medias_root_str = args.medias if args.medias is not None else mode
    medias_root = Path(medias_root_str)
    if not medias_root.is_dir():
        print(f"error: {mode} directory not found: {medias_root}", file=sys.stderr)
        return EXIT_USAGE

    files = expand_selections(
        medias_root, channels=channels, years=years,
        months=months, days=days, hours=hours,
        mode=mode,
    )
    if not files:
        print("0 files matched the given filters.", file=sys.stderr)
        return EXIT_USAGE

    # 4. Dry-run: just list and stop.
    if args.dry_run:
        for rel in files:
            print(rel)
        print(f"\n{len(files)} file(s) matched | {config.summary_str()}",
              file=sys.stderr)
        return EXIT_OK

    # 5. Real run: load models once, transcribe each file, log + summarize.
    return _run(files, medias_root, config, args)


def _run(files: List[str], medias_root: Path, config: TranscribeConfig,
         args: argparse.Namespace) -> int:
    """Load models, process every file, write the log, print the summary."""
    log_path = Path(args.log_file) if args.log_file else _default_log_path(config.out_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    run = summary.RunSummary(total=len(files))

    try:
        bundle = runner.load_models(config)
    except Exception as exc:  # noqa: BLE001
        print(f"error: failed to load models: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_SOME_FAILED

    with log_path.open("w", encoding="utf-8") as log:
        start = summary.format_job_start(len(files), config.summary_str())
        log.write(start + "\n")
        log.flush()
        if args.verbose:
            print(start, file=sys.stderr)

        for rel in files:
            result = run.add(runner.run_file(medias_root, rel, config, bundle))
            if result.status == summary.STATUS_COMPLETED:
                line = summary.format_ok(rel, result.processing_seconds)
            elif result.status == summary.STATUS_SKIPPED:
                line = summary.format_skip(rel)
            else:
                line = summary.format_fail(rel, result.error or "unknown error")
            log.write(line + "\n")
            log.flush()
            if args.verbose:
                print(line, file=sys.stderr)

        end = run.end_line()
        log.write(end + "\n")

    print(f"{run.oneline()}\nlog: {log_path}")
    return EXIT_OK if run.failed == 0 else EXIT_SOME_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
