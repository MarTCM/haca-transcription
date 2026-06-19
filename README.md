# broadcast-srt

Open-source transcription pipeline that automatically generates `.srt` subtitles from
broadcasts in **Moroccan Darija, Modern Standard Arabic, and French** — including files
that **code-switch** between them.

Built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper `large-v3`)
with **per-chunk language detection**, so a single mixed broadcast transcribes natively
instead of being forced into one language.

> This is a standalone tool. It produces standard `.srt`, which means its output can feed
> any subtitle consumer — including the separate HACA sentiment/tonality benchmark — but
> the two projects share no code. See [`docs/TRANSCRIPTION.md`](docs/TRANSCRIPTION.md) for
> the full design.

## Install

```bash
pip install -r requirements.txt
# For GPU (recommended), also install a CUDA build of torch matching your driver, e.g.:
# pip install torch --index-url https://download.pytorch.org/whl/cu121
```

faster-whisper bundles media decoding (PyAV, handles video) and the Silero VAD, so no
separate ffmpeg/onnx install is required for common formats.

## Quickstart

```bash
# Single file — GPU auto-detected, per-chunk language auto-detection, large-v3
python src/transcribe.py --input show.mp4 --out-dir out/

# Batch a whole directory of broadcasts
python src/transcribe.py --input broadcasts/ --out-dir out/

# Quick local smoke test (no GPU): tiny model on CPU
python src/transcribe.py --input clip.wav --model tiny --device cpu

# Force a single language (skip per-chunk detection)
python src/transcribe.py --input journal.mp3 --lang ar --out-dir out/
```

### Key options

| Flag            | Default     | Meaning                                                        |
|-----------------|-------------|----------------------------------------------------------------|
| `--input`       | (required)  | A media file, or a directory to batch-process.                 |
| `--out-dir`     | `out`       | Where `.srt` files are written (`<name>.srt`).                  |
| `--model`       | `large-v3`  | faster-whisper model size or local path.                       |
| `--device`      | `auto`      | `auto` / `cuda` / `cpu`.                                        |
| `--lang`        | `auto`      | `auto` = per-chunk detection; or force a code (`ar`, `fr`...).  |
| `--allowed`     | `ar,fr,en`  | Allow-list for auto detection; off-list → falls back to `ar`.  |
| `--max-chunk-s` | `25`        | Max chunk length (seconds); chunks break only at silence.      |
| `--overwrite`   | off         | Re-transcribe even if the `.srt` already exists.               |

## Recommended workflow

The heaviest, highest-quality runs are meant for a **Kaggle/Colab GPU (T4)** — see
[`notebooks/kaggle_transcribe.ipynb`](notebooks/kaggle_transcribe.ipynb). Local CPU is for
smoke-testing the plumbing, not for quality.

## Tests

```bash
pytest tests/        # SRT-writer format / round-trip tests (no model or audio needed)
```

## Layout

```
src/transcribe.py      pipeline + CLI (decode → VAD → chunk → detect → transcribe → write)
src/srt_writer.py      standard .srt writer
docs/TRANSCRIPTION.md  design notes (why faster-whisper, the Darija reality, limits)
notebooks/             Kaggle GPU runner
tests/                 SRT-writer unit tests
```

## License & limitations

Uses Whisper `large-v3` as-is — Darija word-error-rate is the weakest spot (no
Darija-specific model yet), there is no speaker diarization, and Whisper's native cue
boundaries are kept. See [`docs/TRANSCRIPTION.md`](docs/TRANSCRIPTION.md) §7.
