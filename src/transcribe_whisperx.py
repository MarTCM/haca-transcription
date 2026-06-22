"""
Alternate broadcast -> .srt pipeline using WhisperX.

Adds word-level alignment (wav2vec2) and speaker diarization (pyannote) on top
of the same per-chunk ASR + language-detection as transcribe.py.

Pipeline (see docs/TRANSCRIPTION.md):
    1. decode media to 16 kHz mono            (whisperx.load_audio)
    2. VAD segmentation                        (faster-whisper Silero VAD)
    3. group speech into ~25 s chunks          (respecting silence)
    4. per-chunk language detection            (constrained to allow-list)
    5. transcribe each chunk                   (whisperx / faster-whisper)
    6. [optional] wav2vec2 alignment           (per-language, graceful fallback)
    7. [optional] pyannote diarization         (full-audio, assigns [SPEAKER_XX])
    8. write standard .srt

Alignment fallback chain:
    - French → WhisperX default (ctc-wav2vec2-french)
    - Arabic → boualin/wav2vec2-large-xlsr-53-arabic (community model, BSD-3)
    - Other → silently skipped (unaligned Whisper timestamps used)

Dependencies (pip install -r requirements_whisperx.txt):
    whisperx>=3.8.0, pyannote.audio>=3.1.0

CLI (mirrors transcribe.py flags + diarization extras):
    python src/transcribe_whisperx.py --input show.mp4 --out-dir out/ \\
        [--model large-v3] [--device cuda|cpu|auto] \\
        [--lang auto|ar|fr] [--allowed ar,fr,en] \\
        [--max-chunk-s 25] [--batch-size 8] [--beam-size 5] \\
        [--diarize] [--hf-token TOKEN] \\
        [--min-speakers N] [--max-speakers N] [--overwrite]
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from srt_writer import write_srt

MEDIA_EXTS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".mkv", ".mka", ".mov", ".webm", ".avi", ".ts", ".m4v",
}

SAMPLE_RATE = 16000
DEFAULT_ALLOWED = ("ar", "fr", "en")

ARABIC_ALIGN_MODEL = "boualin/wav2vec2-large-xlsr-53-arabic"


def _import_whisperx():
    try:
        import whisperx
        return whisperx
    except ImportError:
        print(
            "error: whisperx not installed. Run:\n"
            "  pip install -r requirements_whisperx.txt",
            file=sys.stderr,
        )
        sys.exit(1)


def _auto_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
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
    """Load a WhisperX model (wraps faster-whisper CTranslate2 backend)."""
    whisperx = _import_whisperx()
    if device in (None, "auto"):
        device = _auto_device()
    if compute_type is None:
        compute_type = "float16" if device == "cuda" else "int8"
    print(
        f"[load] model={model_size} device={device} compute_type={compute_type}",
        file=sys.stderr,
    )
    return whisperx.load_model(model_size, device=device, compute_type=compute_type)


def decode_audio(path: str):
    """Decode any media file to a 16 kHz mono float32 numpy array."""
    whisperx = _import_whisperx()
    return whisperx.load_audio(path)


def vad_chunks(audio, max_chunk_s: float = 25.0) -> List[Dict]:
    """
    Run Silero VAD and group speech regions into chunks of at most
    ``max_chunk_s`` seconds, breaking only at silence between regions.
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    max_samples = int(max_chunk_s * SAMPLE_RATE)
    vad_opts = VadOptions(max_speech_duration_s=max_chunk_s)
    speech = get_speech_timestamps(audio, vad_options=vad_opts, sampling_rate=SAMPLE_RATE)

    if not speech:
        speech = [{"start": 0, "end": len(audio)}]

    regions: List[Dict] = []
    for r in speech:
        s, e = r["start"], r["end"]
        while e - s > max_samples:
            regions.append({"start": s, "end": s + max_samples})
            s += max_samples
        regions.append({"start": s, "end": e})

    chunks: List[Dict] = []
    cur_start = regions[0]["start"]
    cur_end = regions[0]["end"]
    for region in regions[1:]:
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


def detect_chunk_language(
    model,
    chunk_audio,
    allowed: Sequence[str] = DEFAULT_ALLOWED,
    fallback: str = "ar",
) -> str:
    """
    Detect the dominant language of a chunk via WhisperX's underlying
    faster-whisper model, constrained to ``allowed``.
    """
    try:
        lang, prob, _all = model.model.detect_language(chunk_audio)
    except Exception:
        return "auto"
    return lang if lang in allowed else fallback


def _load_align_model(language_code: str, device: str) -> Tuple:
    """
    Load a wav2vec2 alignment model with Arabic fallback.
    Returns (model, metadata) or (None, None) on failure.
    """
    whisperx = _import_whisperx()
    try:
        return whisperx.load_align_model(language_code, device=device)
    except ValueError:
        if language_code == "ar":
            try:
                print(
                    f"[align] Arabic fallback: {ARABIC_ALIGN_MODEL}",
                    file=sys.stderr,
                )
                return whisperx.load_align_model(
                    language_code, device=device, model_name=ARABIC_ALIGN_MODEL
                )
            except Exception as e:
                print(f"[align] Arabic fallback failed: {e}", file=sys.stderr)
        else:
            print(f"[align] no default aligner for {language_code}", file=sys.stderr)
        return None, None
    except Exception as e:
        print(f"[align] load failed for {language_code}: {e}", file=sys.stderr)
        return None, None


def transcribe_file(
    path: str,
    model,
    lang: str = "auto",
    allowed: Sequence[str] = DEFAULT_ALLOWED,
    max_chunk_s: float = 25.0,
    batch_size: int = 8,
    beam_size: int = 5,
    diarize: bool = False,
    hf_token: Optional[str] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    device: str = "cuda",
) -> List[Dict]:
    """
    Transcribe a media file, returning SRT-ready segment dicts.

    When ``diarize=True``, each segment's text is prefixed with ``[SPEAKER_XX]``
    if a speaker is identified.
    """
    whisperx = _import_whisperx()
    audio = decode_audio(path)
    chunks = vad_chunks(audio, max_chunk_s=max_chunk_s)
    print(f"[vad] {Path(path).name}: {len(chunks)} chunk(s)", file=sys.stderr)

    # ---- Stage: transcribe per chunk (preserves code-switching) ----
    all_segments: List[Dict] = []
    for ci, chunk in enumerate(chunks):
        if lang == "auto":
            chunk_lang = detect_chunk_language(model, chunk["audio"], allowed)
        else:
            chunk_lang = lang
        whisper_lang = None if chunk_lang in ("auto", None) else chunk_lang

        try:
            result = model.transcribe(
                chunk["audio"],
                language=whisper_lang,
                batch_size=batch_size,
                beam_size=beam_size,
            )
        except TypeError:
            result = model.transcribe(
                chunk["audio"],
                language=whisper_lang,
                batch_size=batch_size,
            )

        effective_lang = result.get("language", chunk_lang or "?")
        offset = chunk["start"]
        for seg in result["segments"]:
            text = seg["text"].strip()
            if not text:
                continue
            seg["start"] += offset
            seg["end"] += offset
            seg["lang"] = effective_lang
            all_segments.append(seg)

        print(
            f"[asr] chunk {ci + 1}/{len(chunks)} lang={effective_lang} "
            f"({len(result['segments'])} segs)",
            file=sys.stderr,
        )

    # ---- Stage: alignment per language (graceful fallback) ----
    langs_seen = []
    for s in all_segments:
        if s["lang"] not in langs_seen:
            langs_seen.append(s["lang"])

    aligned: List[Dict] = []
    for lang_code in langs_seen:
        lang_segs = [s for s in all_segments if s["lang"] == lang_code]
        if not lang_segs:
            continue
        am, meta = _load_align_model(lang_code, device)
        if am is not None:
            try:
                aligned_result = whisperx.align(
                    lang_segs, am, meta, audio, device,
                    interpolate_method="nearest",
                )
                for seg in aligned_result["segments"]:
                    seg["lang"] = lang_code
                aligned.extend(aligned_result["segments"])
                print(f"[align] {lang_code}: {len(lang_segs)} segments", file=sys.stderr)
                continue
            except Exception as e:
                print(f"[align] {lang_code} runtime error: {e}", file=sys.stderr)
        aligned.extend(lang_segs)

    # ---- Stage: diarization (pyannote, full-audio) ----
    if diarize:
        if not hf_token:
            print(
                "[diarize] --hf-token required for speaker diarization",
                file=sys.stderr,
            )
        else:
            try:
                from whisperx.diarize import DiarizationPipeline

                diarize_model = DiarizationPipeline(
                    use_auth_token=hf_token, device=device
                )
                diar_kw = {}
                if min_speakers is not None:
                    diar_kw["min_speakers"] = min_speakers
                if max_speakers is not None:
                    diar_kw["max_speakers"] = max_speakers
                diarize_segments = diarize_model(audio, **diar_kw)

                result_with_spk = whisperx.assign_word_speakers(
                    diarize_segments, {"segments": aligned}
                )
                aligned = result_with_spk["segments"]
                speaker_count = len({
                    s.get("speaker", "")
                    for s in aligned if s.get("speaker")
                })
                print(
                    f"[diarize] {speaker_count} speaker(s) assigned",
                    file=sys.stderr,
                )
            except Exception as e:
                print(f"[diarize] failed: {e}", file=sys.stderr)

    # ---- Build output for SRT writer ----
    aligned.sort(key=lambda s: s["start"])

    out_segments: List[Dict] = []
    for seg in aligned:
        text = seg["text"].strip()
        if not text:
            continue
        speaker = seg.get("speaker", "")
        if speaker:
            text = f"[{speaker}] {text}"
        out_segments.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": text,
        })

    return out_segments


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
                    "(WhisperX, per-chunk detection, optional diarization)."
    )
    ap.add_argument("--input", required=True,
                    help="Media file, or directory to batch-process.")
    ap.add_argument("--out-dir", default="out",
                    help="Where to write .srt files (default: out/).")
    ap.add_argument("--model", default="large-v3",
                    help="Model size or path (default: large-v3). "
                         "For Darija, 'large-v3-turbo' is faster with comparable quality; "
                         "see notebooks/ for the anaszil LoRA adapter benchmark.")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--compute-type", default=None,
                    help="Override compute type (float16, int8, ...).")
    ap.add_argument("--lang", default="auto",
                    help="'auto' for per-chunk detection, or force ar/fr.")
    ap.add_argument("--allowed", default=",".join(DEFAULT_ALLOWED),
                    help="Comma-separated allow-list (default: ar,fr,en).")
    ap.add_argument("--max-chunk-s", type=float, default=25.0,
                    help="Max chunk length in seconds (default: 25).")
    ap.add_argument("--batch-size", type=int, default=8,
                    help="WhisperX batch size (default: 8).")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--diarize", action="store_true",
                    help="Enable pyannote speaker diarization.")
    ap.add_argument("--hf-token", default=None,
                    help="HuggingFace read token for pyannote models.")
    ap.add_argument("--min-speakers", type=int, default=None,
                    help="Hint: minimum speakers (diarization).")
    ap.add_argument("--max-speakers", type=int, default=None,
                    help="Hint: maximum speakers (diarization).")
    ap.add_argument("--overwrite", action="store_true",
                    help="Re-transcribe even if .srt exists.")
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
            max_chunk_s=args.max_chunk_s,
            batch_size=args.batch_size, beam_size=args.beam_size,
            diarize=args.diarize, hf_token=args.hf_token,
            min_speakers=args.min_speakers, max_speakers=args.max_speakers,
            device=args.device if args.device != "auto" else _auto_device(),
        )
        write_srt(segments, srt_path)
        print(f"[srt ] {srt_path}  ({len(segments)} cues)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
