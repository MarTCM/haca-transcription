# Batch Transcription CLI — Implementation Plan

## 1. Problem Statement

A stateless command-line tool over the `medias/` tree that transcribes a filtered
subset of `.mp3` broadcasts to mirrored `.srt` output. It shares all transcription
and selection logic with the FastAPI backend (the Transcription UI) via a common
`core/` package, so the two front-ends never drift.

Media hierarchy: `medias/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.mp3`
Output mirror:   `out/srt/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.srt`

## 2. Requirements (gathered)

- Points at a `medias/` folder (default `./medias`).
- Filters by **channel / year / month / day / hour**, each accepting a list and/or
  range, or omitted entirely (= all).
- `--speaker-annotation` flag, **off by default**. When on, implies the WhisperX
  pipeline and requires a Hugging Face token.
- Stateless: no database. Output is `.srt` files + console summary + a run log file.
- All other pipeline options hardcoded to recommended defaults **but also exposed as
  optional flags** (model, Darija-LoRA, language, allowed langs, max chunk, device,
  overwrite, pipeline, out-dir, hf-token).
- Shared core: selection-expansion and the transcribe runner live in a neutral
  `transcription/core/` package imported by both the CLI and the FastAPI backend.

## 3. Project Structure (shared core)

```
transcription/
├── src/                         # existing: transcribe.py, transcribe_whisperx.py, srt_writer.py
├── core/                        # NEW — shared by CLI + API
│   ├── __init__.py
│   ├── config.py                # TranscribeConfig dataclass + defaults + validation
│   ├── selection.py             # range/list parser, medias scan, expand_selections() -> [rel mp3 paths]
│   ├── runner.py                # load models once; run_file() -> result; mirrored SRT; catch errors
│   └── summary.py               # shared JOB START/OK/FAIL/JOB END log + summary formatting
├── cli.py                       # NEW — argparse entrypoint, wires core together
├── tests/
│   ├── test_srt_writer.py       # existing
│   ├── test_selection.py        # NEW
│   ├── test_runner.py           # NEW
│   ├── test_config.py           # NEW
│   └── test_cli.py              # NEW
└── api/                         # imports core; services/scanner.py & transcribe_runner.py
                                 # become thin wrappers over core
```

## 4. CLI Surface

```
python cli.py [--medias DIR]

  # filters (omit any = all):
  --channel al-oula,2m         repeatable or comma-list of channel names
  --year    2024-2025          list and/or range
  --month   1-6                list and/or range (1-12)
  --day     1-15               list and/or range (1-31)
  --hours   9-18,21            list and/or range (0-23); matched against the HH in filename

  # headline option:
  --speaker-annotation         default OFF; implies --pipeline whisperx; requires --hf-token / $HF_TOKEN

  # defaulted-but-overridable:
  --pipeline    faster-whisper   {faster-whisper, whisperx}
  --model       large-v3         faster-whisper model size or local path
  --darija-lora / --no-darija-lora   default ON (route 'ar' chunks through anaszil LoRA)
  --lang        auto             'auto' = per-chunk detection, or a forced code (ar/fr/en...)
  --allowed     ar,fr,en         allow-list for auto detection
  --max-chunk-s 25
  --device      auto             {auto, cuda, cpu}
  --overwrite                    re-transcribe even if .srt exists
  --out-dir     out/srt
  --hf-token    TOKEN            or $HF_TOKEN env var
  --log-file    out/logs/cli-<timestamp>.log
  --dry-run                      list matched files + count, do not transcribe
  -v / --verbose
```

### Grammar

- Range/list parser applies to `--year/--month/--day/--hours`:
  `"9-18,21"` -> `{9,10,...,18,21}`, `"1,3,5"` -> `{1,3,5}`, omitted -> all.
- Hour is parsed from the `HH` portion of the filename (`YYYYMMDDHHMMSS.mp3`).
- `--channel` is a comma-list and/or repeated flag; omitted -> all channels.

### Mixed-language routing

- `--darija-lora` (default on) + `--lang auto` -> Arabic chunks use the
  `anaszil/whisper-large-v3-turbo-darija` LoRA, French/English chunks use the base
  `--model`, per chunk within a single file (existing `transcribe_file` behavior).

### Validation & behavior

- `--speaker-annotation` auto-selects `--pipeline whisperx`; errors out if no HF token.
- Empty match set -> friendly message + exit code 2.
- Continue-on-error per file (skip a bad file, keep going).
- Final `N ok / M failed` summary to stdout; exit code 0 if all ok, non-zero if any failed.

## 5. Shared Core Detail

### core/config.py
- `TranscribeConfig` dataclass: `pipeline, model, darija_lora, language, allowed_langs,
  max_chunk_s, device, overwrite, speaker_annotation, hf_token, out_dir`.
- Recommended defaults baked in; `validate()` enforces annotation ⇒ whisperx + token.

### core/selection.py
- `parse_ranges(spec: str) -> set[int]` — parse `"9-18,21"` style specs.
- `scan_medias(root) -> nested dict` — channel -> year -> month -> day -> [files].
- `expand_selections(root, filters) -> list[str]` — sorted relative `.mp3` paths matching
  the channel/year/month/day/hour filters; graceful empty when `medias/` missing.

### core/runner.py
- Loads base model (`load_model`) and Darija-LoRA pipe (`_load_darija_lora`) once as
  module-level singletons.
- `run_file(root, rel_path, config, out_dir) -> {status, srt_path, error,
  audio_seconds, processing_seconds}`.
- Writes SRT to the mirrored output path via `srt_writer.write_srt`.
- Uses the WhisperX path when `config.pipeline == "whisperx"`.
- Catches CUDA OOM / corrupt / FileNotFoundError as failures rather than crashing.

### core/summary.py
- Shared structured log lines: `JOB START / OK / FAIL / JOB END`, identical to the UI
  job-log format.
- Aggregates per-run totals (ok / failed / elapsed).

## 6. Task Breakdown (test-driven; core first — CLI + API both depend on it)

| Task | File | What to build | Tests | Demo |
|------|------|---------------|-------|------|
| 1 | `core/config.py` | `TranscribeConfig` + defaults + `validate()` | defaults; annotation-without-token raises | print default config; bad combo errors |
| 2 | `core/selection.py` | range parser, medias scan, `expand_selections()` | temp medias fixture: lists, ranges, all, channel + hour filtering, missing dir | dry-run listing prints matched files + count |
| 3 | `core/runner.py` | load models once; `run_file()`; mirrored SRT; error capture | stubbed `transcribe_file`: ar→LoRA only, fr/en→base; failure path returns error; SRT at mirror path | tiny CPU clip → SRT at mirrored path |
| 4 | `core/summary.py` | shared log-line format + run aggregation | formatting + totals | format a sample run log |
| 5 | `cli.py` | argparse; wire selection→runner→summary; dry-run; continue-on-error; log file; exit codes | arg parsing (lists/ranges; annotation-without-token errors); dry-run with stubbed runner | `python cli.py --channel al-oula --year 2024 --month 6 --hours 9-18 --dry-run` lists files; small real run → SRTs + summary |
| 6 | API refactor | point `api/services/scanner.py` + `transcribe_runner.py` at `core/`; update UI `PLAN.md` §2/§7 | existing API tests still pass | API and CLI yield identical SRT for the same file |
| 7 | Docs | README/QUICKSTART CLI section with examples | — | follow README example end-to-end |

## 7. Edge Cases & Error Handling

| Scenario | Handling |
|----------|----------|
| `medias/` missing or empty | Friendly message, exit code 2 |
| No files match the filters | Print "0 files matched", exit code 2 |
| `--speaker-annotation` without HF token | Validation error before any work, exit code 2 |
| Invalid range spec (e.g. `--hours 9-`) | argparse-level error with example |
| File disappears between scan and transcribe | Runner catches FileNotFoundError -> mark failed, continue |
| GPU OOM / corrupt file | Runner catches -> mark failed, continue with next |
| `.srt` already exists | Skip unless `--overwrite` |
| `out/srt` or `out/logs` missing | Created automatically |
| Same env requirement | Run inside the env with `transcription/requirements.txt` (torch/faster-whisper/whisperx/peft) |
