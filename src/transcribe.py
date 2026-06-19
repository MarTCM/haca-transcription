"""
Broadcast audio/video -> .srt transcription (Darija / Arabic / French).

Pipeline (see docs/TRANSCRIPTION.md):
    1. decode media to 16 kHz mono           (faster_whisper.decode_audio, PyAV)
    2. VAD segmentation                       (Silero VAD bundled with faster-whisper)
    3. group speech regions into ~25 s chunks (respecting silence boundaries)
    4. per-chunk language detection           (constrained to an allow-list)
    5. transcribe each chunk                  (faster-whisper Whisper large-v3)
    6. collect segments with absolute timing
    7. write a standard .srt                  (srt_writer.write_srt)

Per-chunk language detection (step 4) is what lets a single code-switched
broadcast (Darija + MSA + French) transcribe natively instead of being forced
into one language for the whole file.

The heavy dependency (faster-whisper) is imported lazily so that `--help`,
imports, and the SRT-writer tests work without it installed.

CLI:
    python src/transcribe.py --input <file|dir> --out-dir out/ \
        [--model large-v3] [--device cuda|cpu|auto] [--lang auto|ar|fr] \
        [--allowed ar,fr,en] [--max-chunk-s 25] [--beam-size 5] [--overwrite]
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from srt_writer import write_srt

# Media containers we will look for when --input is a directory.
MEDIA_EXTS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".mkv", ".mka", ".mov", ".webm", ".avi", ".ts", ".m4v",
}

SAMPLE_RATE = 16000
DEFAULT_ALLOWED = ("ar", "fr", "en")


# --------------------------------------------------------------------------- #
# Model + audio loading
# --------------------------------------------------------------------------- #
def _auto_device() -> str:
    """Return 'cuda' if a GPU is visible, else 'cpu'."""
    try:
        import torch  # optional; only used for the probe
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    # ctranslate2 can report CUDA devices even without torch installed.
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def load_model(
    model_size: str = "large-v3",
    device: Optional[str] = None,
    compute_type: Optional[str] = None,
):
    """
    Load a faster-whisper model.

    device       : 'cuda', 'cpu', or None/'auto' to detect.
    compute_type : None picks a sensible default per device
                   ('float16' on GPU, 'int8' on CPU).
    """
    from faster_whisper import WhisperModel

    if device in (None, "auto"):
        device = _auto_device()
    if compute_type is None:
        compute_type = "float16" if device == "cuda" else "int8"

    print(f"[load] model={model_size} device={device} compute_type={compute_type}",
          file=sys.stderr)
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def decode_audio(path: str):
    """Decode any media file to a 16 kHz mono float32 numpy array."""
    from faster_whisper import decode_audio as _decode
    return _decode(path, sampling_rate=SAMPLE_RATE)


# --------------------------------------------------------------------------- #
# VAD chunking
# --------------------------------------------------------------------------- #
def vad_chunks(audio, max_chunk_s: float = 25.0) -> List[Dict]:
    """
    Run Silero VAD and group speech regions into chunks of at most
    ``max_chunk_s`` seconds, breaking only at silence between regions.

    Returns a list of {'start': sec, 'end': sec, 'audio': np.ndarray} dicts
    where 'audio' is the contiguous slice for that chunk and start/end are
    absolute times in the original file.
    """
    from faster_whisper.vad import get_speech_timestamps

    speech = get_speech_timestamps(audio, sampling_rate=SAMPLE_RATE)

    # No speech detected (e.g. music-only or over-aggressive VAD): fall back to
    # treating the whole file as a single chunk so we never silently drop audio.
    if not speech:
        return [{"start": 0.0, "end": len(audio) / SAMPLE_RATE, "audio": audio}]

    max_samples = int(max_chunk_s * SAMPLE_RATE)
    chunks: List[Dict] = []
    cur_start = speech[0]["start"]
    cur_end = speech[0]["end"]

    for region in speech[1:]:
        # Extend the current chunk if it still fits within the size budget.
        if region["end"] - cur_start <= max_samples:
            cur_end = region["end"]
        else:
            chunks.append({"start": cur_start, "end": cur_end})
            cur_start = region["start"]
            cur_end = region["end"]
    chunks.append({"start": cur_start, "end": cur_end})

    out: List[Dict] = []
    for c in chunks:
        out.append({
            "start": c["start"] / SAMPLE_RATE,
            "end": c["end"] / SAMPLE_RATE,
            "audio": audio[c["start"]:c["end"]],
        })
    return out


# --------------------------------------------------------------------------- #
# Per-chunk language detection
# --------------------------------------------------------------------------- #
def detect_chunk_language(
    model,
    chunk_audio,
    allowed: Sequence[str] = DEFAULT_ALLOWED,
    fallback: str = "ar",
) -> str:
    """
    Detect the dominant language of a chunk, constrained to ``allowed``.

    Darija surfaces as 'ar' in Whisper; constraining to {ar, fr, en} keeps noisy
    Moroccan-Arabic speech from drifting to spurious languages. Anything outside
    the allow-list maps to ``fallback`` (default 'ar').
    """
    try:
        lang, prob, _all = model.detect_language(chunk_audio)
    except AttributeError:
        # Older faster-whisper without detect_language(): let transcribe() decide.
        return "auto"
    return lang if lang in allowed else fallback


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #
def transcribe_file(
    path: str,
    model,
    lang: str = "auto",
    allowed: Sequence[str] = DEFAULT_ALLOWED,
    max_chunk_s: float = 25.0,
    beam_size: int = 5,
) -> List[Dict]:
    """
    Transcribe one media file into a list of segments:
    {'start': sec, 'end': sec, 'text': str, 'lang': str}.

    lang='auto' -> per-chunk language detection (code-switching friendly).
    lang='ar'/'fr'/... -> force that language for every chunk.
    """
    audio = decode_audio(path)
    chunks = vad_chunks(audio, max_chunk_s=max_chunk_s)
    print(f"[vad] {Path(path).name}: {len(chunks)} chunk(s)", file=sys.stderr)

    segments: List[Dict] = []
    for ci, chunk in enumerate(chunks):
        if lang == "auto":
            chunk_lang = detect_chunk_language(model, chunk["audio"], allowed)
        else:
            chunk_lang = lang
        # 'auto' here means: let transcribe() detect (older fw without detect_language).
        whisper_lang = None if chunk_lang == "auto" else chunk_lang

        result, info = model.transcribe(
            chunk["audio"],
            language=whisper_lang,
            beam_size=beam_size,
            vad_filter=False,                 # already VAD-chunked
            condition_on_previous_text=False, # avoid hallucination carry-over across chunks
        )
        effective_lang = whisper_lang or getattr(info, "language", "?")
        offset = chunk["start"]
        for seg in result:
            text = seg.text.strip()
            if not text:
                continue
            segments.append({
                "start": offset + seg.start,
                "end": offset + seg.end,
                "text": text,
                "lang": effective_lang,
            })
        print(f"[asr] chunk {ci + 1}/{len(chunks)} lang={effective_lang}",
              file=sys.stderr)

    return segments


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _gather_inputs(input_path: Path) -> List[Path]:
    if input_path.is_dir():
        return sorted(
            p for p in input_path.iterdir()
            if p.is_file() and p.suffix.lower() in MEDIA_EXTS
        )
    return [input_path]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Transcribe Darija/Arabic/French broadcasts to .srt "
                    "(faster-whisper, per-chunk language detection)."
    )
    ap.add_argument("--input", required=True,
                    help="Media file, or a directory to batch-process.")
    ap.add_argument("--out-dir", default="out",
                    help="Where to write .srt files (default: out/).")
    ap.add_argument("--model", default="large-v3",
                    help="faster-whisper model size or path (default: large-v3).")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--compute-type", default=None,
                    help="Override compute type (e.g. float16, int8, int8_float16).")
    ap.add_argument("--lang", default="auto",
                    help="'auto' for per-chunk detection, or force a code (ar, fr...).")
    ap.add_argument("--allowed", default=",".join(DEFAULT_ALLOWED),
                    help="Comma-separated allow-list for auto detection (default: ar,fr,en).")
    ap.add_argument("--max-chunk-s", type=float, default=25.0,
                    help="Max chunk length in seconds (default: 25).")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--overwrite", action="store_true",
                    help="Re-transcribe even if the .srt already exists.")
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"error: input not found: {input_path}", file=sys.stderr)
        return 2

    files = _gather_inputs(input_path)
    if not files:
        print(f"error: no media files found in {input_path}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    allowed = tuple(x.strip() for x in args.allowed.split(",") if x.strip())

    # Decide up-front which files actually need work, so we can skip loading the
    # (large) model if there's nothing to do.
    todo = []
    for f in files:
        srt_path = out_dir / (f.stem + ".srt")
        if srt_path.exists() and not args.overwrite:
            print(f"[skip] {srt_path} exists (use --overwrite)", file=sys.stderr)
            continue
        todo.append((f, srt_path))

    if not todo:
        print("[done] nothing to do.", file=sys.stderr)
        return 0

    model = load_model(args.model, args.device, args.compute_type)

    for f, srt_path in todo:
        print(f"[file] {f}", file=sys.stderr)
        segments = transcribe_file(
            str(f), model,
            lang=args.lang, allowed=allowed,
            max_chunk_s=args.max_chunk_s, beam_size=args.beam_size,
        )
        write_srt(segments, srt_path)
        print(f"[srt ] {srt_path}  ({len(segments)} cues)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
