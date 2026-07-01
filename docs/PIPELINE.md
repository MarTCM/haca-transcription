# Pipeline — end-to-end reference

## What this project does

A broadcast **audio or video file** (`.mp3`, `.wav`, `.mp4`, `.mkv`, `.ts`...) in
**Moroccan Darija, Modern Standard Arabic, or French** — including files that
arbitrarily **code-switch** between these languages — is turned into a standard
**`.srt` subtitle file**.

Output is a list of cues, each with:
- a sequential integer index
- `HH:MM:SS,mmm --> HH:MM:SS,mmm` time range
- the transcribed text (in Arabic or Latin script)

This `.srt` is the input format consumed by the downstream HACA sentiment/tonality
benchmark (a separate project). The two projects share nothing but the file format.

---

## Architecture

Two independent Python scripts under `src/`, sharing only the SRT writer:

```
src/
├── transcribe.py          # Pipeline A: faster-whisper (CTranslate2), lighter deps
├── transcribe_whisperx.py # Pipeline B: WhisperX (adds alignment + diarization)
└── srt_writer.py          # Shared .srt output code
```

**Pipeline A** — `transcribe.py` is the simpler, dependency-lighter default.
Dependencies: `faster-whisper` (which bundles PyAV for media decoding, Silero VAD
for speech detection, and CTranslate2 for fast inference). Perfect for:
- Straightforward transcription of any broadcast
- Batch-processing directories
- Local CPU smoke tests
- Production on a T4 GPU

**Pipeline B** — `transcribe_whisperx.py` wraps the same audio pipeline (decoding,
VAD, chunking, per-chunk language detection) inside the WhisperX library, which
adds two optional stages:
1. **Word-level alignment** via wav2vec2 forced alignment (sub-100 ms precision)
2. **Speaker diarization** via pyannote.audio (labels each cue `[SPEAKER_00]`)

Both scripts accept the same core flags (`--input`, `--out-dir`, `--model`,
`--lang`, `--allowed`, `--darija-lora`...). WhisperX adds `--diarize`,
`--hf-token`, `--batch-size`, `--min-speakers`, `--max-speakers`.

**Why two scripts instead of one?** WhisperX pulls in significantly more
dependencies (`pyannote.audio` requires PyTorch + ONNX + its own pipelines).
Keeping the simpler path separate means a user who only needs `.srt` output
doesn't install 500 MB of speaker-labelling models they won't use.

**What they share:**
- The per-chunk audio pipeline (decode → VAD → chunk)
- The per-chunk language detection logic
- The LoRA adapter helpers (`--darija-lora`)
- The SRT writer (`srt_writer.write_srt()`)
- Every media format the decoders can handle

---

## The shared audio pipeline (steps 1–3)

Both scripts follow these first three steps identically.

### Step 1: Decode to 16 kHz mono

```python
def decode_audio(path: str):
    from faster_whisper import decode_audio as _decode
    return _decode(path, sampling_rate=SAMPLE_RATE)   # SAMPLE_RATE = 16000
```

This uses PyAV (ffmpeg bindings, bundled with faster-whisper) to open any
container, mix to mono, and resample to 16 kHz. The output is a 1-D `float32`
numpy array (`[sample_0, sample_1, ...]`).

**Supported formats:** `.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`, `.opus`, `.aac`,
`.wma`, `.mp4`, `.mkv`, `.mka`, `.mov`, `.webm`, `.avi`, `.ts`, `.m4v` — and
anything else ffmpeg can decode.

### Step 2: VAD (voice-activity detection)

```python
def vad_chunks(audio, max_chunk_s: float = 25.0) -> List[Dict]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    max_samples = int(max_chunk_s * SAMPLE_RATE)
    vad_opts = VadOptions(max_speech_duration_s=max_chunk_s)
    speech = get_speech_timestamps(audio, vad_options=vad_opts, sampling_rate=SAMPLE_RATE)
```

**Silero VAD** (a small neural network trained to detect human speech) scans the
audio and returns a list of `{start: sample_idx, end: sample_idx}` regions where
speech is present. The key parameter `max_speech_duration_s` caps each VAD region
at the chunk budget — continuous speech gets split at natural pause points.

A fallback: if VAD returns nothing (pure music, over-aggressive threshold), the
entire file is treated as a single chunk so no audio is silently dropped.

### Step 3: Group into chunks

```python
    # Merge adjacent regions as long as they fit within the budget.
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

Speech regions from VAD are merged into chunks of at most `--max-chunk-s`
(25 s default), breaking **only at silence** so we never cut mid-word. Each chunk
carries its absolute start/end time in the original file plus the audio slice.

**Why chunk?** Whisper has a ~30 s context window. Chunking ensures each segment
fits comfortably within that window. More importantly, chunking enables per-chunk
language detection (step 4), which is the key to handling code-switched broadcasts.

```
audio ──▶ decode 16k mono ──▶ VAD ──▶ merge into ~25 s chunks
                                    (break at silence)
```

---

## Step 4: Per-chunk language detection

**This is the core innovation** that makes code-switching work.

Vanilla Whisper detects **one** language from the first ~30 s and uses it for the
whole file. A HACA broadcast can open in French, switch to Darija, and quote MSA
— one global language is wrong for most of it.

Instead, we detect **per chunk**:

```python
def detect_chunk_language(model, chunk_audio, allowed=("ar", "fr", "en"),
                          fallback="ar") -> str:
    try:
        lang, prob, _all = model.detect_language(chunk_audio)
    except AttributeError:
        return "auto"
    return lang if lang in allowed else fallback
```

`model.detect_language()` runs the Whisper encoder on the chunk audio and returns
the most probable language. We constrain this to an **allow-list** (`--allowed`,
default `ar,fr,en`). Anything outside — noisy Darija could otherwise be
mis-detected as Persian (`fa`) or Urdu (`ur`) — is silently mapped to `ar`.

**Force a single language** by passing `--lang ar` or `--lang fr`; per-chunk
detection is skipped entirely.

How this looks in `transcribe_file()`:

```python
def transcribe_file(path, model, lang="auto", allowed=DEFAULT_ALLOWED,
                    max_chunk_s=25.0, beam_size=5, ...):
    audio = decode_audio(path)
    chunks = vad_chunks(audio, max_chunk_s=max_chunk_s)

    segments = []
    for ci, chunk in enumerate(chunks):
        if lang == "auto":
            chunk_lang = detect_chunk_language(model, chunk["audio"], allowed)
        else:
            chunk_lang = lang

        # ... then transcribe this chunk (see below) ...
```

---

## Pipeline A: `transcribe.py` (faster-whisper)

### Model loading

```python
def load_model(model_size="large-v3", device=None, compute_type=None):
    from faster_whisper import WhisperModel

    if device in (None, "auto"):
        device = _auto_device()
    if compute_type is None:
        compute_type = "float16" if device == "cuda" else "int8"

    return WhisperModel(model_size, device=device, compute_type=compute_type)
```

`_auto_device()` probes for CUDA via `torch.cuda.is_available()` first, then via
`ctranslate2.get_cuda_device_count()` as a fallback (for environments where
faster-whisper is installed but torch is not).

`compute_type` defaults differ per device:
- `cuda` → `float16` (fast, ~3 GB VRAM for large-v3)
- `cpu`  → `int8` (slower but memory-efficient)

### Chunk transcription

After language detection, each chunk is transcribed:

```python
        whisper_lang = None if chunk_lang == "auto" else chunk_lang
        result, info = model.transcribe(
            chunk["audio"],
            language=whisper_lang,
            beam_size=beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
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
```

Key points:
- **`beam_size=5`** — larger = better quality but slower. 5 is a good balance.
- **`vad_filter=False`** — not needed because we already did VAD ourselves.
- **`condition_on_previous_text=False`** — prevents a hallucination in one chunk
  from bleeding into the next.
- Segment timestamps are relative to the chunk; we offset them by the chunk's
  absolute start time in the original file.

### Writing the SRT

```python
write_srt(segments, srt_path)
```

The shared `srt_writer.py` produces a standard SubRip file:
- 1-based integer index per cue
- `HH:MM:SS,mmm --> HH:MM:SS,mmm` (comma decimal, compatible with every video player)
- Blocks separated by a blank line, UTF-8 encoding

### CLI flow (`main()`)

```
1. Parse args (--input, --out-dir, --model, ...)
2. Gather input files (single file or scan directory for MEDIA_EXTS)
3. Skip any file whose .srt already exists (unless --overwrite)
4. Load model (the heavy step — first run downloads ~3 GB)
5. Optionally load LoRA adapter (if --darija-lora)
6. For each file: transcribe_file() → write_srt()
7. Return 0
```

---

## Pipeline B: `transcribe_whisperx.py` (WhisperX)

This script uses the same per-chunk pipeline as Pipeline A but wraps it inside
WhisperX, which adds two optional post-processing stages.

### What is WhisperX?

WhisperX is a wrapper around faster-whisper that:
1. Runs the same CTranslate2 model for transcription
2. Feeds the transcribed text + audio to a **wav2vec2 forced-aligner** for
   word-level timestamps (sub-100 ms accuracy instead of Whisper's ~1 s segments)
3. Optionally runs **pyannote.audio speaker diarization** to label who spoke when

### Stage 6 (optional): wav2vec2 alignment

```python
# After all chunks are transcribed and merged:
align_model, metadata = _load_align_model(lang, device)
result_aligned = whisperx.align(
    segments, align_model, metadata, audio, device,
    return_char_alignments=False,
)
```

Each language gets a different aligner:
| Language | Aligner model | Source |
|----------|---------------|--------|
| French   | `ctc-wav2vec2-french` | WhisperX built-in |
| Arabic   | `boualin/wav2vec2-large-xlsr-53-arabic` | HuggingFace community (BSD-3) |
| Other    | (skipped) | Falls back to Whisper's native timestamps |

The aligned segments replace the original coarse timestamps with word-level
boundaries. If alignment fails for any language, the Whisper timestamps are kept.

### Stage 7 (optional): pyannote diarization

```python
diarize_model = whisperx.DiarizationPipeline(use_auth_token=hf_token, device=device)
diar_segments = diarize_model(audio, min_speakers=min_speakers, max_speakers=max_speakers)
result_segments = whisperx.assign_word_speakers(diar_segments, result_aligned)
```

Pyannote processes the full audio (not per chunk) to identify distinct speakers.
It assigns each word from the alignment stage a speaker label (`SPEAKER_00`,
`SPEAKER_01`, ...). Words from the same speaker are merged into single cues.

**Requirements:**
- A HuggingFace **read token** (set `--hf-token`)
- Accepting usage terms for the gated pyannote models:
  - `pyannote/speaker-diarization-community-1`
  - `pyannote/segmentation-3.0`

### Extra CLI flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--batch-size` | 8 | Chunks processed in parallel on GPU |
| `--diarize` | off | Enable pyannote speaker labeling |
| `--hf-token` | None | HuggingFace read token for gated models |
| `--min-speakers` | None | Hint for diarization (floor) |
| `--max-speakers` | None | Hint for diarization (ceiling) |

> **Full code walkthrough:** See [`docs/WHISPERX_GUIDE.md`](WHISPERX_GUIDE.md) for a
> line-by-line annotated tour of every function in the WhisperX pipeline.

---

## Step 5 (variant): Darija LoRA adapter (`--darija-lora`)

Both pipelines support the **anaszil LoRA adapter** for improved Darija recognition.
When `--darija-lora` is passed, the chunk loop branches:

```
if chunk_lang == "ar":
    → route through HuggingFace transformers + PEFT pipeline
else:
    → use the normal CTranslate2 engine (faster-whisper / WhisperX)
```

```python
if darija_lora and chunk_lang == "ar":
    lora_segs = _transcribe_with_lora(chunk["audio"], lora_pipe, chunk["start"], "ar")
    segments.extend(lora_segs)
    continue   # skip the normal transcribe() call
```

### Loading the adapter

```python
def _load_darija_lora(lora_model, lora_base, device):
    base = WhisperForConditionalGeneration.from_pretrained(lora_base, ...)
    model = PeftModel.from_pretrained(base, lora_model)
    processor = WhisperProcessor.from_pretrained(lora_base, ...)
    pipe = pipeline("automatic-speech-recognition",
                    model=model, tokenizer=..., feature_extractor=...)
    return pipe
```

This loads:
- `openai/whisper-large-v3-turbo` as the base model (in `float16` on GPU)
- The `anaszil/whisper-large-v3-turbo-darija` LoRA weights on top
- A HuggingFace `pipeline` that handles chunking, inference, and timestamping

The pipe is cached as a module-level singleton so it's loaded at most once.

### Why not always use the LoRA?

The LoRA runs in **PyTorch** (HF pipeline), which is slower per token than
**CTranslate2** (faster-whisper's inference engine). French and English quality
is identical between the two. The hybrid approach — Arabic chunks via LoRA,
everything else via CTranslate2 — gives the best of both worlds.

### LoRA + speaker labels (the `words` field)

LoRA segments need word-level timestamps for WhisperX's alignment + diarization
pipeline to assign speakers. The helper `_words_from_segment()` creates
approximate word timestamps by splitting the segment duration evenly across
words. These are refined by the wav2vec2 aligner and used by `assign_word_speakers`
to match speaker segments.

Without this field, Arabic chunks routed through the LoRA would not get
`[SPEAKER_XX]` labels even when diarization is enabled with a valid token.

### Requirements

```bash
pip install transformers peft
```

### Benchmark results

| Model | Time (1h audio) | WER | Notes |
|-------|----------------|-----|-------|
| `anaszil/...-darija` (LoRA) | ~20 s | ~25% | **Fastest, cleanest** |
| `MaghrebVoice/...-large-v3` | ~70 s | ~26% | Full fine-tune, 3.4× slower |
| Base `large-v3-turbo` | ~15 s | ~28% | No Darija specialisation |

(Benchmark run in `notebooks/kaggle_compare_darija_models2.ipynb` on a T4 GPU,
real broadcast audio.)

---

## Real output examples

Below are the actual SRTs produced by the WhisperX pipeline running on a
[T4 GPU on Kaggle](https://kaggle.com/) with the trimmed broadcast sample
[`samples/mobachara_ma3akom_trimmed.mp3`](../samples/).

The clip is a Moroccan political talk show about the 2026 Finance Law, with
4 guests and a host discussing regional development disparities ("المغرب بسرعتين"
— two-speed Morocco).

### Plain transcription (51 cues, ~10:29)

```
 1
 00:00:02,695 --> 00:00:25,924
 السلام عليكم ورحبا بكم مشاهدات ومشاهدين القناة الثانية في حلقة جديدة من برنامج
 مباشرة معكم وضعنا لهذا المساء ونقاش ردان وواصل فيه وتابعة بعض النقاط المشروع
 قانون المالية التي تناقش حاليا في البرنامج اليوم ستناولون زاوية الفروقات
 المجالية التي تعرفها العالية من جهة المملكة وليه حد

 2
 00:00:26,527 --> 00:00:49,342
 هذا مشروع قانون المالية يجب على السؤال الكبير للمغرب الصراعتين مغرب التفاوتات
 المجالية في التنمية اللي كان يناقش عليه خلال الأشهر الأخيرة شنال أبرز الإجراءات
 التدابير والسياسيات التي يحملها المشروع في أفق تنمية مجالية عادلة وخصوصا توفر
 تنمية عادلة لفيا فور الشول خصوصا للفئات الشابة

 8
 00:02:42,467 --> 00:03:04,335
 دبا مشروع القانون 2026 جابوا أحد المعطيات اللي جريئة جدا جريئة من حيث الاستثمار
 العمومي 380 مليار درهم إجراءات في قطاع الصحة والتعليم 140 مليار درهم 38 ألف
 منصب مالي

 40
 00:07:56,275 --> 00:08:02,059
 فين مشات خمسين مليار؟ فين هو تقييم ديالها؟ فين هي الحاصلة ديالها؟ فين هو هذا الصندوق؟.
```

**Observations:**
- **All Arabic script** (Darija + MSA) — no French in this particular clip, despite
  code-switching being common in Moroccan media. This is because formal political
  debate stays mostly in Arabic.
- **Content is coherent** — topics, numbers, names, and political arguments are
  correctly captured ("380 مليار درهم", "كأس إفريقيا 2026", "كأس العالم 2030").
- **Typical Darija errors** are present but minor: "شنال" instead of "شنو هي", some
  missing diacritics/characters. Nothing that impedes understanding.
- **Diarization was NOT active** — the `HF_TOKEN` was empty, so pyannote was skipped.
  The plain and diarized outputs are identical.

### With diarization (same clip, same 51 cues)

The `_diarized.srt` file is **identical** to the plain one — no `[SPEAKER_XX]`
prefixes. This confirms that without a valid `HF_TOKEN`, the pyannote diarization
step is silently skipped and WhisperX falls back to plain transcription.

To get speaker labels:
1. Set `HF_TOKEN` to a valid HuggingFace read token (see notebook cell 3)
2. Accept terms at `pyannote/speaker-diarization-community-1` and
   `pyannote/segmentation-3.0`
3. Pass `--diarize`

### What outputs are in `out/`

```
out/
├── mobachara_ma3akom_trimmed_whisperx.srt           # plain transcription
├── mobachara_ma3akom_trimmed_whisperx_diarized.srt   # same (HF_TOKEN was empty)
├── jamel_debbouze_darija.srt                          # earlier run on comedy clip
└── benchmark_results.json                             # model comparison data
```

---

## How to run

### Locally (CPU)

```bash
# Install base pipeline
pip install -r requirements.txt

# Single file
python src/transcribe.py --input samples/mobachara_ma3akom_trimmed.mp3 --out-dir out/

# For WhisperX, also install:
pip install -r requirements_whisperx.txt
python src/transcribe_whisperx.py --input samples/mobachara_ma3akom_trimmed.mp3 --out-dir out/
```

CPU works for smoke tests with `--model tiny` or `--model base`. For real
broadcasts, you need a GPU.

### On Kaggle (T4 GPU, recommended)

Open one of:
- [`notebooks/kaggle_transcribe.ipynb`](../notebooks/kaggle_transcribe.ipynb) —
  Pipeline A (faster-whisper)
- [`notebooks/kaggle_whisperx.ipynb`](../notebooks/kaggle_whisperx.ipynb) —
  Pipeline B (WhisperX, alignment, optional diarization)
- [`notebooks/kaggle_compare_darija_models.ipynb`](../notebooks/kaggle_compare_darija_models.ipynb) —
  A/B benchmark (used to produce the recommendation)
- [`notebooks/kaggle_ab_darija.ipynb`](../notebooks/kaggle_ab_darija.ipynb) —
  Base vs LoRA comparison

All notebooks auto-clone the repo — no manual dataset upload needed. Set
accelerator to **GPU T4 x2** and turn Internet **On**.

### With the LoRA adapter

```bash
pip install transformers peft
python src/transcribe.py --input show.mp4 --out-dir out/ --darija-lora
```

---

## FAQ

**Q: Why is Darija labeled as "Arabic"?**

Whisper has no dedicated Darija language code. Moroccan Arabic is transcribed
under the generic `ar` label, in Arabic script. The transcription is still
correct and usable.

**Q: What if the language is detected wrong?**

Constrain the allow-list with `--allowed ar,fr,en`. Anything outside falls back
to `ar`. To force a single language for the whole file, use `--lang ar` or
`--lang fr`.

**Q: Why do the plain and diarized SRTs look identical?**

Diarization requires a valid HuggingFace read token (`HF_TOKEN`). Without one,
pyannote is skipped and WhisperX outputs plain transcription. Set the token in
notebook cell 3 and accept the model terms.

**Q: Why are there no speaker labels?**

You must pass `--diarize` AND a valid `--hf-token`. Diarization is off by
default because it requires acceptance of gated model terms and a HuggingFace
account.

**Q: Why two scripts instead of one with optional features?**

`pyannote.audio` pulls in hundreds of megabytes of dependencies (PyTorch, ONNX,
diarization pipelines). Users who only need `.srt` output shouldn't have to
install or wait for all that.

**Q: Why not always use the LoRA adapter?**

The LoRA runs in PyTorch (HF pipeline), which is slower per token than
CTranslate2 (faster-whisper). French and English quality is identical with or
without it. The `--darija-lora` flag only routes Arabic chunks through the
adapter, so you only pay the cost where it helps.

**Q: Why LoRA instead of a full fine-tune?**

Our benchmark shows the anaszil LoRA adapter is **3.4× faster** than a full
`large-v3` fine-tune (MaghrebVoice) while achieving comparable WER. LoRA adapters
are also much smaller (~50 MB vs ~3 GB) and can be swapped without downloading a
new base model.

**Q: Can I use my own audio files?**

Yes. Upload via Kaggle's **Add Data** → Upload, or pass a local file path to
`--input`. The pipeline accepts any format ffmpeg can decode (mp3, wav, mp4,
mkv, ts...).

**Q: The output has very long cues. Can I split them?**

Not currently. We keep Whisper's native segment boundaries. True subtitle
re-segmentation requires word-level timestamps and a cue-length budget —
something a future version or a downstream tool can add.

**Q: What about music, noise, or overlapping speech?**

Silero VAD filters silence and non-speech. Music and heavy noise can still
produce hallucinated or empty cues. The downstream HACA benchmark has a "garble
gate" (`asr_quality.py`) that drops unintelligible cues rather than mislabelling
them.

**Q: How do I check which languages were detected?**

Each segment dict in the output has a `"lang"` key — the per-chunk language code.
The `srt_writer` doesn't print it to the SRT, but you can inspect it programmatically
or use the per-language cue counter in the notebook:

```python
from collections import Counter
print(Counter(s["lang"] for s in segments))
```

**Q: What's the difference between `large-v3` and `large-v3-turbo`?**

Turbo is a distilled version of large-v3: comparable quality on most benchmarks,
but ~2× faster and smaller. It's the default model for the LoRA adapter and is
recommended for all workflows.

**Q: Can I use a local model path instead of a HuggingFace name?**

Yes. `--model` accepts any local path to a faster-whisper CTranslate2 model
directory, or a HuggingFace model ID that faster-whisper can resolve. Same for
`--lora-model` and `--lora-base`.

---

## Media Ingestion Pipeline (fetch_youtube / fetch_instagram)

Before the transcription pipeline can run, raw audio must be obtained from the
broadcast sources. Two tools in `tools/` handle this ingestion stage, sitting
*in front of* the transcription pipeline described above.

### Overview

```
YouTube channel  ──► fetch_youtube.py  ──► youtube/{channel}/{YYYY}/{MM}/{title}.mp3
                                                │
Instagram account ──► fetch_instagram.py ──► instagram/{account}/{YYYY}/{MM}/{title}.mp3
                                                │
                                                ▼
                                  organize_medias.py (optional)
                                                │
                                                ▼
                            medias/{channel}/{year}/{month}/{day}/{stamp}.mp3
                                                │
                                                ▼
                                    cli.py → transcription pipeline
```

### `tools/fetch_youtube.py`

Downloads the audio of every new video from a YouTube channel, incrementally.
Uses **yt-dlp** (Python API) for channel listing and download, with
`FFmpegExtractAudio` for audio extraction. On-disk layout:

```
youtube/{channel}/{YYYY}/{MM}/{video_title}.{ext}
```

Each run only downloads videos not already in the download archive
(`youtube/.download-archive.txt`). A filesystem safety net backfills the archive
from existing files, so the archive self-heals if deleted or if files are copied
in by hand. The `--scan-limit` (default 50) bounds how many of the newest
uploads are examined per run, keeping large channels fast. Dry-run, `--since`,
and `--max-downloads` flags give fine-grained control.

### `tools/fetch_instagram.py`

Downloads the audio of every new video post and reel from one or more Instagram
accounts. Uses **instaloader** for profile iteration and video download, and
**ffmpeg** for audio extraction from staged video files. On-disk layout:

```
instagram/{account}/{YYYY}/{MM}/{caption_title}.{ext}
```

Requires a one-time interactive login (`--login`) to save a session file; all
subsequent runs load the session silently. The same incremental deduplication
strategy (download archive + on-disk safety net), `--scan-limit`, `--since`,
`--max-downloads`, and dry-run support are identical to the YouTube tool.

### Shared foundation: `tools/_media_common.py`

Both tools import from **`tools/_media_common.py`**, which provides:

- `slugify_channel` / `sanitize_filename` — filesystem-safe name helpers
- `stamp_from_datetime` — formats any datetime as a 14-digit UTC `YYYYMMDDHHMMSS`
- `dest_for` — shared path builder: `{out}/{account}/{YYYY}/{MM}/{title}.{ext}`
- `load_archive` / `append_archive` — archive I/O
- `make_logger` — tee logger

All three tools produce byte-identical on-disk layouts.

### `tools/fetch_tiktok.py`

Downloads the audio of every new video from one or more TikTok accounts. Same
yt-dlp engine as the YouTube tool. Key differences:

- **No login required** for public accounts. Private/geo-restricted accounts:
  `--cookies-file <file.txt>` (Netscape-format cookies exported from a browser).
- **Single-phase listing** — TikTok's yt-dlp flat extractor already returns
  `timestamp`, `title`, and `uploader` in the listing, so no per-video
  `extract_info` call is needed. Each new video costs exactly one yt-dlp call
  (the download), not two.
- **No JS runtime** — TikTok extraction does not use the EJS challenge system;
  no deno or `yt-dlp-ejs` needed.
- **Handle normalisation** — `normalize_handle` strips `@` and lowercases, so
  `@2MMaroc` and `2mmaroc` both map to the same folder.

```
tiktok/{handle}/{year}/{month}/{title}.mp3
tiktok/.download-archive.txt
```

See [`docs/TIKTOK_DOWNLOADER.md`](TIKTOK_DOWNLOADER.md) for the full walkthrough.

### Further reading

- [`docs/YOUTUBE_DOWNLOADER.md`](YOUTUBE_DOWNLOADER.md) — YouTube tool.
- [`docs/INSTAGRAM_DOWNLOADER.md`](INSTAGRAM_DOWNLOADER.md) — Instagram tool.
- [`docs/TIKTOK_DOWNLOADER.md`](TIKTOK_DOWNLOADER.md) — TikTok tool.
