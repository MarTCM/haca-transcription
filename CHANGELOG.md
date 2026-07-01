# CHANGELOG — Media Ingestion Tooling

All changes made during the 2026-06-30 / 2026-07-01 development session.
Commits are in `haca-transcription` on `main` (starting at `43aa74d`).

---

## [2026-07-01] Documentation — all markdown + LaTeX updated

**Commit `ba376c0` — Update all markdown docs for ingestion tooling**

### `README.md`
- Added a new **Media Ingestion Tools** section (between "Batch CLI" and
  "Recommended workflow") covering all three tools:
  - `tools/fetch_youtube.py` with layout, example commands, and link to
    `YOUTUBE_DOWNLOADER.md`.
  - `tools/fetch_instagram.py` with layout, example commands, and link to
    `INSTAGRAM_DOWNLOADER.md`.
  - `tools/organize_medias.py` (brief mention of its role in preparing ingested
    audio for the transcription CLI).
- Updated the **Tests** section: mention that the three ingestion test files
  run without a network connection, yt-dlp, instaloader, or ffmpeg; total
  test count updated to 178 passing.
- Updated the **Layout** table: added `tools/_media_common.py`,
  `tools/fetch_youtube.py`, `tools/fetch_instagram.py`,
  `tools/requirements-youtube.txt`, `tools/requirements-instagram.txt`,
  `tests/test_media_common.py`, `tests/test_fetch_youtube.py`,
  `tests/test_fetch_instagram.py`, `docs/YOUTUBE_DOWNLOADER.md`,
  `docs/INSTAGRAM_DOWNLOADER.md`.

### `docs/PIPELINE.md`
- Added new section at the end: **Media Ingestion Pipeline
  (fetch_youtube / fetch_instagram)** explaining that both ingestion tools sit
  upstream of transcription, produce identical `{account}/{year}/{month}/{title}.mp3`
  trees, share `_media_common.py`, and how the output feeds into `cli.py` for
  batch transcription. Includes an ASCII flow diagram and cross-links to both
  detailed docs.

### `docs/CLI_ARCHITECTURE.md`
- Added new **Section 12 — Media Ingestion Tools** covering:
  - **12.1** `_media_common.py` — table of every exported symbol with its
    purpose.
  - **12.2** `fetch_youtube.py` — architecture summary, two-phase listing,
    dedup strategy, full CLI flag table, output layout, and integration note.
  - **12.3** `fetch_instagram.py` — architecture summary, session auth (login,
    2FA, session file), `posts_provider` injection, stage→ffmpeg pipeline,
    full CLI flag table, and output layout.

### `docs/CHALLENGES.md`
- Added new **Section 6 — Media Ingestion Challenges** with four subsections:
  - **6.1** YouTube channel listing hangs on large channels — root cause
    (yt-dlp flat extractor pages entire history before returning), fix
    (`playlistend` + `scan_limit`/`listing_end()`), and progress feedback
    lines.
  - **6.2** yt-dlp JS runtime detection failure ("two denos" trap) — the
    broken `/usr/bin/deno` shadowing the working `~/.deno/bin/deno` on PATH,
    the `yt-dlp-ejs` solver script requirement, and the diagnosis + fix.
  - **6.3** Instagram authentication and rate-limiting — session file
    approach, 2FA injection pattern, rate-limit mitigations, gitignore of the
    session credential.
  - **6.4** Per-platform dedup strategy — the two-mechanism (archive +
    filesystem safety net) pattern and the archive backfill behaviour.

---

**Commit `6853f96` — Add ingestion tools section to `report/etat_actuel.tex`**

### `report/etat_actuel.tex`
Added a new French-language `\section{Outils d'ingestion des médias}` with four
subsections:
- `\subsection{Téléchargeur YouTube incrémental — fetch_youtube.py}` — purpose,
  `{compte}/{année}/{mois}/{titre}.mp3` layout (with `lstlisting` snippet), yt-dlp
  engine, deduplication archive, and a `\begin{description}` block for all key
  flags (`--scan-limit`, `--js-runtimes`, `--dry-run`, `--log`).
- `\subsection{Téléchargeur Instagram — fetch_instagram.py}` — purpose,
  instaloader authentication and 2FA session handling (`--session-file`), ffmpeg
  audio extraction, same layout, deduplication register.
- `\subsection{Module de fonctions partagé — _media_common.py}` — all shared
  helpers listed in an itemize block.
- `\subsection{Suite de tests}` — 178 tests, pytest, mocks for
  yt-dlp/instaloader/ffmpeg, scenarios covered.

---

**Commit `7827c71` — Add ingestion chapter to `docs/transcription_report.tex`**

### `docs/transcription_report.tex`
Added a new `\chapter{Outils d'ingestion des médias / Media Ingestion Tools}` with
six sections:
1. **Vue d'ensemble / Overview** — flow from source platforms through ingestion
   tools to the transcription CLI; TikZ diagram.
2. **Module partagé `_media_common.py`** — every exported constant and function
   with description.
3. **Téléchargeur YouTube (`fetch_youtube.py`)** — dependencies/JS runtime
   (deno + yt-dlp-ejs), two-phase listing strategy, two-layer deduplication,
   `scan-limit`/`playlistend` bounding, output layout, and a CLI flag table with
   all 9 flags.
4. **Téléchargeur Instagram (`fetch_instagram.py`)** — dependencies, session auth
   with 2FA, `posts_provider` injection, stage→ffmpeg pipeline, dedup + scan-limit,
   and a CLI flag table with all 11 flags.
5. **Tests** — accurate counts (65 YouTube + 41 Instagram + 12 shared + 60
   pre-existing = 178), dependency injection strategy with code example, table of
   test files.
6. **Diagramme de flux / Full Pipeline Flow** — TikZ step-by-step diagram tracing
   8 steps from source selection through dedup, audio acquisition, output layout,
   transcription CLI, both pipeline variants, to SRT output.

---

## [2026-06-30] Instagram incremental audio downloader

**Commit `df1eb40` — Document the Instagram downloader architecture and code**

### `docs/INSTAGRAM_DOWNLOADER.md` *(new, 684 lines)*
Full architecture and line-by-line code walkthrough covering:
- What the tool does; example commands.
- Where it fits in the HACA project; the shared-module diagram.
- Architecture: pure-core vs injected-I/O table, pipeline mermaid diagram,
  deduplication (archive + filesystem), hybrid staging→ffmpeg, authentication.
- Library choices: instaloader as a library (not CLI), ffmpeg, stdlib only
  otherwise.
- Line-by-line walkthrough of every function in `_media_common.py` and
  `fetch_instagram.py`: `caption_title`, `stamp_from_post`, `archive_key`,
  `instagram_dest_path`, `ffmpeg_extract_cmd`, `make_loader`, `load_or_login`
  (with 2FA injection), `stage_download`, `extract_audio`, `download_account`
  loop (step-by-step annotation), `download_all`, `build_parser`, `main`.
- Testing: `FakePost`, `FakeLoader`, `fake_extract` strategy; coverage table.
- Operational notes: install, login/sessions, rate limits & ToS, cron, logs &
  progress, exit codes.
- Limitations: reels coverage, stories, title collisions, no live bar.

---

**Commit `3ddbcaf` — Add Instagram incremental audio downloader**

### `tools/fetch_instagram.py` *(new, 533 lines)*
Full incremental Instagram audio downloader. Features:
- **`caption_title(post)`** — first non-empty caption line or `None`.
- **`stamp_from_post(post)`** — calls `stamp_from_datetime(post.date_utc)`.
- **`archive_key(post)`** — `"instagram <shortcode>"`.
- **`instagram_dest_path(...)`** — `out/{account}/{YYYY}/{MM}/{title}.{ext}`.
- **`ffmpeg_extract_cmd(src, dest, fmt)`** — pure function building the ffmpeg
  argv (`-vn -acodec … -q:a 0` for mp3); supports mp3, m4a, aac, opus, ogg, wav,
  flac.
- **`InstaConfig`** dataclass — all run knobs with derived `archive_path`,
  `staging_dir`, `default_session_file()`, `post_limit()`.
- **`RunStats`** dataclass — per-account tallies with `add()` for multi-account
  aggregation.
- **`make_loader(loader_cls, staging)`** — configures Instaloader for
  videos-only staging (no thumbnails, metadata, captions).
- **`load_or_login(...)`** — load session or `--login` (password via `getpass`,
  2FA via injected `twofa_prompt` and `two_factor_exc`), saves session.
  Injectable prompts/exceptions for testability.
- **`_default_posts_provider(loader, account)`** — `Profile.get_posts()`
  (newest first).
- **`stage_download(loader, post, staging)`** — `download_post` → locate mp4 by
  shortcode glob.
- **`extract_audio(src, dest, fmt, runner)`** — ffmpeg via injectable `runner`.
- **`_cleanup_staging(staging, shortcode)`** — remove all staged files in
  `finally`.
- **`download_account(account, cfg, loader, ...)`** — bounded post loop with
  `[account i/N]` counter, video filter, archive check, since filter, on-disk
  backfill, dry-run, staging+extract, cleanup, and per-post error isolation.
- **`download_all(cfg, loader, ...)`** — per-account loop with account-level
  error isolation; aggregates stats.
- **`build_parser()` / `main()`** — lazy instaloader import, 2FA wiring, multi-
  account summary, exit codes (0/1/2).

### `tools/requirements-instagram.txt` *(new)*
Declares `instaloader>=4.14` and documents the system ffmpeg requirement.

### `tests/test_fetch_instagram.py` *(new, 405 lines)*
41 tests. Classes: `FakePost`, `FakeLoader` (writes a real `*.mp4` stub,
`produce_file=False` switch), `fake_extract`, `provider()`. Coverage:
`caption_title`, `stamp_from_post`, `archive_key`, `instagram_dest_path` (with
and without caption), `ffmpeg_extract_cmd` (mp3 VBR, m4a no quality flag),
`extract_audio` (success + failure path), `make_loader` configuration,
`InstaConfig` (`post_limit`, `default_session_file`), `load_or_login` (load /
missing-session / login / 2FA), full `download_account` loop (new / non-video /
archived / on-disk / idempotent / max-downloads / since / dry-run / error
isolation), `download_all` aggregation, and all parser flags.

### `.gitignore` updated
Added `youtube/` and `instagram/` to the gitignore so media files, dedup
archives, and the Instagram session secret are never accidentally committed.

---

## [2026-06-30] Shared helpers extraction

**Commit `25af723` — Extract shared downloader helpers into `_media_common`**

### `tools/_media_common.py` *(new, 119 lines)*
Pure, dependency-free shared module used by both downloaders:
- **`DEFAULT_SCAN_LIMIT = 50`** — shared default scan window.
- **`_ILLEGAL_FS`** regex — characters illegal on common filesystems.
- **`slugify_channel(title, fallback)`** — safe path segment from a channel/account
  title; preserves non-Latin letters and emoji.
- **`sanitize_filename(name, max_len=150, fallback)`** — safe filename stem with
  length cap.
- **`stamp_from_datetime(d)`** — any `datetime` → 14-digit UTC `YYYYMMDDHHMMSS`;
  aware datetimes converted to UTC, naive assumed UTC.
- **`dest_for(out_root, account, stamp, title, ext)`** — shared path builder
  `out/{account}/{YYYY}/{MM}/{title}.{ext}`.
- **`load_archive(path)`** → `set` of lines; missing file → empty set.
- **`append_archive(path, key)`** — crash-safe append, creates parent dirs.
- **`make_logger(log_path)`** → `(emit, close)` tee logger; timestamps in file,
  clean on stdout, immediate flush.

### `tools/fetch_youtube.py` refactored
Removed the now-duplicate definitions of `slugify_channel`, `sanitize_filename`,
`load_archive`, `append_archive`, and `make_logger`; replaced with imports from
`_media_common`. Added `stamp_from_datetime` usage inside `stamp_from_info`.
`dest_path` now calls `dest_for`. All 65 YouTube tests stay green after the
refactor.

### `tests/test_media_common.py` *(new, 89 lines)*
12 direct tests for `_media_common.py`: `slugify_channel` (parametrized),
`sanitize_filename` (parametrized, truncation), `stamp_from_datetime` (naive
UTC + aware-timezone conversion), `dest_for` (layout + dotted-ext strip),
archive round-trip, and `make_logger` (file writes, append, no-op without file).

---

## [2026-06-30] YouTube downloader — JS runtime / yt-dlp-ejs fix

**Commit `d8fb7aa` — Document JS runtime + yt-dlp-ejs requirement**

### Problem diagnosed and fixed
`yt-dlp-ejs` Python package was not installed. Recent yt-dlp needs **both** a
working JS runtime (deno ≥ 2.3.0) **and** the `yt-dlp-ejs` challenge-solver
script to extract YouTube. The tool continued to warn about "No supported
JavaScript runtime" even after deno was installed because:
1. `yt-dlp-ejs` was missing (the solver script yt-dlp needs alongside the
   runtime).
2. A broken `/usr/bin/deno` (linker error: `undefined symbol: sqlite3...`) was
   first on PATH, shadowing the working `~/.deno/bin/deno`.

**Fixes applied:**
- Installed `yt-dlp-ejs==0.8.0` into the project venv.
- Added `yt-dlp-ejs>=0.8.0` to `tools/requirements-youtube.txt`.

### `tools/requirements-youtube.txt` updated
Added `yt-dlp-ejs>=0.8.0` with a detailed comment explaining:
- What `yt-dlp-ejs` does (ships the challenge-solver script offline).
- The "two denos" gotcha (broken system deno shadowing the working one).
- How to diagnose (`command -v deno` + `deno --version`).
- How to fix (PATH order, `--js-runtimes` flag, or remove the broken deno).

### `docs/YOUTUBE_DOWNLOADER.md` updated
- Rewrote section 7.1b into **"JavaScript runtime + solver (required by recent
  yt-dlp)"** covering both requirements (runtime + `yt-dlp-ejs`), the "two denos"
  gotcha with diagnosis commands and all three fix options, and the note about
  `node`/`bun` needing `--remote-components` while deno+yt-dlp-ejs avoids it.

---

## [2026-06-30] YouTube downloader — incremental improvements

These changes were made iteratively during the session and are all reflected in
the final `tools/fetch_youtube.py` and `docs/YOUTUBE_DOWNLOADER.md`.

### Layout change: stamp-based → title-based filenames
Changed the output filename from `{YYYYMMDDHHMMSS}.{ext}` to `{video_title}.{ext}`,
with the timestamp falling back as the filename only when a video has no title.
The year/month folder was simultaneously changed from `YYYY-MM` to `YYYY/MM` (two
separate folder levels). Added `sanitize_filename()` and the `sanitize_filename`
helper. The internal stamp is still computed and used for folder building.

Rationale: title-based filenames are more immediately readable; the layout now
matches `fetch_instagram.py` exactly so both tools can feed the same downstream
workflow.

### `--js-runtimes RUNTIME[:PATH]` flag *(repeatable)*
Added `--js-runtimes` to `fetch_youtube.py`:
- Parsed by `parse_js_runtimes(values)` → yt-dlp's `{runtime: {path}}` dict
  (mirrors yt-dlp's own CLI parsing exactly, including `RUNTIME:PATH` splitting
  and lowercasing).
- Forwarded through `list_entries`, `fetch_info`, and `build_ydl_opts`/`download_audio`.
- Added to `FetchConfig.js_runtimes` field.
- Tests: `test_parse_js_runtimes_*` (4 parametrized cases), `test_build_ydl_opts_includes_js_runtimes`, `test_parser_js_runtimes_repeatable`.

### `[i/N]` progress counter
Every per-video log line now carries `[{idx}/{total}]` so the user knows where
in the channel upload list they are. A `[..] downloading <title>` line is printed
*before* a download starts (so a long video doesn't sit silent), and `[ok]`/
`[plan]`/`[error]` after. The total (`N`) comes from the bounded entry list, so
the fraction is always meaningful.

### `--log FILE` option (tee logger)
Added `--log FILE` flag: the tool's own output (header, per-video lines, summary)
is tee'd to a log file, each line prefixed with `YYYY-MM-DD HH:MM:SS` and flushed
immediately so `tail -f` works live. The console stays timestamp-free. Implemented
via `make_logger(log_path)` (later moved to `_media_common.py`).

### `--scan-limit` flag (channel listing bound)
Added `--scan-limit N` (default 50) to bound the yt-dlp flat listing to the
channel's N most recent uploads (`playlistend` option). This fixes the hang when
pointing the tool at a large TV channel. `FetchConfig.listing_end()` implements
the logic: never let the window drop below `--max-downloads`; `0` = no bound.
Added listing feedback: `"listing uploads (scanning N most recent)..."` and
`"examining N upload(s)"`.

### `% escape` in titles
Any literal `%` in a video title is escaped to `%%` in the yt-dlp `outtmpl`
template to prevent it being treated as a template field. Test:
`test_build_ydl_opts_escapes_percent_in_title`.

### Test suite expanded
65 tests total by end of session (up from the initial 42). All additions are in
`tests/test_fetch_youtube.py`:
- `test_sanitize_filename_*` (3 tests)
- `test_dest_path_layout_uses_title`, `test_dest_path_falls_back_to_stamp_without_title`, `test_dest_path_strips_dotted_ext_and_sanitizes_title`
- `test_parse_js_runtimes_*` (4 tests)
- `test_build_ydl_opts_escapes_percent_in_title`, `test_build_ydl_opts_includes_js_runtimes`
- `test_parser_js_runtimes_repeatable`
- `test_list_entries_passes_playlistend`
- `test_listing_end_*` (4 tests: default, explicit, never-below-max-downloads, zero-unbounded)
- `test_make_logger_*` (3 tests: file+stdout, append, no-op)
- `test_download_new_downloads_all_new` updated for `[i/N]` counter assertions
- `test_parser_defaults` updated (scan_limit, log)
- `test_parser_full` updated (scan_limit, log, js_runtimes)

---

## [2026-06-30] YouTube channel incremental audio downloader — initial implementation

**Commits `43aa74d` + `ebc756d` + `d90431e`**

### `tools/fetch_youtube.py` *(new, 575 lines → grew through iterations)*
Incremental YouTube channel audio downloader. Initial design:
- **`slugify_channel(title)`** — safe folder name from a channel title.
- **`stamp_from_info(info)`** — 14-digit UTC stamp from yt-dlp `timestamp` (epoch,
  preferred) or `upload_date` (8-digit date, fallback); raises `ValueError` if neither.
- **`sanitize_filename(name, max_len, fallback)`** — safe filename stem.
- **`dest_path(out_root, channel, info, ext)`** — final output path.
- **`normalize_channel_url(url)`** — appends `/videos` to bare channel URLs so yt-dlp
  returns the uploads list.
- **`archive_key(entry)`** — `"<extractor> <id>"` in yt-dlp's own format (interchangeable
  with yt-dlp's `--download-archive`).
- **`entry_url(entry)`** — watch URL from a flat-playlist entry.
- **`load_archive` / `append_archive`** — dedup archive I/O.
- **`parse_js_runtimes(values)`** — CLI `RUNTIME[:PATH]` strings → yt-dlp dict.
- **`build_ydl_opts(...)`** — yt-dlp options dict for one video download.
- **`FetchConfig`** dataclass — all knobs with derived `archive_path`, `listing_end()`.
- **`RunStats`** dataclass — download/skip/error tallies + `planned` list for dry-run.
- **`list_entries(url, factory, js_runtimes, playlistend)`** — Phase 1: cheap flat
  listing bounded by `playlistend`.
- **`fetch_info(url, factory, js_runtimes)`** — Phase 2: full metadata per video.
- **`download_audio(url, dest, fmt, factory, js_runtimes)`** — actual download.
- **`download_new(cfg, factory, log=print)`** — the main orchestration loop.
- **`make_logger(log_path)`** → tee logger.
- **`build_parser()` / `main()`** — lazy yt-dlp import, argparse CLI, exit codes 0/1/2.

### `tools/requirements-youtube.txt` *(new)*
`yt-dlp>=2025.1.1`, ffmpeg system dependency documented, `yt-dlp-ejs>=0.8.0`
(added in `d8fb7aa`).

### `tests/test_fetch_youtube.py` *(new, 534 lines → grew through iterations)*
`FakeYDL` class with class-level `entries`/`info_map`/`downloaded`; `fake_ydl`
pytest fixture. Tests for all pure helpers and the full orchestration loop.

### `docs/YOUTUBE_DOWNLOADER.md` *(new, 1130 lines)*
Detailed walkthrough:
- §1 What it does + example commands.
- §2 Project fit.
- §3 Architecture: pure-core/IO table, pipeline mermaid, two-phase extraction
  rationale, dedup strategy, idempotency.
- §4 Library choices: yt-dlp Python API, system ffmpeg, stdlib only.
- §5 Line-by-line walkthrough of every function.
- §6 Testing strategy.
- §7 Operational notes: install, JS runtime + solver, big channels/scan-limit,
  scheduling, logs & progress, archive, exit codes.

---

## Summary

| File | Status | Lines added |
|------|--------|------------|
| `tools/_media_common.py` | New | 119 |
| `tools/fetch_youtube.py` | New (iterated) | ~490 net |
| `tools/fetch_instagram.py` | New | 533 |
| `tools/requirements-youtube.txt` | New | 20 |
| `tools/requirements-instagram.txt` | New | 10 |
| `tests/test_media_common.py` | New | 89 |
| `tests/test_fetch_youtube.py` | New (iterated) | 534 |
| `tests/test_fetch_instagram.py` | New | 405 |
| `docs/YOUTUBE_DOWNLOADER.md` | New (iterated) | 1130 |
| `docs/INSTAGRAM_DOWNLOADER.md` | New | 684 |
| `docs/PIPELINE.md` | Updated | +86 |
| `docs/CLI_ARCHITECTURE.md` | Updated | +186 |
| `docs/CHALLENGES.md` | Updated | +150 |
| `docs/transcription_report.tex` | Updated | +1918 |
| `report/etat_actuel.tex` | Updated | +339 |
| `README.md` | Updated | +96 |
| `.gitignore` | Updated | +3 |

**Test count:** 178 passing (65 YouTube + 41 Instagram + 12 shared + 60 pre-existing).
**Commits:** 10 commits on `main` (`43aa74d` → `7827c71`).
