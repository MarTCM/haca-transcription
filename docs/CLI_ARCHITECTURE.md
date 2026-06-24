# Batch Transcription CLI — Architecture & Codebase Guide

This document explains the batch transcription CLI end to end: what it does, how
it is structured, every module and function, the design decisions, and everything
you need to understand and extend the codebase.

> Companion docs: `CLI_PLAN.md` (the implementation plan this was built from),
> `README.md` (the transcription pipeline), and `transcription-ui/PLAN.md` (the
> web UI that shares the same `core/` package).

---

## 1. What it is

A command-line tool that batch-transcribes broadcasts (audio or video — see
`MEDIA_EXTS`) from a `medias/`
directory tree into `.srt` subtitle files. You point it at the tree, filter the
subset you want (by channel / year / month / day / hour, individually or "all"),
and it transcribes each matching file — routing Moroccan Darija through a
specialized LoRA model while French/English stay on the base Whisper model, all
within the same file.

It is **stateless** (no database): output is the `.srt` files, a run log, and a
console summary.

### Media & output layout

```
medias/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.mp3   # input
out/srt/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.srt  # output (mirrors input)
out/logs/cli-<timestamp>.log                                 # run log
```

The output mirrors the input arborescence 1:1; the filename stem is identical,
only the extension changes.

---

## 2. Why it is built this way

The CLI does **not** reimplement transcription. Two pipelines already exist in
`transcription/src/`:

- `transcribe.py` — faster-whisper, per-chunk language detection, optional Darija
  LoRA routing.
- `transcribe_whisperx.py` — same, plus word alignment and speaker diarization.

The CLI (and the future FastAPI web backend) are both **thin front-ends** over a
shared `core/` package. `core/` owns the reusable logic — configuration,
file selection, the per-file runner, and run summaries — so the CLI and the UI
can never drift apart in behavior or output. The CLI just parses flags and prints;
the UI just serves HTTP and streams progress. Both call the same `core` functions.

```
                ┌─────────────┐         ┌──────────────────┐
                │   cli.py    │         │  FastAPI backend │   (front-ends)
                └──────┬──────┘         └────────┬─────────┘
                       │     both import          │
                       ▼                          ▼
                ┌───────────────────────────────────────┐
                │              core/  (shared)           │
                │  config · selection · runner · summary │
                └───────────────────┬───────────────────┘
                                    │ wraps
                                    ▼
                ┌───────────────────────────────────────┐
                │   src/  transcribe.py / _whisperx.py   │
                │        srt_writer.py  (existing)       │
                └───────────────────────────────────────┘
```

Two principles make this robust:

1. **Lazy heavy imports.** The faster-whisper / WhisperX stack is imported only
   inside the functions that actually transcribe. Importing `core` — and running
   selection, config, and dry-run logic — works with nothing but the standard
   library installed. This keeps the tool fast to start and the tests light.
2. **Injection seams.** `core.runner.run_file()` accepts optional `transcribe_fn`
   and `write_fn` parameters. Production code uses the real models; tests pass
   fakes. Every path/error branch is testable without a GPU or a model download.

---

## 3. Directory map

```
transcription/
├── cli.py                  # CLI entry point (argparse → core)
├── core/                   # shared logic (CLI + web UI both import this)
│   ├── __init__.py         # re-exports config symbols
│   ├── config.py           # TranscribeConfig dataclass + validation
│   ├── selection.py        # range parsing, medias scan, expand_selections()
│   ├── runner.py           # load models once, run_file(), mirrored SRT, errors
│   └── summary.py          # FileResult, RunSummary, log-line formatting
├── src/                    # existing pipelines (unchanged)
│   ├── transcribe.py
│   ├── transcribe_whisperx.py
│   └── srt_writer.py
└── tests/
    ├── test_config.py
    ├── test_selection.py
    ├── test_runner.py
    ├── test_cli.py
    └── test_srt_writer.py  # pre-existing
```

---

## 4. End-to-end data flow

A real run flows through these steps (see `cli.main()`):

```
1. parse argv                         build_parser().parse_args()
2. parse filters                      parse_channels(), parse_ranges()  [core.selection]
3. build + validate config            config_from_args() → TranscribeConfig.validate()  [core.config]
4. expand selection → file list       expand_selections()  [core.selection]
   └─ if --dry-run: print list, exit
5. load models ONCE                    runner.load_models(config)  [core.runner → src]
6. for each file:                      runner.run_file(...)  [core.runner]
     ├─ skip if .srt exists (no --overwrite)
     ├─ transcribe (src.transcribe_file)  → segments
     ├─ write mirrored .srt (srt_writer.write_srt)
     └─ capture failures as FileResult(status="failed")
   each result → RunSummary + a log line  [core.summary]
7. write JOB END line, print one-line summary
8. exit code: 0 if all ok, 1 if any failed, 2 on usage error
```

Per-file failures (missing file, corrupt audio, CUDA OOM) never abort the batch:
they are caught, recorded, logged as `[FAIL]`, and the run continues.

---

## 5. Module reference

### 5.1 `core/config.py` — the run configuration

`TranscribeConfig` is a `@dataclass` holding every option that affects a run. It
is the single source of truth shared by the CLI and the web UI.

Fields (grouped):

| Group | Field | Default | Meaning |
|-------|-------|---------|---------|
| headline | `pipeline` | `"faster-whisper"` | backend: `faster-whisper` or `whisperx` |
| headline | `speaker_annotation` | `False` | pyannote diarization (WhisperX only) |
| headline | `hf_token` | `None` | Hugging Face token (required if annotation on) |
| model | `model` | `"large-v3"` | faster-whisper model size or local path |
| model | `darija_lora` | `True` | route `ar` chunks through the anaszil Darija LoRA |
| model | `language` | `"auto"` | `auto` = per-chunk detection, or a forced code |
| model | `allowed_langs` | `("ar","fr","en")` | allow-list when `language=="auto"` |
| model | `max_chunk_s` | `25.0` | max VAD chunk length (seconds) |
| model | `device` | `"auto"` | `auto` / `cuda` / `cpu` |
| model | `overwrite` | `False` | re-transcribe even if `.srt` exists |
| output | `out_dir` | `"out/srt"` | output root; SRTs mirror medias underneath |
| decode | `beam_size` | `5` | decoder beam size |
| whisperx | `batch_size` | `8` | WhisperX batch size (ignored by faster-whisper) |
| whisperx | `min_speakers` / `max_speakers` | `None` | diarization hints |
| lora | `lora_model` / `lora_base` | anaszil / turbo | LoRA adapter + base model |

Key methods:

- `__post_init__()` — normalizes `allowed_langs`: a string like `"ar, fr ,en"`
  becomes the tuple `("ar","fr","en")`, so callers may pass either form.
- `validate() -> self` — raises `ConfigError` (a `ValueError` subclass) when the
  config is inconsistent. Rules enforced:
  - `pipeline` must be a known value;
  - `speaker_annotation` requires `pipeline == "whisperx"` **and** a non-empty
    `hf_token`;
  - `max_chunk_s > 0`; `device` is one of the three; `allowed_langs` non-empty.
  Returns `self` so you can write `cfg = TranscribeConfig(...).validate()`.
- `with_overrides(**kwargs)` — returns a copy with fields replaced (via
  `dataclasses.replace`); the original is untouched. Used by the UI's "retry"
  feature and handy for tests.
- `summary_str()` — the one-line config description embedded in the `[JOB START]`
  log header, e.g. `pipeline=faster-whisper | model=large-v3 | darija_lora=true | ...`.

Module constants: `PIPELINE_FASTER_WHISPER`, `PIPELINE_WHISPERX`,
`VALID_PIPELINES`, `DEFAULT_ALLOWED`, `DEFAULT_OUT_DIR`, `DEFAULT_LORA_MODEL`,
`DEFAULT_LORA_BASE`.

### 5.2 `core/selection.py` — what to transcribe

Turns coarse filters into a concrete file list. No heavy imports — safe and fast
for dry-runs.

**Range/list parsing**

- `parse_ranges(spec) -> set[int] | None`
  Parses `"9-18,21"`-style specs into a set of ints. Grammar: comma-separated
  tokens, each a single integer (`"21"`) or an inclusive range (`"9-18"`).
  Whitespace is ignored. `None`/empty → `None`, meaning "no filter" (= all).
  Raises `SelectionError` on a reversed range (`"18-9"`) or non-integer token.
- `parse_channels(spec) -> set[str] | None`
  Accepts a comma-string, an iterable of strings (each possibly comma-containing,
  as produced by a repeatable `--channel` flag), or `None`. Returns the set of
  names, or `None` for all.

**Scanning**

- `scan_medias(root) -> MediaIndex`
  Walks the tree into a nested dict
  `channel -> year(int) -> month(int) -> day(int) -> [filenames]`.
  Non-numeric year/month/day directories and non-media files (extensions outside
  `MEDIA_EXTS`) are skipped. A missing `root` returns `{}` (never raises).
  Everything is sorted.
- `hour_of(filename) -> int | None`
  Extracts the hour (0–23) from a `YYYYMMDDHHMMSS.<ext>` filename via regex
  (`stamp[8:10]`) — any media extension. Also accepts the legacy 12-digit
  `YYYYMMDDHHMM` form. Returns `None` for filenames that don't match the stamp
  pattern.

`MEDIA_EXTS` mirrors the set in `src/transcribe.py` / `src/transcribe_whisperx.py`
(audio + video: `.mp3 .wav .m4a .flac .ogg .opus .aac .wma .mp4 .mkv .mka .mov
.webm .avi .ts .m4v`). Both pipelines extract the audio track from video, so the
CLI batches video too (WhisperX's decode path needs `ffmpeg` on PATH; faster-whisper
bundles PyAV).

**Expansion**

- `expand_selections(root, channels=None, years=None, months=None, days=None, hours=None, *, index=None) -> list[str]`
  The heart of the module. Any filter left `None`/empty means "all" at that level.
  Returns a **sorted** list of paths **relative to `root`**, using forward slashes:
  `"al-oula/2024/06/01/20240601090000.mp3"`. Pass a pre-built `index` (from
  `scan_medias`) to avoid re-walking the disk. Hour filtering reads the hour from
  each filename via `hour_of`.

`MediaIndex` is the documented type alias for the nested-dict index.
`SelectionError` is the `ValueError` subclass raised on bad specs.

### 5.3 `core/runner.py` — how each file is transcribed

The bridge between a `TranscribeConfig` and the `src/` pipelines.

**Path bootstrapping.** `_SRC_DIR` points at `transcription/src`.
`_ensure_src_on_path()` prepends it to `sys.path` on demand, because the src
modules use a flat `from srt_writer import write_srt` import that requires the
directory itself to be importable. All heavy imports happen lazily inside the
functions below.

**Loading models once**

- `class ModelBundle` — holds `pipeline`, the loaded `model`, the optional
  `lora_pipe`, and the resolved `device`, for the lifetime of a run.
- `load_models(config) -> ModelBundle` — picks the backend module
  (`transcribe` or `transcribe_whisperx`) from `config.pipeline`, resolves
  `device` (`auto` → `_auto_device()`), loads the base model via the backend's
  `load_model()`, and — when `config.darija_lora` — loads the LoRA pipe via
  `_load_darija_lora()`. Called **once** per batch so models aren't reloaded per
  file (the expensive part of any run).

**Transcription dispatch**

- `_default_transcribe(bundle, path, config) -> list[dict]` — calls the correct
  backend's `transcribe_file(...)`, forwarding every relevant config field. For
  WhisperX it also passes `diarize=config.speaker_annotation`, `hf_token`,
  `batch_size`, and the speaker hints. Returns the list of segment dicts
  (`{start, end, text, lang}`).
- `_write_srt(segments, out_path)` — thin wrapper over `srt_writer.write_srt`.

Both are isolated precisely so tests can replace them.

**Output mirroring**

- `srt_output_path(rel_path, out_dir) -> Path` — maps
  `"al-oula/2024/06/01/20240601090000.mp3"` to
  `out_dir/al-oula/2024/06/01/20240601090000.srt` (swap suffix, preserve subdirs).
- `_audio_span(segments)` — approximate transcribed length = max segment `end`.

**The per-file entry point**

```python
run_file(media_root, rel_path, config, bundle=None, *,
         transcribe_fn=None, write_fn=None) -> FileResult
```

Logic, in order:
1. Compute input path and mirrored output path.
2. If the `.srt` exists and `overwrite` is off → return `FileResult(status="skipped")`;
   transcription is never invoked.
3. If the input file is missing → `FileResult(status="failed", error="file not found: …")`.
4. Otherwise time the work, call `transcribe_fn` (default `_default_transcribe`)
   then `write_fn` (default `_write_srt`). **Any exception** is caught and returned
   as `FileResult(status="failed", error="<ExcType>: <msg>")` — the batch keeps going.
5. On success → `FileResult(status="completed", srt_path=…, audio_seconds=…, processing_seconds=…)`.

`transcribe_fn` / `write_fn` are the **injection seams**: production passes neither
(real models run); tests pass fakes to exercise every branch without a model.

### 5.4 `core/summary.py` — logging & aggregation

Produces the exact log-line format shared with the web UI's per-job logs, so a CLI
run log and a UI job log are byte-for-byte comparable.

- `FileResult` dataclass — `rel_path, status, srt_path, error, audio_seconds,
  processing_seconds`. Status constants: `STATUS_COMPLETED`, `STATUS_FAILED`,
  `STATUS_SKIPPED`.
- Formatters (each returns one line; timestamp defaults to now, ISO-8601 seconds):
  - `format_job_start(total, config_summary, ts=None)` → `[JOB START]  … | N files | <config>`
  - `format_ok(rel_path, processing_seconds, ts=None)` → `[OK]         … | path | 4.2s`
  - `format_skip(rel_path, ts=None)` → `[SKIP]       … | path | exists (use --overwrite)`
  - `format_fail(rel_path, error, ts=None)` → `[FAIL]       … | path | <error>`
  - `format_job_end(status, done, total, ok, failed, ts=None)` → `[JOB END]    … | completed | 120/120 | 118 ok, 2 failed`
- `class RunSummary` — accumulates `FileResult`s and reports:
  `ok`, `failed`, `skipped`, `done` (ok+failed), `elapsed_seconds`,
  `status` (`"failed"` if any failed else `"completed"`), `end_line()`, and
  `oneline()` (the compact stdout summary). `add(result)` appends and returns it.

Example log:

```
[JOB START]  2026-06-24T11:40:01 | 3 files | pipeline=faster-whisper | model=large-v3 | darija_lora=true | language=auto | speaker_annotation=false
[OK]         2026-06-24T11:40:46 | al-oula/2024/06/01/20240601090000.mp3 | 44.8s
[FAIL]       2026-06-24T11:41:02 | al-oula/2024/06/01/20240601100000.mp3 | RuntimeError: CUDA out of memory
[SKIP]       2026-06-24T11:41:02 | al-oula/2024/06/01/20240601230000.mp3 | exists (use --overwrite)
[JOB END]    2026-06-24T11:41:30 | failed | 2/3 | 1 ok, 1 failed
```

### 5.5 `cli.py` — the entry point

- `build_parser() -> argparse.ArgumentParser` — defines all flags (kept separate
  so tests can introspect parsing). `--hours` has the alias `--hour`; the Darija
  LoRA is a mutually-exclusive `--darija-lora` / `--no-darija-lora` pair defaulting
  to on.
- `config_from_args(args) -> TranscribeConfig` — builds and **validates** the
  config. Turning on `--speaker-annotation` transparently upgrades the pipeline to
  WhisperX and falls back to the `$HF_TOKEN` environment variable when
  `--hf-token` is absent.
- `main(argv=None) -> int` — orchestrates the flow in §4 and returns the exit code.
- `_run(files, medias_root, config, args) -> int` — loads models once, processes
  each file, writes the log (default `out/logs/cli-<timestamp>.log`), prints the
  summary. Exit `0` if all ok, `1` if any failed.

Exit codes: `EXIT_OK = 0`, `EXIT_SOME_FAILED = 1`, `EXIT_USAGE = 2`
(missing medias dir, no matches, bad range, invalid config).

---

## 6. Usage

Run from the `transcription/` directory.

```bash
# Dry-run: list what would be transcribed, run no models.
python cli.py --channel al-oula --year 2024 --month 6 --hours 9-18 --dry-run

# Transcribe two channels, all of June 2024, with the recommended defaults.
python cli.py --channel al-oula,2m --year 2024 --month 6

# A single day, specific hours, forcing the turbo model and overwriting.
python cli.py --channel 2m --year 2024 --month 6 --day 1 \
    --hours 9-12,18 --model large-v3-turbo --overwrite

# Speaker annotation (WhisperX diarization) — needs a Hugging Face token.
python cli.py --channel 2m --year 2024 --month 6 --day 1 \
    --speaker-annotation --hf-token hf_xxx
# (or: export HF_TOKEN=hf_xxx  and omit --hf-token)
```

### Flag reference

| Flag | Default | Meaning |
|------|---------|---------|
| `--medias DIR` | `medias` | root of the medias tree |
| `--channel` | all | channel name(s); repeatable and/or comma-list |
| `--year` | all | year filter: list and/or range (`2024`, `2024-2025`) |
| `--month` | all | month filter 1–12 (`1-6`) |
| `--day` | all | day filter 1–31 (`1,15,30`) |
| `--hours` / `--hour` | all | hour filter 0–23 (`9-18,21`) |
| `--speaker-annotation` | off | diarization; implies WhisperX + needs a token |
| `--pipeline` | `faster-whisper` | `faster-whisper` or `whisperx` |
| `--model` | `large-v3` | model size or local path |
| `--darija-lora` / `--no-darija-lora` | on | Darija LoRA routing for `ar` chunks |
| `--lang` | `auto` | `auto` per-chunk, or a forced code |
| `--allowed` | `ar,fr,en` | allow-list for auto detection |
| `--max-chunk-s` | `25` | max VAD chunk length (seconds) |
| `--device` | `auto` | `auto` / `cuda` / `cpu` |
| `--overwrite` | off | re-transcribe even if `.srt` exists |
| `--hf-token` | `$HF_TOKEN` | HF token for diarization |
| `--out-dir` | `out/srt` | output root (mirrors medias) |
| `--log-file` | `out/logs/cli-<ts>.log` | run log path |
| `--dry-run` | off | list matched files and exit |
| `-v` / `--verbose` | off | echo each log line to stderr live |

### Mixed-language routing (the headline behavior)

With `--darija-lora` (default) and `--lang auto`, each ~25 s VAD chunk is
language-detected independently. Chunks detected as Arabic (`ar`) — which is how
Darija surfaces in Whisper — are transcribed by the
`anaszil/whisper-large-v3-turbo-darija` LoRA adapter; French/English chunks use
the base `--model`. A single code-switched broadcast is therefore transcribed
natively per segment, not forced into one language. This logic lives in
`src/transcribe.py::transcribe_file`; the CLI/core simply enable and feed it.

---

## 7. Installation & requirements

The CLI imports the transcription stack lazily, so **selection, config, and
dry-run work with only the standard library**. Actual transcription requires the
same environment as the pipeline:

```bash
pip install -r requirements.txt          # faster-whisper, etc.
pip install -r requirements_whisperx.txt  # only if you use --pipeline whisperx / annotation
# Darija LoRA needs: pip install transformers peft
```

Run everything in **one** environment — the CLI, the pipelines, and (later) the
FastAPI backend all share it.

---

## 8. Testing

```bash
python -m pytest tests/ -q
```

55 tests cover the core and CLI (plus the pre-existing SRT-writer tests). They run
**without** a GPU, a model download, or faster-whisper installed, thanks to the
lazy imports and the `transcribe_fn`/`write_fn` injection seams.

| Test file | Covers |
|-----------|--------|
| `test_config.py` | defaults, validation rules (annotation⇒whisperx+token, device, ranges), `with_overrides`, `summary_str` |
| `test_selection.py` | `parse_ranges`/`parse_channels`, `scan_medias` over a temp tree, `hour_of`, `expand_selections` with every filter combination |
| `test_runner.py` | `srt_output_path` mirroring, success/skip/overwrite/missing/exception paths, config pass-through — all via injected fakes |
| `test_cli.py` | flag parsing, `config_from_args` (annotation→whisperx, env token), dry-run listing, usage-error exit codes, full run with stubbed models (log content + exit codes) |

**Testing pattern to reuse:** never load a real model in a test. For runner logic,
pass `transcribe_fn=lambda bundle, path, cfg: [segments]` and
`write_fn=lambda segs, out: ...`. For CLI logic, `monkeypatch` `runner.load_models`
and `runner.run_file`.

---

## 9. Extending the codebase

- **Add a config option:** add a field to `TranscribeConfig`, enforce it in
  `validate()`, surface it in `core.runner._default_transcribe` (forward to
  `transcribe_file`), and add a flag in `cli.build_parser` + `config_from_args`.
  Because the UI builds the same `TranscribeConfig`, it gets the option too.
- **Add a filter dimension:** extend `expand_selections` (and `scan_medias` if it
  needs new tree structure) plus a `--flag` parsed with `parse_ranges`.
- **Add a new pipeline:** give it a module in `src/` exposing the same
  `load_model` / `_load_darija_lora` / `transcribe_file` surface, add its name to
  `VALID_PIPELINES`, and branch on it in `runner.load_models` /
  `_default_transcribe`.
- **Change the log format:** edit only `core/summary.py` — both the CLI and the UI
  inherit the change.

---

## 10. Gotchas & invariants

- **`src/` on `sys.path`.** The src modules use flat imports; `runner` injects the
  path automatically, but anything importing them directly must do the same.
- **Models load once.** `load_models` is called a single time in `_run`; never call
  it inside the per-file loop.
- **Failures are values, not exceptions.** `run_file` returns a failed
  `FileResult` instead of raising. Don't wrap it in a try/except expecting throws.
- **`audio_seconds` is an approximation** (max segment end), not the precise source
  duration — it avoids a second decode pass.
- **Hours come from the filename**, not file metadata. A file whose name doesn't
  match `YYYYMMDDHHMM.<ext>` is excluded by any hour filter (and has `hour_of → None`).
- **Audio and video are supported** (any extension in `MEDIA_EXTS`); the WhisperX
  decode path additionally needs `ffmpeg` on PATH.
- **Filters are "all" when omitted.** Passing nothing transcribes the entire tree —
  use `--dry-run` first to confirm the match count.
- **Stateless.** The CLI writes no database; the web UI's job DB is separate. The
  shared output is files + logs only.

---

## 11. Running with Docker (GPU)

On a machine with an NVIDIA GPU you can run the CLI in a container. Files:
`Dockerfile.gpu`, `.dockerignore`, and `docker-compose.yml` (all in `transcription/`).

### Host prerequisites

- NVIDIA driver (compatible with CUDA 12.4).
- **NVIDIA Container Toolkit** — this is what makes `docker run --gpus all` expose
  the GPU to the container. Without it the container starts but sees no GPU.

### Why a CUDA + cuDNN base image

faster-whisper runs on CTranslate2, which dynamically loads **cuBLAS** and
**cuDNN** at runtime. The image is built on `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`
so both libraries are present; this avoids the most common GPU error
(`Unable to load libcudnn…` / `libcublas…`). torch is installed from the CUDA 12.4
wheel index so its CUDA version matches the base image.

### Plain Docker

```bash
# Build (context is the transcription/ directory)
docker build -f transcription/Dockerfile.gpu -t haca-transcribe:gpu transcription/

# Run — args after the image name are CLI flags (ENTRYPOINT is python3 cli.py)
docker run --rm --gpus all \
  -v /data/medias:/data/medias:ro \      # input tree (read-only)
  -v /data/out:/app/out \                # SRTs + logs written back to host
  -v haca-model-cache:/cache/huggingface \  # persist multi-GB models
  -e HF_TOKEN=hf_xxx \                    # only needed for --speaker-annotation
  haca-transcribe:gpu \
  --medias /data/medias --channel al-oula --year 2024 --month 6 --device cuda

# Dry-run in the container
docker run --rm --gpus all -v /data/medias:/data/medias:ro \
  haca-transcribe:gpu --medias /data/medias --dry-run
```

### Docker Compose (nicer for repeated runs)

`docker-compose.yml` declares the GPU reservation, volumes, and `HF_TOKEN` once.
It mounts medias at the CLI's default path (`/app/medias`) and relies on
`--device auto`, so you only pass the filters. The `IMAGE` env var picks which
image to run — the locally-built tag by default, or a Docker Hub tag on the GPU box.

**Local machine (build the image):**

```bash
export MEDIAS_DIR=/data/medias       # host medias path (defaults to ./medias)
export HF_TOKEN=hf_xxx               # only for --speaker-annotation

docker compose build
docker compose run --rm transcribe --channel al-oula --year 2024 --month 6
docker compose run --rm transcribe --dry-run
docker compose run --rm transcribe --channel 2m --day 1 --speaker-annotation
```

**GPU box (pull the image from Docker Hub — no build):**

```bash
export IMAGE=<DOCKERHUB_USER>/haca-transcribe:gpu   # selects the Hub image
export MEDIAS_DIR=/data/medias
export HF_TOKEN=hf_xxx                               # only for --speaker-annotation

docker compose pull
docker compose run --rm transcribe --channel al-oula --year 2024 --month 6
```

The `build:` block in the compose file is only consulted by `docker compose build`
(or when the image is absent), so it's harmless on the box where you pull instead.

> Note: arguments passed to `docker compose run` **replace** the service's
> `command`, which is why the compose file bakes no flag defaults — it positions
> medias and the GPU via mounts/`--device auto` instead, so your appended flags
> are the whole command.

### Volumes & model cache

The first run downloads the models (`large-v3`/turbo, the Darija LoRA, and
pyannote for diarization) — several GB. The `haca-model-cache` named volume mounted
at `HF_HOME=/cache/huggingface` persists them so later runs start immediately.
Mount your real medias read-only (`:ro`) and `out/` writable so results land on the
host.

### Gotchas

- **Version match:** host driver ↔ base-image CUDA (12.4) ↔ torch wheel (`cu124`).
  If your driver is older, drop to a matching CUDA base tag and torch index URL.
- **cuDNN/cuBLAS errors:** use the `-cudnn-` base image (already done) or, on a
  non-cuDNN base, `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`.
- **No GPU in container:** almost always a missing NVIDIA Container Toolkit or a
  forgotten `--gpus all` / compose `deploy.resources` block.
- **CPU-only box:** this image targets GPU; for CPU just run the CLI natively with
  `--device cpu --model tiny` (see §6), or build a slim CPU image from
  `python:3.11-slim` like `benchmark/Dockerfile`.

### Building and pushing to Docker Hub

You do **not** need a GPU to build or push — the GPU is only used at runtime.
Build on any machine (even CPU-only), push to Docker Hub, then pull on the GPU
box. Replace `<DOCKERHUB_USER>` with your Docker Hub username.

```bash
cd transcription

# 1. Log in to Docker Hub (prompts for username + password / access token).
docker login

# 2. Build, tagging the image for your Docker Hub namespace.
docker build -f Dockerfile.gpu -t <DOCKERHUB_USER>/haca-transcribe:gpu .

# 3. Push it (several GB — CUDA + torch — so it takes a while).
docker push <DOCKERHUB_USER>/haca-transcribe:gpu
```

Then on the GPU box (needs only the NVIDIA driver + NVIDIA Container Toolkit):

```bash
docker pull <DOCKERHUB_USER>/haca-transcribe:gpu

docker run --rm --gpus all \
  -v /data/medias:/data/medias:ro \
  -v /data/out:/app/out \
  -v haca-model-cache:/cache/huggingface \
  -e HF_TOKEN=hf_xxx \
  <DOCKERHUB_USER>/haca-transcribe:gpu \
  --medias /data/medias --channel al-oula --year 2024 --month 6 --device cuda
```

**Cross-architecture note.** The GPU box is almost certainly `linux/amd64`. If you
build on an Apple Silicon (ARM) Mac, build explicitly for the target arch or the
image won't run on the box:

```bash
docker buildx build --platform linux/amd64 \
  -f Dockerfile.gpu -t <DOCKERHUB_USER>/haca-transcribe:gpu --push .
```

(`buildx ... --push` builds and pushes in one step.) If both machines are
x86-64 Linux, the plain `docker build` above is fine.

**The image bundles code + dependencies, not models.** The Whisper / LoRA /
pyannote weights (multi-GB) download on first run into the `haca-model-cache`
volume — keep that `-v ...:/cache/huggingface` mount on the GPU box so they
persist across runs.

**Private repo / no Docker Hub?** Use the offline transfer instead:

```bash
docker build -f Dockerfile.gpu -t haca-transcribe:gpu .
docker save haca-transcribe:gpu | gzip > haca-transcribe-gpu.tar.gz   # copy to box
# on the GPU box:
docker load < haca-transcribe-gpu.tar.gz
```

To use the Docker Hub image with `docker-compose.yml`, replace the `build:` block
with `image: <DOCKERHUB_USER>/haca-transcribe:gpu`.
