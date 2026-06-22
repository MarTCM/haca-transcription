# Transcription pipeline — design notes

This project turns a broadcast **audio or video file** into a standard **`.srt`**
subtitle file, for content in **Moroccan Darija, Modern Standard Arabic, and French**
(including files that code-switch between them).

It is the *front half* of a larger media-analysis stack: the back half (the HACA
sentiment/tonality benchmark) already consumes `.srt` and was missing only a way to
*produce* one from raw broadcasts. The two are kept as separate projects on purpose;
they share nothing but the `.srt` file format.

---

## 1. Why faster-whisper / Whisper large-v3

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) is a re-implementation of
OpenAI's Whisper on the CTranslate2 inference engine. We use it because:

- **Multilingual by design.** Whisper covers Arabic (`ar`) and French (`fr`) natively,
  which is exactly our broadcast mix.
- **Fast and light.** CTranslate2 runs `large-v3` on a single T4 (16 GB) in `float16`,
  and degrades gracefully to `int8` on CPU for smoke tests.
- **Batteries included.** It bundles media decoding (PyAV — handles video too) and a
  Silero **VAD** (voice-activity detector), so we don't add an ffmpeg/onnx zoo.
- **Word/segment timestamps** come for free, which is what an `.srt` needs.

We deliberately use the model **as-is** (no fine-tuning) in the base pipeline. However,
benchmarking has identified [`anaszil/whisper-large-v3-turbo-darija`](https://huggingface.co/anaszil/whisper-large-v3-turbo-darija)
(LoRA adapter on `large-v3-turbo`) as the best Darija ASR option — see
`notebooks/kaggle_compare_darija_models2.ipynb`. Use `--darija-lora` to route Arabic
chunks through the adapter (see §8). The turbo model (`large-v3-turbo` via
faster-whisper) is also a drop-in replacement that's faster than `large-v3` with
comparable quality.

## 2. The Darija reality (important)

Whisper has **no dedicated Darija label.** Moroccan Arabic is transcribed under the
generic Arabic code `ar`, in Arabic script. That is acceptable here:

- The script is still correct (Arabic), so the text is usable.
- Downstream, the benchmark's language router (`srt_utils.detect_lang`) keys off
  *script*, and a camel-tools step disambiguates MSA vs Darija — none of which we need
  to solve at transcription time.
- Darija word-error-rate is genuinely higher than for MSA/French. The benchmark already
  expects noisy ASR and has a *garble gate* (`asr_quality.py`) that drops unintelligible
  cues rather than mislabelling them. So imperfect Darija degrades gracefully instead of
  poisoning results.

## 3. Per-chunk language detection (the code-switching trick)

Vanilla Whisper detects **one** language from the first ~30 s and applies it to the
whole file. A HACA broadcast can open in French, switch to Darija, and quote MSA — one
global language is wrong for most of it.

So instead of one detection per file, we detect **per chunk**:

1. **Decode** the file to 16 kHz mono (`decode_audio`).
2. **VAD** the audio (Silero) to get speech regions; silence is discarded.
3. **Group** regions into chunks of at most `--max-chunk-s` (default 25 s), breaking
   *only at silence* so we never cut mid-word. Each chunk carries its absolute start
   time in the original file.
4. **Detect language per chunk** (`model.detect_language`), constrained to an
   **allow-list** (`--allowed`, default `ar,fr,en`). Anything outside it falls back to
   `ar` — this stops noisy Darija from being mis-detected as, say, Persian or Urdu.
5. **Transcribe** each chunk in its detected language, with
   `condition_on_previous_text=False` so a hallucination in one chunk can't snowball
   into the next.
6. Segment timestamps are offset by the chunk's absolute start and collected.
7. **Write** a standard `.srt`.

Force a single language with `--lang ar` (or `fr`) to skip step 4 entirely.

```
audio ─▶ decode 16k mono ─▶ VAD ─▶ ~25s chunks ─▶ [detect lang]─▶ transcribe ─▶ .srt
                                     (break at silence)  per chunk    per chunk
```

## 4. Output contract

A standard SubRip file (`src/srt_writer.py`):

- 1-based integer index per cue
- `HH:MM:SS,mmm --> HH:MM:SS,mmm` (comma decimal)
- blocks separated by a blank line, UTF-8

This is exactly what `srt_utils.parse_srt` in the benchmark expects, so the output is
interoperable without either project importing the other. (The round-trip is unit-tested
in `tests/test_srt_writer.py`.)

## 5. Compute

| Target              | device | compute_type | model      | notes                          |
|---------------------|--------|--------------|------------|--------------------------------|
| Kaggle/Colab T4     | cuda   | float16      | large-v3   | primary path, best quality     |
| Local CPU smoke     | cpu    | int8         | tiny/base  | proves plumbing; quality N/A   |

`--device auto` picks `cuda` when a GPU is visible (probed via torch or ctranslate2),
else `cpu`. `--compute-type` overrides the per-device default.

## 6. Usage

```bash
# Single file, GPU, per-chunk language auto-detection
python src/transcribe.py --input show.mp4 --out-dir out/

# Batch a directory
python src/transcribe.py --input broadcasts/ --out-dir out/

# Force French; smaller model on CPU for a quick local check
python src/transcribe.py --input clip.wav --model tiny --device cpu --lang fr

# Best Darija quality with the anaszil LoRA adapter (requires transformers+peft)
python src/transcribe.py --input show.mp4 --out-dir out/ --darija-lora

# Same for WhisperX pipeline
python src/transcribe_whisperx.py --input show.mp4 --out-dir out/ --darija-lora
```

## 8. LoRA adapter for Darija (`--darija-lora`)

Use the [`anaszil/whisper-large-v3-turbo-darija`](https://huggingface.co/anaszil/whisper-large-v3-turbo-darija)
adapter to improve Darija recognition. Benchmark results show it is **3.4× faster** than
the MaghrebVoice fine-tune and produces cleaner transcriptions.

**How it works:**

1. `--darija-lora` loads the LoRA on top of `openai/whisper-large-v3-turbo` using
   HuggingFace `transformers` + `PEFT`.
2. During per-chunk transcription, any chunk detected as Arabic (`lang == "ar"`) is
   routed through the LoRA pipeline instead of faster-whisper / WhisperX.
3. French/English chunks continue through the original engine (CTranslate2).

**Why not always use the LoRA?** It runs in PyTorch (HF pipeline), which is slower per
token than CTranslate2. French/English quality is identical to the base turbo model, so
we only pay the LoRA cost where it matters — Arabic.

**Requirements:**
```bash
pip install transformers peft
```

**CLI flags:**
```
--darija-lora                          enable routing
--lora-model anaszil/...-darija        adapter path (default)
--lora-base openai/...-turbo           base model (default)
```

## 7. Limitations / future work

- **Darija WER** is the weakest point (no Darija-specific model). Could be improved later
  by swapping in a fine-tuned Whisper/wav2vec2 Darija checkpoint behind the same CLI.
- **No speaker diarization.** Cues are not labelled by speaker. Adding it means moving to
  WhisperX + pyannote (and an HF token).
- **No subtitle re-segmentation.** We keep Whisper's native segment boundaries; we do not
  re-flow long cues to a strict characters-per-line limit (that needs word timestamps to
  keep timing honest). Fine for downstream NLP; a human subtitler may want tighter cues.
- **Music / overlapping speech / heavy noise** can still produce hallucinated or empty
  cues; the downstream garble gate is the safety net.
