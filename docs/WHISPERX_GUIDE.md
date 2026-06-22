# WhisperX Pipeline — Complete Guide

## Overview

`transcribe_whisperx.py` is an alternate transcription pipeline that replaces the
faster-whisper-only approach of `transcribe.py` with **WhisperX** — a wrapper that
adds:

- **Word-level alignment** via wav2vec2 forced alignment (sub-100 ms timestamp accuracy)
- **Speaker diarization** via pyannote.audio 3.1 (labels each cue with `[SPEAKER_00]`)
- **Batched inference** for higher throughput on GPU

Both files coexist. `transcribe.py` remains the simpler, dependency-lighter default.
`transcribe_whisperx.py` is the upgrade path when you need speakers or precise words.

### File location

```
transcription/
├── src/
│   ├── transcribe.py          # original (faster-whisper only)
│   ├── transcribe_whisperx.py # this file (WhisperX + diarization)
│   └── srt_writer.py          # shared SRT writer (used by both)
├── notebooks/
│   ├── kaggle_transcribe.ipynb    # original notebook
│   └── kaggle_whisperx.ipynb     # WhisperX notebook
├── docs/
│   ├── TRANSCRIPTION.md           # original design notes
│   └── WHISPERX_GUIDE.md         # this file
├── requirements.txt               # original deps
├── requirements_whisperx.txt      # extra deps for WhisperX
└── tests/
    └── test_srt_writer.py         # shared tests
```

### Dependencies

```
whisperx>=3.8.0
pyannote.audio>=3.1.0
pytest>=8.0
```

---

## Pipeline — 8 stages

```
audio file
    │
    ▼
┌──────────────────────────────────────────────┐
│ 1. Decode to 16 kHz mono                     │  whisperx.load_audio()
│    (any format: mp4, mkv, mp3, wav, ...)     │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│ 2. VAD segmentation                          │  Silero VAD (via faster-whisper)
│    (Silero VAD finds speech regions)         │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│ 3. Group into ~25 s chunks                   │  vad_chunks()
│    (merge small regions, break at silence    │
│     when over budget)                        │
└──────────────────────────────────────────────┘
    │
    ▼  (per chunk)
┌──────────────────────────────────────────────┐
│ 4. Per-chunk language detection              │  detect_chunk_language()
│    (constrained to allow-list ar,fr,en)      │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│ 5. Transcribe chunk                          │  model.transcribe()
│    (WhisperX / faster-whisper large-v3)      │
└──────────────────────────────────────────────┘
    │
    ▼  (merge all chunks)
┌──────────────────────────────────────────────┐
│ 6. [optional] wav2vec2 alignment             │  whisperx.align()
│    (per-language, graceful fallback)         │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│ 7. [optional] pyannote diarization           │  DiarizationPipeline()
│    (full-audio speaker labeling)             │  assign_word_speakers()
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│ 8. Write standard .srt                       │  srt_writer.write_srt()
│    (with optional [SPEAKER_XX] prefixes)     │
└──────────────────────────────────────────────┘
    │
    ▼
  .srt file
```

---

## Code walkthrough

### 1. Constants and imports

```python
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
```

- `srt_writer` is the shared SRT writer used by both pipelines (same file, no duplication)
- `MEDIA_EXTS` — every container/audio format WhisperX's decoder (PyAV) can handle
- `SAMPLE_RATE` — Whisper operates on 16 kHz mono. All audio is resampled to this
- `DEFAULT_ALLOWED` — the language allow-list for per-chunk detection. Darija surfaces
  as `ar` in Whisper; constraining to `ar, fr, en` prevents mis-detection as Persian/Urdu
- `ARABIC_ALIGN_MODEL` — a community wav2vec2 model fine-tuned on Arabic (Common Voice +
  MGB-2). BSD-3 licensed. Used as fallback since WhisperX has no default Arabic aligner

### 2. Lazy WhisperX import

```python
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
```

WhisperX is **imported lazily** — it's only loaded when a function that needs it is
actually called. This means:
- `--help` and argument parsing work without whisperx installed
- Importing the module won't pull in 3 GB of torch/ctranslate2 at module-load time
- The error message tells you exactly what to install

Every function that touches WhisperX calls `_import_whisperx()` first.

### 3. Device auto-detection

```python
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
```

Checks two backends for GPU availability:
1. **PyTorch** (`torch.cuda.is_available()`) — the standard CUDA probe
2. **CTranslate2** (faster-whisper's inference engine) — a fallback for environments
   where torch isn't installed but ctranslate2 has CUDA support

If neither finds a GPU, returns `"cpu"`. This is the same logic used in
`transcribe.py`.

### 4. Model loading

```python
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
```

- Wraps `whisperx.load_model()`, which loads a **faster-whisper** model (CTranslate2
  backend) then wraps it with the WhisperX API for alignment/diarization
- `device="auto"` → GPU if available, else CPU
- `compute_type` defaults: `"float16"` on GPU (fast, ~3 GB VRAM for large-v3),
  `"int8"` on CPU (slower but memory-efficient)
- Progress is printed to stderr so stdout stays clean for piping

### 5. Audio decoding

```python
def decode_audio(path: str):
    """Decode any media file to a 16 kHz mono float32 numpy array."""
    whisperx = _import_whisperx()
    return whisperx.load_audio(path)
```

WhisperX's `load_audio()` wraps PyAV (ffmpeg bindings) to decode any media format
to a 16 kHz mono `float32` numpy array. Handles everything from `.wav` to `.mp4`,
`.mkv`, `.ts` (broadcast transport streams), etc.

### 6. VAD chunking (preserves code-switching)

```python
def vad_chunks(audio, max_chunk_s: float = 25.0) -> List[Dict]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    max_samples = int(max_chunk_s * SAMPLE_RATE)
    vad_opts = VadOptions(max_speech_duration_s=max_chunk_s)
    speech = get_speech_timestamps(audio, vad_options=vad_opts, sampling_rate=SAMPLE_RATE)
```

Uses Silero VAD (bundled with faster-whisper) to find speech regions in the audio.
Silero is a neural VAD — it outputs `{start, end}` pairs in sample indices for every
speech segment. The `max_speech_duration_s` option caps each VAD region at the chunk
budget, so continuous speech gets split at natural pauses (Silero's model can detect
sentence boundaries).

```python
    if not speech:
        speech = [{"start": 0, "end": len(audio)}]
```

If VAD finds no speech at all (e.g. music, very noisy clip, over-aggressive threshold),
fall back to treating the entire file as one chunk. This guarantees we never silently
drop audio.

```python
    regions: List[Dict] = []
    for r in speech:
        s, e = r["start"], r["end"]
        while e - s > max_samples:
            regions.append({"start": s, "end": s + max_samples})
            s += max_samples
        regions.append({"start": s, "end": e})
```

Defensive hard-split: if any VAD region still exceeds the budget (e.g. Silero version
that ignores `max_speech_duration_s`), cut it into budget-sized pieces.

```python
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
```

Group small adjacent speech regions into chunks of at most `max_chunk_s`. The grouping
only breaks at silence — this ensures we never cut mid-word or mid-sentence. A chunk
might be 12 s (one short region) or 25 s (several regions merged).

```python
    out: List[Dict] = []
    for c in chunks:
        out.append({
            "start": c["start"] / SAMPLE_RATE,
            "end": c["end"] / SAMPLE_RATE,
            "audio": audio[c["start"]:c["end"]],
        })
    return out
```

Convert sample indices to seconds, slice the raw audio array for each chunk, and return
a list of `{start, end, audio}` dicts. Each chunk has its absolute start time in the
original file, which is used later to offset segment timestamps.

**Why per-chunk instead of one-shot WhisperX?** WhisperX's built-in pipeline detects
one language for the entire file. A HACA broadcast can switch between Darija, French,
and MSA in the same programme. By VAD-chunking first and detecting language per chunk,
each chunk transcribes in its own language — code-switching works natively.

### 7. Per-chunk language detection

```python
def detect_chunk_language(
    model,
    chunk_audio,
    allowed: Sequence[str] = DEFAULT_ALLOWED,
    fallback: str = "ar",
) -> str:
    try:
        lang, prob, _all = model.model.detect_language(chunk_audio)
    except Exception:
        return "auto"
    return lang if lang in allowed else fallback
```

- Uses the underlying faster-whisper model (`model.model.detect_language()`) for a
  fast language prediction on the chunk audio
- If the detected language is in the allow-list (`ar, fr, en`), use it
- If not (e.g. Whisper thinks noisy Darija is Urdu), fall back to `"ar"` (the most
  common broadcast language)
- If detection fails entirely, return `"auto"` — letting Whisper's transcribe()
  auto-detect during the actual transcription pass
- The detection is just a forward pass through Whisper's encoder head — it takes ~50 ms
  on GPU and doesn't require decoding

### 8. Alignment model loading (with Arabic fallback)

```python
def _load_align_model(language_code: str, device: str) -> Tuple:
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
```

WhisperX uses **language-specific wav2vec2 models** for forced alignment. It ships
with tested defaults for `{en, fr, de, es, it, ja, zh, nl, uk, pt}`.

For our use case:
- **French** (`fr`) → WhisperX picks `ctc-wav2vec2-french` automatically
- **Arabic** (`ar`) → not in the default list, so `load_align_model()` raises
  `ValueError`. We catch it and try the community model `boualin/wav2vec2-large-xlsr-53-arabic`
  (fine-tuned on Arabic Common Voice + MGB-2, BSD-3 license). It uses a CTC character
  tokenizer compatible with WhisperX's alignment pipeline
- **Any other language** → silently skipped. The segments keep Whisper's native
  segment-level timestamps (accurate to ~1 s, good enough for SRT)

If loading or inference fails for any reason, we log a warning and continue without
alignment — the pipeline never crashes on an unavailable aligner.

### 9. The core function — `transcribe_file()`

```python
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
```

This is the main entry point. It returns a list of SRT-ready segment dicts:
```python
[
    {"start": 0.0, "end": 2.5, "text": "مرحبا بالجميع"},
    {"start": 2.5, "end": 5.0, "text": "[SPEAKER_01] Bonjour à tous"},
]
```

When `diarize=False`, no `[SPEAKER_XX]` prefix is added. When `diarize=True`, each
segment that has a speaker assignment gets the prefix.

#### 9a. Transcribe per chunk

```python
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
                vad_filter=False,
                condition_on_previous_text=False,
                beam_size=beam_size,
            )
        except TypeError:
            result = model.transcribe(
                chunk["audio"],
                language=whisper_lang,
                batch_size=batch_size,
                vad_filter=False,
                condition_on_previous_text=False,
            )
```

For each chunk:
1. Detect language (or use forced language from `--lang`)
2. Transcribe with WhisperX. `vad_filter=False` because we already VAD-chunked
   (double VAD can cut speech boundaries twice, degrading quality)
3. `condition_on_previous_text=False` prevents hallucination carry-over between chunks
4. The `try/except TypeError` handles WhisperX versions where `beam_size` is not
   forwarded to the underlying faster-whisper call

```python
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
```

- `offset` adjusts segment timestamps from relative-within-chunk to absolute in file
- The `lang` field is tagged on each segment for the alignment stage (alignment is
  per-language)
- Empty-text segments are dropped (Whisper sometimes emits silence as empty cues)

#### 9b. Alignment per language

```python
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
                continue
            except Exception as e:
                print(f"[align] {lang_code} runtime error: {e}", file=sys.stderr)
        aligned.extend(lang_segs)
```

Alignment runs **once per language** (not once per chunk):
1. Collect all segments for each language
2. Load the appropriate wav2vec2 model (French default / Arabic fallback / skip)
3. Call `whisperx.align()` with the segments and the full audio waveform
4. If alignment succeeds, the output segments have `words` key (word-level timestamps)
5. If alignment fails, fall back to the original unaligned segments

The `interpolate_method="nearest"` parameter handles words that the aligner can't
find in the audio (e.g. punctuation, numbers) by copying the nearest aligned word's
timestamp.

**Why alignment per language instead of all at once?** The wav2vec2 model is
language-specific — French wav2vec2 can't align Arabic text, and vice versa. Running
alignment separately per language ensures each segment uses the right phonetic model.

**Why is alignment optional?** Whisper's native segment timestamps are accurate to
~1-2 s, which is acceptable for SRT subtitles. Alignment improves this to ~50-100 ms
per word, which matters for karaoke-style highlighting but isn't critical for
downstream NLP. And Arabic has no default aligner in WhisperX, so we'd be forcing a
community model that might not work for all audio conditions.

#### 9c. Diarization (pyannote)

```python
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
                print(f"[diarize] {speaker_count} speaker(s) assigned", file=sys.stderr)
            except Exception as e:
                print(f"[diarize] failed: {e}", file=sys.stderr)
```

Speaker diarization identifies **who spoke when** using pyannote.audio:
1. `DiarizationPipeline` loads a pre-trained speaker embedding model from HuggingFace
   (gated — requires accepting terms and providing a read token)
2. `diarize_model(audio)` returns a DataFrame with `{start, end, speaker}` rows
   for each speaker turn
3. `assign_word_speakers()` merges the diarization output with our transcribed
   segments by timestamp overlap — each segment gets a `speaker` field like
   `"SPEAKER_00"`
4. The speaker hint parameters (`--min-speakers`, `--max-speakers`) can improve
   accuracy if you know approximately how many speakers are in the recording

Key details:
- Diarization runs on the **full audio** (not per-chunk), which gives the model
  more context to distinguish speakers consistently across the whole file
- pyannote is **language-agnostic** — it works on voice characteristics (pitch,
  timbre, rhythm), not on language content. So it works equally well on Darija,
  French, or mixed audio
- If `--hf-token` is not provided but `--diarize` is set, we print a warning and
  skip diarization rather than crashing

**HuggingFace token setup:**
1. Create account at huggingface.co
2. Accept terms at:
   - https://huggingface.co/pyannote/speaker-diarization-community-1
   - https://huggingface.co/pyannote/segmentation-3.0
3. Generate a read token at https://huggingface.co/settings/tokens
4. Pass it with `--hf-token YOUR_TOKEN`

#### 9d. Build SRT output

```python
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
```

Final assembly:
1. Sort segments chronologically (alignment may have reordered segments by language)
2. Strip leading/trailing whitespace from text
3. If diarization assigned a speaker, prefix text with `[SPEAKER_XX]`
4. Return clean dicts with only the fields `srt_writer.write_srt()` needs
   (`start`, `end`, `text`)

The speaker prefix format `[SPEAKER_00]` is transparent to the downstream benchmark's
SRT parser (`srt_utils.parse_srt` in `benchmark/src/`) — it reads the text field as-is,
so the speaker label becomes part of the text that the tonality classifier sees. This
is intentional: different speakers may have different tonality, and keeping the label
in the text gives the classifier the option to learn speaker-specific patterns.

### 10. File gathering

```python
def _gather_inputs(input_path: Path) -> List[Path]:
    if input_path.is_dir():
        return sorted(
            p for p in input_path.iterdir()
            if p.is_file() and p.suffix.lower() in MEDIA_EXTS
        )
    return [input_path]
```

If `--input` is a directory, scan it for files with known media extensions and sort
alphabetically. If it's a single file, wrap it in a list. This is the same logic as
`transcribe.py`.

### 11. CLI (`main()`)

```python
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
                    help="Model size or path (default: large-v3).")
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
```

All CLI flags match `transcribe.py` where applicable, with five additions:

| Flag | Added for | Default | Meaning |
|---|---|---|---|
| `--batch-size` | WhisperX | 8 | Number of audio chunks to process in parallel on GPU |
| `--diarize` | WhisperX | off | Enable pyannote speaker diarization |
| `--hf-token` | WhisperX | None | HuggingFace read token (required for diarization) |
| `--min-speakers` | WhisperX | None | Hint for diarization (speaker count floor) |
| `--max-speakers` | WhisperX | None | Hint for diarization (speaker count ceiling) |

The `--compute-type` flag is inherited (allows `int8` on low-VRAM GPUs or CPU).

```python
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
```

Pre-processing:
1. Validate input exists
2. Gather all candidate files (single or batch)
3. Filter out files that already have `.srt` output (unless `--overwrite`)
4. Skip model loading entirely if there's nothing to do — this saves 5-10 seconds
   and 3 GB of GPU memory on Kaggle reruns

```python
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
```

Execution:
1. Load the model (once, reused for all files in batch mode)
2. For each file: transcribe → write SRT
3. Returns 0 on success, 2 on input errors

---

## CLI usage

### Basic transcription (no diarization)

```bash
# Single file — GPU, per-chunk language detection
python src/transcribe_whisperx.py --input show.mp4 --out-dir out/

# Batch directory
python src/transcribe_whisperx.py --input broadcasts/ --out-dir out/

# CPU smoke test (tiny model, int8)
python src/transcribe_whisperx.py --input clip.wav --model tiny --device cpu

# Force French (skip per-chunk detection)
python src/transcribe_whisperx.py --input journal.mp3 --lang fr
```

### With speaker diarization

```bash
python src/transcribe_whisperx.py --input interview.mp4 \
    --diarize --hf-token hf_your_read_token_here

# With speaker hints (if you know there are 3 people)
python src/transcribe_whisperx.py --input panel.mp4 \
    --diarize --hf-token hf_xxx --min-speakers 3 --max-speakers 3
```

This produces SRT with `[SPEAKER_00]`/`[SPEAKER_01]` prefixes.

### Output

```
out/
├── show.srt           # plain transcription
├── interview.srt      # with [SPEAKER_00] / [SPEAKER_01] prefixes
└── panel.srt          # with speakers (and hint bounds)
```

---

## Output format

### Without diarization

```
1
00:00:00,000 --> 00:00:02,500
السلام عليكم ورحمة الله

2
00:00:02,500 --> 00:00:05,000
Bonjour tout le monde
```

### With diarization

```
1
00:00:00,000 --> 00:00:02,500
[SPEAKER_00] السلام عليكم ورحمة الله

2
00:00:02,500 --> 00:00:05,000
[SPEAKER_01] Bonjour tout le monde
```

The format is a standard SubRip file as defined by `srt_writer.py`:
- 1-based integer index
- `HH:MM:SS,mmm --> HH:MM:SS,mmm` (comma as decimal separator)
- UTF-8 encoded
- Blocks separated by a blank line

---

## Compute requirements

| Scenario | device | compute_type | model | VRAM | Notes |
|---|---|---|---|---|---|
| Kaggle T4 (GPU) | cuda | float16 | large-v3 | ~3 GB | Primary path, best quality |
| Local RTX 3060+ | cuda | float16 | large-v3 | ~3 GB | Comfortable with 8 GB+ cards |
| Local RTX 2060 | cuda | int8 | large-v3 | ~1.8 GB | Slight quality loss, works |
| CPU only | cpu | int8 | tiny/base | ~0.5 GB | Smoke test only; quality N/A |

WhisperX alignment adds ~200 MB VRAM for the Arabic wav2vec2 model.

Diarization (pyannote) adds ~500 MB VRAM and ~1-2× real-time processing overhead.

Total VRAM for full pipeline (large-v3 + Arabic align + diarization): ~4-5 GB on T4.
The T4's 16 GB is more than enough.

### Latency (T4, large-v3, float16)

| Stage | Time | Notes |
|---|---|---|
| Model load | 10-15 s | First run downloads ~3 GB weights |
| VAD chunking | ~0.1× real-time | Silero is fast |
| Per-chunk transcription | ~0.5× real-time | Batched, 8 chunks at once |
| Alignment (Arabic) | ~0.3× real-time | Per-language pass |
| Diarization | ~1-2× real-time | Full audio, pyannote |
| **Total** | **~2-3× real-time** | 10 min broadcast → 20-30 min |

---

## Alignment model details

WhisperX uses **wav2vec2 CTC models** for forced alignment. These models predict
character-level probabilities for each audio frame (every 20 ms). The alignment
algorithm finds the optimal path through these probabilities that matches the
transcribed text — giving each character (and therefore each word) a precise timestamp.

### Default aligners (automatic)

WhisperX ships with default aligners for: `en, fr, de, es, it, ja, zh, nl, uk, pt`.

For French (`fr`), it automatically selects a CTC wav2vec2 model fine-tuned on French
speech (typically `jonatasgrosman/wav2vec2-large-xlsr-53-french` under the hood).

### Arabic fallback

For Arabic (`ar`), we use `boualin/wav2vec2-large-xlsr-53-arabic`:
- **Base**: `facebook/wav2vec2-large-xlsr-53` (multilingual, 53 languages)
- **Fine-tuned on**: Arabic Common Voice + MGB-2 (broadcast news)
- **License**: BSD-3
- **Tokenizer**: character-level (Arabic script, 28 letters + diacritics + tatweel)
- **Expected accuracy**: Good for MSA, fair for Darija (shares the same Arabic script
  but different vocabulary/pronunciation — the character-level CTC model captures
  phonetic patterns that are similar enough for usable alignment)

### Why no Darija-specific model?

There is no publicly available Darija-specific wav2vec2 model as of 2026. Darija is
an under-resourced dialect. The Arabic model is a reasonable compromise: Darija and
MSA share the same script and most phonemes, so character-level alignment works
despite vocabulary differences.

If alignment fails or degrades quality, it is silently skipped — Whisper's native
segment timestamps (accurate to ~1 s) are used instead. The pipeline never degrades
transcription quality because of alignment.

---

## Diarization model details

Speaker diarization uses **pyannote.audio 3.1** with the
`pyannote/speaker-diarization-community-1` pipeline:

1. **Segmentation**: `pyannote/segmentation-3.0` — a neural model that detects
   speaker changes (trained on over 100 speakers)
2. **Embedding**: `pyannote/embedding` — extracts speaker voice characteristics
   (a voice fingerprint) from each segment
3. **Clustering**: Agglomerative clustering groups segments by voice similarity →
   assigns `SPEAKER_00`, `SPEAKER_01`, etc.

The model is **speaker-agnostic** (it doesn't identify known people) and
**language-agnostic** (it works on voice acoustics, not words).

**Requirements:**
- HuggingFace account
- Accepted terms at the two model pages (one-time)
- Read token passed via `--hf-token` or `HF_TOKEN` env var

**Limitations:**
- Overlapping speech is not handled well (pyannote assigns overlapping segments to
  one speaker, usually the louder one)
- Very short segments (<0.5 s) may not have enough voice data for reliable clustering
- The model assigns arbitrary labels (`SPEAKER_00`, `SPEAKER_01`) that are consistent
  within one audio file but not across files

---

## Comparison: `transcribe.py` vs `transcribe_whisperx.py`

| Feature | `transcribe.py` | `transcribe_whisperx.py` |
|---|---|---|
| Backend | faster-whisper | WhisperX (faster-whisper + wrappers) |
| Dependencies | 1 (`faster-whisper`) | 2 (`whisperx`, `pyannote.audio`) |
| Per-chunk code-switching | ✅ | ✅ (same VAD chunking) |
| Word-level timestamps | ❌ (segment only) | ✅ (wav2vec2 alignment, if available) |
| Speaker diarization | ❌ | ✅ (pyannote, optional) |
| Batched inference | ❌ (per-chunk sequential) | ✅ (batched via WhisperX) |
| Memory usage | ~3 GB (large-v3) | ~4-5 GB (with align + diarize) |
| Arabic alignment | N/A | Fallback community model |
| Installation | `pip install -r requirements.txt` | `pip install -r requirements_whisperx.txt` |
| Use case | Quick, reliable transcription | Production with speaker labels |

**When to use which:**
- Use `transcribe.py` for bulk transcription where you only need text + timestamps
- Use `transcribe_whisperx.py` when you need speaker labels, word-level timestamps,
  or batched throughput

---

## Kaggle notebook

The Kaggle notebook `notebooks/kaggle_whisperx.ipynb` provides a step-by-step
interactive environment for running WhisperX on a T4 GPU:

### Cell structure

| # | Type | What it does |
|---|---|---|
| 1 | markdown | Title + setup instructions |
| 2 | markdown | Section: Install |
| 3 | code | `!pip install whisperx pyannote.audio` |
| 4 | markdown | Section: Mount repo code |
| 5 | code | Auto-find `transcribe_whisperx.py` under `/kaggle/input/` |
| 6 | markdown | Section: HF token setup |
| 7 | code | Paste token + accept pyannote model terms |
| 8 | markdown | Section: Choose a clip |
| 9 | code | Auto-discover media files → set `CLIP` |
| 10 | markdown | Section: Load model |
| 11 | code | `wx.load_model("large-v3", device="cuda", compute_type="float16")` |
| 12 | markdown | Section: Transcribe (no diarization) |
| 13 | code | `wx.transcribe_file()` → print preview + language mix |
| 14 | markdown | Section: Write .srt |
| 15 | code | `write_srt()` + print first 2000 chars |
| 16 | markdown | Section: Verify SRT |
| 17 | code | Regex parse → assert cue count matches |
| 18 | markdown | Section: Run with diarization |
| 19 | code | `wx.transcribe_file(diarize=True, hf_token=HF_TOKEN)` + speaker stats |
| 20 | markdown | Section: Write speaker-labeled .srt |
| 21 | code | `write_srt()` with `[SPEAKER_XX]` outputs |
| 22 | markdown | Section: Download instructions |
| 23 | code | List output files in `/kaggle/working/` |

### Setup checklist

1. **Kaggle Settings**: Accelerator → GPU T4 x2 (or P100). Internet → On
2. **Upload data**: Create a Kaggle Dataset with your `transcription/` folder
   and any media files. Or add media as a separate Dataset
3. **HF token** (for diarization): Set `HF_TOKEN = "hf_..."` in cell 7
4. **Run all cells**: Executes sequentially, outputs appear inline
5. **Download**: Output `.srt` files in `/kaggle/working/` → download from Kaggle
   Output panel

### First run notes

- First cell downloads WhisperX + dependencies (~100 MB)
- Model load downloads large-v3 weights (~3 GB) — takes 10-15 s on T4
- Arabic alignment model download (~500 MB) — happens on first Arabic segment
- Diarization model download (~200 MB) — happens on first `--diarize` run
- All models are cached in `/root/.cache/` for subsequent cells/runs

---

## Troubleshooting

### WhisperX not installed
```
error: whisperx not installed. Run:
  pip install -r requirements_whisperx.txt
```
→ Self-explanatory. The lazy import means this only appears when you actually try to use
  the pipeline, not on `--help`.

### HF token errors (diarization)
```
[diarize] --hf-token required for speaker diarization
```
→ Pass `--hf-token` or the variable is empty.

```
OSError: Unable to load model. Check your HF token and model access.
```
→ Three possible causes:
  1. Token is invalid or expired → regenerate at huggingface.co/settings/tokens
  2. You haven't accepted terms for `pyannote/speaker-diarization-community-1` or
     `pyannote/segmentation-3.0` → accept at the model pages (one-time)
  3. Token doesn't have "read" permission → create a new token with read scope

```
[diarize] failed: CUDA out of memory
```
→ Diarization adds ~500 MB VRAM. Reduce `--batch-size` or use `--compute-type int8`.

### Arabic alignment fails
```
[align] Arabic fallback failed: ...
```
→ The community Arabic wav2vec2 model might not work for all audio conditions.
  The pipeline continues without alignment (Whisper's native timestamps are used).
  If this is consistent, you can try a different Arabic wav2vec2 model by modifying
  `ARABIC_ALIGN_MODEL` at line 49.

### CUDA out of memory
```
torch.cuda.OutOfMemoryError: CUDA out of memory.
```
→ Options (in order of effectiveness):
  1. Reduce `--batch-size` (4 or 2 instead of 8)
  2. Use `--compute-type int8` (lower quality, half the VRAM)
  3. Use a smaller model (`--model medium` or `--model small`)
  4. Disable diarization (omit `--diarize`)
  5. Disable alignment (can't disable via CLI — modify the code to skip alignment)

### No audio detected
```
[vad] clip.mp4: 0 chunk(s)
```
→ The file was decoded but Silero VAD found no speech regions. Possible causes:
  - The file is music-only or very noisy
  - The audio codec is not supported by PyAV
  - The file has a video track but no audio track
  Check that `ffprobe <file>` shows an audio stream. The pipeline forces one chunk
  spanning the whole file in this case, so transcription still runs — but quality
  will be poor if there really is no speech.

### Typing `--hf-token` shows as two separate arguments
```
python src/transcribe_whisperx.py --hf-token hf_xxx
```
This is correct argparse behavior. The double-dash with hyphens is the standard CLI
convention. `argparse` converts `--hf-token` to `args.hf_token`. Do NOT use an
underscore in the CLI flag (`--hf_token` won't be recognized — argparse treats
underscores and hyphens differently in long option names).

---

## File reference

All paths relative to `transcription/`.

| File | Role | Lines |
|---|---|---|
| `src/transcribe_whisperx.py` | WhisperX pipeline (this file) | 435 |
| `src/srt_writer.py` | Shared SRT writer (imported by both pipelines) | 70 |
| `notebooks/kaggle_whisperx.ipynb` | Kaggle GPU runner (WhisperX pipeline) | 23 cells |
| `notebooks/kaggle_compare_darija_models.ipynb` | Kaggle benchmark — compare 3 Darija HF models side-by-side | 14 cells |
| `docs/WHISPERX_GUIDE.md` | This document | — |
| `requirements_whisperx.txt` | `whisperx`, `pyannote.audio`, `pytest` | 3 |

The original `transcribe.py` and `kaggle_transcribe.ipynb` are unchanged.
