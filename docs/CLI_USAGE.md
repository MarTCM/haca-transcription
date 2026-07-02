# CLI Usage Guide

This guide provides the command-line usage and reference for all transcription, ingestion, and organization tools available in this repository.

---

## 1. Batch Transcription CLI (`cli.py`)

Processes a structured directory tree of media files and generates mirrored `.srt` subtitle files.

```bash
python cli.py --mode [medias|youtube|tiktok] [options]
```

### Key Options

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `medias` | Ingestion mode layout: `medias`, `youtube`, or `tiktok`. |
| `--medias` | *value of `--mode`* | Root path of the input media tree directory. |
| `--out-dir` | `out/srt` | Output root directory where subtitle files mirror the input tree. |
| `--pipeline` | `faster-whisper` | Core backend: `faster-whisper` or `whisperx`. |
| `--model` | `large-v3` | Model size or local path (e.g. `large-v3`, `large-v3-turbo`, `tiny`). |
| `--darija-model` | `large` | Darija model type: `large` (default, large-v3-turbo LoRA) or `small` (ychafiqui/whisper-small-darija). |
| `--darija-lora` | Enabled | Route Arabic chunks through the Darija LoRA adapter. |
| `--no-darija-lora` | Disabled | Disable the Darija LoRA; transcribe all chunks with base model. |
| `--lang` | `auto` | Forced language code (`ar`, `fr`, `en`, `es`) or `auto` for per-chunk detection. |
| `--allowed` | `ar,fr,en,es` | Comma-separated list of allowed languages for auto-detection. |
| `--speaker-annotation` | Disabled | Enable speaker diarization (requires `--pipeline whisperx` and token). |
| `--hf-token` | None | Hugging Face token for diarization (can also set `$HF_TOKEN`). |
| `--overwrite` | Disabled | Re-transcribe even if the target `.srt` already exists. |
| `--dry-run` | Disabled | List matched files and exit without transcribing. |

### Filters (Omit any to mean "all")

- `--channel`: Channel folder names to include (repeatable and/or comma-separated).
- `--year`: Year filter list/range (e.g. `2024` or `2024-2025`).
- `--month`: Month filter (1-12) list/range (e.g. `1-6` or `1,6,12`).
- `--day`: Day filter (1-31) list/range (e.g. `1,15,30`). *Only supported in `medias` mode.*
- `--hours` (or `--hour`): Hour filter (0-23) list/range (e.g. `9-18,21`). *Only supported in `medias` mode.*

### Examples

```bash
# Preview files in YouTube mode for channel 2MTV from 2026
python cli.py --mode youtube --channel 2MTV --year 2026 --dry-run

# Transcribe standard broadcast media from June 2024 with default settings
python cli.py --channel al-oula,2m --year 2024 --month 6

# Run speaker diarization on a specific day's broadcasts
python cli.py --channel 2m --year 2024 --month 6 --day 1 --speaker-annotation --hf-token hf_xxx
```

---

## 2. Single-File Transcription CLI (`src/transcribe.py`)

Transcribes a single audio/video file or a flat list of files in a directory.

```bash
python src/transcribe.py --input <file_or_dir> [options]
```

### Key Options

| Flag | Default | Description |
|------|---------|-------------|
| `--input` | *(Required)* | Path to a media file, or a flat directory of files. |
| `--out-dir` | `out` | Where to write the `.srt` files. |
| `--model` | `large-v3` | Model size or path. |
| `--darija-model` | `large` | Darija model type: `large` or `small`. |
| `--device` | `auto` | Compute device: `auto`, `cuda`, or `cpu`. |
| `--lang` | `auto` | Forced language code (`ar`, `fr`...) or `auto` for per-chunk detection. |
| `--allowed` | `ar,fr,en,es` | Allowed languages for auto-detection. |
| `--darija-lora` | Disabled | Route Arabic chunks through the Darija LoRA adapter. |
| `--overwrite` | Disabled | Re-transcribe even if target `.srt` exists. |

### Examples

```bash
# Transcribe a single file using default GPU & Darija settings
python src/transcribe.py --input show.mp4 --out-dir out/

# Quick test on CPU with a tiny model
python src/transcribe.py --input clip.wav --model tiny --device cpu
```

---

## 3. Media Ingestion & Downloaders (`tools/`)

Incremental downloaders that fetch audio tracks and save them in the channel-based directory tree format.

### A. YouTube Downloader (`tools/fetch_youtube.py`)

Downloads audio incrementally from a YouTube channel.

```bash
python tools/fetch_youtube.py --url <channel_url> [options]
```

- `--url`: YouTube channel URL (e.g. `https://www.youtube.com/@SomeChannel`).
- `--out`: Output directory (default: `youtube`).
- `--audio-format`: Target audio format (default: `mp3`).
- `--max-downloads`: Cap the number of downloads this run.
- `--scan-limit`: How many of the channel's newest uploads to scan (default: `50`, use `0` for all).
- `--since`: Lower bound date (`YYYYMMDD`) to filter uploads.
- `--dry-run`: List videos that would be downloaded without fetching.
- `--log`: File path to log download operations.

---

### B. TikTok Downloader (`tools/fetch_tiktok.py`)

Downloads audio incrementally from a TikTok account handle.

```bash
python tools/fetch_tiktok.py --account <handle> [options]
```

- `--account`: TikTok handle with or without `@` (repeatable).
- `--out`: Output directory (default: `tiktok`).
- `--audio-format`: Target audio format (default: `mp3`).
- `--max-downloads`: Cap the number of downloads per account.
- `--scan-limit`: How many videos to scan from recent uploads (default: `50`, use `0` for all).
- `--since`: Lower bound date (`YYYYMMDD`) to filter uploads.
- `--cookies-file`: Netscape cookies file path for private/restricted profiles.
- `--dry-run`: List videos that would be downloaded without fetching.
- `--log`: File path to log download operations.

---

### C. Instagram Downloader (`tools/fetch_instagram.py`)

Downloads audio incrementally from Instagram video posts and reels.

```bash
# One-time interactive login to store credentials
python tools/fetch_instagram.py --user YOUR_USERNAME --login

# Download new media using saved session
python tools/fetch_instagram.py --user YOUR_USERNAME --account <account_name> [options]
```

- `--account`: Instagram profile name to fetch (repeatable).
- `--user`: Instagram username used to load or save session.
- `--login`: Prompts password and 2FA to authenticate and save session file.
- `--out`: Output directory (default: `instagram`).
- `--audio-format`: Target audio format (default: `mp3`).
- `--max-downloads`: Cap the number of downloads per account.
- `--scan-limit`: How many posts to scan from recent profile uploads (default: `50`, use `0` for all).
- `--since`: Lower bound date (`YYYYMMDD`) to filter uploads.
- `--dry-run`: List posts that would be downloaded without fetching.
- `--log`: File path to log download operations.

---

## 4. Media Organizer (`tools/organize_medias.py`)

Organizes a flat media directory structure into the chronological directory layout (`{channel}/{year}/{month}/{day}/{filename}`) expected by `cli.py` in `medias` mode.

```bash
python tools/organize_medias.py --medias <root_dir> [options]
```

- `--medias`: Path to the media root folder (containing channel folders).
- `--copy`: Copy files instead of moving them.
- `--dry-run`: Print planned re-locations without executing.

### Examples

```bash
# Dry-run: see where files would be moved
python tools/organize_medias.py --medias /path/to/medias --dry-run

# Move flat files to chronological tree structure
python tools/organize_medias.py --medias /path/to/medias
```
