# broadcast-srt

Open-source transcription pipeline that automatically generates `.srt` subtitles from
broadcasts in **Moroccan Darija, Modern Standard Arabic, and French** — including files
that **code-switch** between them.

Built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper `large-v3`,
or `large-v3-turbo` for faster inference) with **per-chunk language detection**, so a
single mixed broadcast transcribes natively instead of being forced into one language.

> **Best Darija model:** Benchmarking confirms [`anaszil/whisper-large-v3-turbo-darija`](https://huggingface.co/anaszil/whisper-large-v3-turbo-darija)
> (LoRA adapter on turbo) is 3.4× faster and more accurate than full large-v3 fine-tunes
> on real broadcast audio. See `notebooks/kaggle_compare_darija_models2.ipynb`.

> This is a standalone tool. It produces standard `.srt`, which means its output can feed
> any subtitle consumer — including the separate HACA sentiment/tonality benchmark — but
> the two projects share no code. See [`docs/PIPELINE.md`](docs/PIPELINE.md) for the full
> architecture, code walkthrough, and real output examples.

## Install

```bash
pip install -r requirements.txt
# For GPU (recommended), also install a CUDA build of torch matching your driver, e.g.:
# pip install torch --index-url https://download.pytorch.org/whl/cu121
# For speaker annotation (WhisperX + diarization):
# pip install -r requirements_whisperx.txt
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
| `--allowed`     | `ar,fr,en,es`  | Allow-list for auto detection; off-list → falls back to `ar`.  |
| `--max-chunk-s` | `25`        | Max chunk length (seconds); chunks break only at silence.      |
| `--overwrite`   | off         | Re-transcribe even if the `.srt` already exists.               |

## Batch CLI (medias tree)

`src/transcribe.py` handles a single file or a flat directory. For transcribing a
structured archive, `cli.py` batches over a `medias/` tree and writes a mirrored
`out/srt/` tree, with channel/date/hour filtering and a structured run log. It
shares all transcription logic with the FastAPI backend (the Transcription UI) via
the `core/` package, so the two stay in lock-step.

```
medias/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.mp3   # input
out/srt/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.srt  # output (mirrors input)
```

```bash
# Dry-run: list the matched files, run no models.
python cli.py --channel al-oula --year 2024 --month 6 --hours 9-18 --dry-run

# Transcribe two channels, all of June 2024, with the recommended defaults
# (Darija-LoRA on: Arabic chunks → LoRA, French/English → base model).
python cli.py --channel al-oula,2m --year 2024 --month 6

# Speaker annotation (WhisperX diarization); needs a Hugging Face token.
python cli.py --channel 2m --year 2024 --month 6 --day 1 \
    --speaker-annotation --hf-token hf_xxx
```

Filters (`--channel`, `--year`, `--month`, `--day`, `--hours`) accept lists and/or
ranges (`9-18,21`); omit any to mean "all". `--speaker-annotation` is off by
default. See [`docs/CLI_ARCHITECTURE.md`](docs/CLI_ARCHITECTURE.md) for the full
flag reference, the shared `core/` architecture, and every design decision.

### Docker (GPU)

A GPU image is provided (`Dockerfile.gpu` + `docker-compose.yml`):

```bash
export MEDIAS_DIR=/data/medias
docker compose build
docker compose run --rm transcribe --channel al-oula --year 2024 --month 6
```

Building needs no GPU (only runtime does). See the "Running with Docker (GPU)"
section of [`docs/CLI_ARCHITECTURE.md`](docs/CLI_ARCHITECTURE.md) for building,
pushing to Docker Hub, and running on a remote GPU box.

## Media Ingestion Tools

Three tools in `tools/` handle the ingestion stage that sits *in front of*
transcription — pulling raw audio off YouTube channels and Instagram accounts
before the transcription pipeline processes it.

All three tools share a common helpers module, **`tools/_media_common.py`**,
which provides the filename sanitisation, UTC timestamp formatting, archive I/O,
shared path builder, and the tee logger used by both downloaders. This keeps the
two downloaders consistent: they produce byte-identical on-disk layouts and stamp
filenames the same way.

### `tools/fetch_youtube.py` — YouTube incremental audio downloader

Downloads the audio of every new video from a YouTube channel, incrementally:

```
{out}/{channel}/{year}/{month}/{video_title}.{ext}   # default out: youtube/
```

Only videos not yet in the download archive are fetched. Re-runs are cheap —
new videos are the only ones processed. A filesystem safety net (on-disk check
with archive backfill) keeps the archive consistent even if it is deleted or
files are copied in by hand.

```bash
# New videos since last run
python tools/fetch_youtube.py --url https://www.youtube.com/@SomeChannel

# Bound a large first run; preview with dry-run
python tools/fetch_youtube.py --url https://www.youtube.com/@SomeChannel \
    --max-downloads 5 --since 20260101 --dry-run
```

See [`docs/YOUTUBE_DOWNLOADER.md`](docs/YOUTUBE_DOWNLOADER.md) for the full
architecture, library choices, and a line-by-line code walkthrough.

### `tools/fetch_instagram.py` — Instagram incremental audio downloader

Downloads the audio of every new video post and reel from one or more Instagram
accounts. Uses the same on-disk layout and deduplication strategy as the YouTube
tool:

```
{out}/{account}/{year}/{month}/{caption_title}.{ext}   # default out: instagram/
```

Requires a one-time interactive login (`--login`) to save a session; subsequent
runs load the session silently. instaloader handles rate-limit backoff; ffmpeg
extracts audio from the staged video files.

```bash
# One-time login
python tools/fetch_instagram.py --user YOUR_LOGIN --login

# Fetch new videos from one or more accounts
python tools/fetch_instagram.py --user YOUR_LOGIN \
    --account natgeo --account 2m.ma

# Dry-run preview
python tools/fetch_instagram.py --user YOUR_LOGIN --account natgeo --dry-run
```

See [`docs/INSTAGRAM_DOWNLOADER.md`](docs/INSTAGRAM_DOWNLOADER.md) for the full
architecture, authentication design, and a line-by-line code walkthrough.

### `tools/organize_medias.py` — media tree organiser

Reshuffles a flat or loosely-structured media directory into the canonical
`medias/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.{ext}` tree consumed by
the transcription CLI (`cli.py`). Run this on audio downloaded by the two tools
above to prepare it for batch transcription.

## Recommended workflow

The heaviest, highest-quality runs are meant for a **Kaggle/Colab GPU (T4)** — see
[`notebooks/kaggle_transcribe.ipynb`](notebooks/kaggle_transcribe.ipynb). Local CPU is for
smoke-testing the plumbing, not for quality.

## Tests

```bash
pytest tests/        # SRT-writer + core (config/selection/runner) + CLI + media ingestion tests
```

The `core/` and `cli.py` tests run without a GPU, a model download, or
faster-whisper installed — they use injected fakes for the transcription call.

The media ingestion tests (`test_media_common.py`, `test_fetch_youtube.py`,
`test_fetch_instagram.py`) run without a network connection, yt-dlp, instaloader,
or ffmpeg — they use injected fakes for all I/O.

**178 tests passing** in total across all test files.

## Layout

```
cli.py                         batch CLI over a medias/ tree (filters → mirrored out/srt/)
core/                          shared logic (config, selection, runner, summary) — CLI + UI
src/transcribe.py              pipeline + CLI (decode → VAD → chunk → detect → transcribe → write)
src/transcribe_whisperx.py     alternate pipeline (WhisperX + alignment + diarization)
src/srt_writer.py              standard .srt writer
tools/_media_common.py         shared helpers for both media downloaders (filenames, stamps, archive, logger)
tools/fetch_youtube.py         YouTube incremental audio downloader (yt-dlp)
tools/fetch_instagram.py       Instagram incremental audio downloader (instaloader + ffmpeg)
tools/organize_medias.py       reshuffles a media tree into the canonical YYYYMMDDHHMMSS layout
tools/requirements-youtube.txt pip requirements for fetch_youtube.py
tools/requirements-instagram.txt pip requirements for fetch_instagram.py
Dockerfile.gpu                 GPU image for the batch CLI
docker-compose.yml             convenience wrapper for repeated GPU runs
docs/CLI_ARCHITECTURE.md       batch CLI architecture, code walkthrough, Docker guide
docs/INSTAGRAM_DOWNLOADER.md   Instagram downloader architecture and code walkthrough
docs/PIPELINE.md               comprehensive reference (start here)
docs/TRANSCRIPTION.md          design notes (why faster-whisper, the Darija reality)
docs/WHISPERX_GUIDE.md         WhisperX code walkthrough (deep-dive supplement)
docs/YOUTUBE_DOWNLOADER.md     YouTube downloader architecture and code walkthrough
notebooks/                     Kaggle GPU runners
tests/test_srt_writer.py       SRT writer tests
tests/test_config.py           core config tests
tests/test_selection.py        core selection tests
tests/test_runner.py           core runner tests
tests/test_cli.py              CLI tests
tests/test_media_common.py     shared media helpers tests
tests/test_fetch_youtube.py    YouTube downloader tests (42 tests, FakeYDL)
tests/test_fetch_instagram.py  Instagram downloader tests (41 tests, FakeLoader)
```

## License & limitations

Whisper on Darija is the weakest spot, though the `anaszil/whisper-large-v3-turbo-darija`
LoRA adapter (see notebook comparison) significantly improves it. There is no speaker
diarization in the basic pipeline (use `transcribe_whisperx.py --diarize` for that), and
Whisper's native cue boundaries are kept. See [`docs/TRANSCRIPTION.md`](docs/TRANSCRIPTION.md) §7.
