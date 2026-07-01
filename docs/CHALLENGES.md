# Challenges & Difficulties — HACA Transcription Pipeline

A consolidated account of the hard problems hit while building the broadcast→`.srt`
transcription pipeline, the batch CLI, the GPU Docker image, and the native installs.
Grounded in [`docs/PIPELINE.md`](PIPELINE.md), [`docs/CLI_ARCHITECTURE.md`](CLI_ARCHITECTURE.md),
[`docs/WHISPERX_GUIDE.md`](WHISPERX_GUIDE.md) and [`docs/DOCKER_AND_CLI_GUIDE.md`](DOCKER_AND_CLI_GUIDE.md),
plus the field issues surfaced when running on real GPU/Windows boxes.

---

## 1. Transcription-quality challenges

### 1.1 Code-switching (Darija ↔ MSA ↔ French)
Moroccan broadcasts switch language mid-programme. Vanilla Whisper detects **one**
language from the first ~30 s and applies it to the whole file — wrong for most of a
mixed broadcast. Solution: **per-chunk language detection** — VAD-split the audio into
~25 s chunks (breaking only at silence), run `model.detect_language()` on each, and
constrain to an allow-list (`ar,fr,en`) so noisy Darija isn't mis-detected as Persian/Urdu.

### 1.2 No dedicated Darija language code
Whisper has no Darija code; Moroccan Arabic is transcribed under the generic `ar` label
in Arabic script. Usable, but it means language routing and any downstream language logic
treat Darija and MSA as the same bucket.

### 1.3 Darija accuracy vs speed (the LoRA trade-off)
The base `large-v3-turbo` under-recognises Darija. The `anaszil` Darija LoRA improves it
(~25% WER, 3.4× faster than a full MaghrebVoice fine-tune), **but** it runs in PyTorch
(HF `transformers` pipeline), which is slower per token than faster-whisper's CTranslate2
engine. Solution: a **hybrid** — route only `ar` chunks through the LoRA, keep FR/EN on
CTranslate2. This added complexity (two inference engines in one file) but gave the best
quality/speed mix. It also later turned out to be the source of the `torchcodec`/FFmpeg
dependency surprise (§4.2).

### 1.4 Diarized LoRA segments need word timestamps
LoRA segments lacked the `words` field that WhisperX's alignment + diarization needs to
assign `[SPEAKER_XX]`. Without it, Arabic chunks routed through the LoRA wouldn't get
speaker labels even with diarization on. Solution: synthesise approximate per-word
timestamps (`_words_from_segment`) for LoRA output, later refined by the wav2vec2 aligner.

### 1.5 Long, un-resegmented cues
Whisper's native segment boundaries can be very long (20+ s cues). Proper subtitle
re-segmentation to a characters-per-line budget needs word-level timestamps and isn't
implemented — a known limitation left for a downstream tool.

### 1.6 Music / noise / hallucination
Music and heavy noise can yield hallucinated or empty cues. Silero VAD filters silence,
and the downstream benchmark has a garble gate, but the transcription side does not fully
solve hallucination on non-speech audio.

---

## 2. Architecture & dependency-isolation challenges

### 2.1 WhisperX's heavy dependency footprint
`pyannote.audio` (diarization) pulls in hundreds of MB (PyTorch + ONNX + pipelines).
Forcing every user to install it just to get `.srt` output is wasteful. Solution: **two
scripts** — `transcribe.py` (faster-whisper, light) and `transcribe_whisperx.py`
(alignment + diarization) — sharing only the audio pipeline, LoRA helpers, and SRT writer.

### 2.2 Sharing logic between CLI and (planned) web backend
The batch CLI and the planned web backend must agree on selection/transcription logic.
Solution: a `core/` package (config, selection, runner, summary) imported by both, with
injection seams (`transcribe_fn`/`write_fn`) so the logic is testable without GPU/models
(59→60 stub tests).

---

## 3. Filename / layout challenges

### 3.1 14-digit filename format mismatch (a silent bug)
The selection logic assumed 12-digit `YYYYMMDDHHMM` filenames, but real exports are
14-digit `YYYYMMDDHHMMSS`. `hour_of()` returned `None` for real files, so `--hours`
filtering **silently dropped every file**. Fixed by widening the regex to accept 12 or 14
digits (hour stays at positions 8–9), with tests pinning both forms.

### 3.2 Flat medias trees
Real medias arrived as `channel/<all files flat>`, but the CLI expects
`channel/year/month/day/<file>`. Solution: a `tools/organize_medias.py` utility that reads
the date from each filename stamp and moves files into the right subfolders (dry-run +
copy modes, idempotent, skips unrecognised names).

---

## 4. Deployment & environment challenges (the bulk of the field pain)

### 4.1 GPU Docker build kept timing out on a slow network
The cu124 torch stack is ~2.5 GB across several large wheels. The original Dockerfile
installed `requirements.txt` first, which dragged in the generic PyPI torch (~530 MB) +
triton, **then** reinstalled the cu124 torch — downloading torch twice — and a single
stalled read aborted the whole `RUN`. Fixes: install the CUDA-matched torch **first** so
the requirements don't pull the PyPI build; make the install **resumable** with a BuildKit
pip cache mount + split layers (so a re-run continues from cached wheels) and
`--retries`/`--timeout`.

### 4.2 FFmpeg / torchcodec: the "no FFmpeg needed" assumption was incomplete
faster-whisper decodes with bundled PyAV (no system FFmpeg), and the docs initially said
FFmpeg was only needed for `--speaker-annotation`. In practice the **Darija LoRA path**
(default on) goes through `transformers`/`torchcodec`, which **does** need the system
FFmpeg shared libraries — so a real run failed with *"Could not load libtorchcodec"* on a
box without FFmpeg. Compounding issues on Windows:
- **FFmpeg 8 is too new** — torchcodec supports FFmpeg **4–7** only.
- torchcodec needs the **shared** FFmpeg DLLs (`avcodec-*.dll`, …), not the static
  `ffmpeg.exe` — so `ffmpeg --version` reporting 7.x is not sufficient.
- On **Python 3.8+, Windows no longer searches PATH** for an extension module's dependent
  DLLs, so even a shared FFmpeg on PATH may not be found — the reliable workaround is to
  copy the FFmpeg DLLs next to torchcodec's own DLLs.
- Escape hatch: `--no-darija-lora` avoids torchcodec entirely (PyAV only).

### 4.3 Wrong torch build: CPU instead of CUDA
A "GPU box" run was actually executing on **`torch 2.8.0+cpu`** (CPU-only), so no GPU was
used. Root causes documented: `pip install torch …` is a **no-op when torch is already
present** (the CUDA wheel never replaces the CPU one — needs uninstall / `--force-reinstall`),
and `pip` vs `python` environment mismatches. Lesson baked into the guide: verify the
**build tag** (`+cu128`/`+cu126`), not just `torch.cuda.is_available()`, and confirm
`nvidia-smi` works.

### 4.4 Python version too new for torch
Native installs on Python **3.14** failed with *"Could not find a version that satisfies
the requirement torch"* — PyTorch publishes wheels only up to Python 3.13. Guidance:
use 64-bit **Python 3.10–3.12**.

### 4.5 Version-pin conflicts in the CUDA stack
An unpinned CUDA install pulled the newest torch (`2.11.0+cu128`), but `whisperx 3.8.6`
and `pyannote-audio 4.0.5` **pin** `torch~=2.8.0` (+ matching `torchaudio`/`torchvision`/
`torchcodec`). Resolution: pin the whole stack —
`torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0` from the cu126/cu128 channel matching
the driver, plus `torchcodec==0.7.*`.

### 4.6 Gated diarization models
`--speaker-annotation` needs a Hugging Face token **and** the account must have accepted
the terms for the gated pyannote models (`speaker-diarization`, `segmentation`). Without a
valid token, WhisperX **silently** skips diarization and emits plain transcription — which
looks like a bug but is by design.

### 4.7 Latent Docker version conflict (open)
The GPU `Dockerfile.gpu` still installs torch unpinned from the **cu124** channel (max
torch 2.6.0), while `requirements_whisperx.txt` now pulls whisperx 3.8.6 which pins
`torch~=2.8.0` — so the image build would likely hit the same §4.5 conflict at the
whisperx step. Not yet fixed (earlier builds timed out before reaching it). Recommended:
pin the image's torch stack to 2.8.0 on cu126/cu128 the same way the native docs now do.

---

## 5. Cross-cutting lessons

- **PyAV ≠ torchcodec.** faster-whisper's own decode is self-contained, but the
  transformers/WhisperX paths bring a separate FFmpeg dependency — the decode story is
  pipeline-dependent, not global.
- **Pin the CUDA stack.** whisperx/pyannote constrain torch tightly; unpinned installs
  silently over-shoot and break.
- **Verify the build tag, not the bool.** `+cpu` vs `+cuXXX` is the real signal.
- **Run it where it's designed to run.** Most of the field pain (CPU torch, FFmpeg DLLs,
  Python-version wheels) is Windows-specific; the GPU Docker image or a Linux GPU box
  avoids it and is far faster.

---

## 6. Media Ingestion Challenges

Challenges hit while building the YouTube and Instagram incremental audio
downloaders (`tools/fetch_youtube.py` and `tools/fetch_instagram.py`). The
companion architecture docs are [`YOUTUBE_DOWNLOADER.md`](YOUTUBE_DOWNLOADER.md)
and [`INSTAGRAM_DOWNLOADER.md`](INSTAGRAM_DOWNLOADER.md).

### 6.1 YouTube channel listing hangs on large channels

**Symptom.** On a channel with tens of thousands of uploads (e.g. a TV network
archive), `yt-dlp.extract_info(channel_url, extract_flat="in_playlist")` pages
through the *entire* upload history before returning. This can take many minutes
and looks indistinguishable from a hung process — no output, no progress, no
error.

**Root cause.** yt-dlp's flat extractor lazily pages through a playlist; when you
iterate `info["entries"]` (a generator) Python forces all pages before returning
the full list. The `--max-downloads` cap that was intended to bound the work
never gets a chance to apply because the listing step happens before the
per-video loop.

**Fix — `playlistend` + `scan_limit`.** The `FetchConfig.listing_end()` method
returns `max(scan_limit, max_downloads)` (defaulting `scan_limit` to 50) and
passes it to yt-dlp as `opts["playlistend"]`. yt-dlp stops paging early once it
has fetched that many entries, so the listing step is bounded regardless of
channel size. `scan_limit=0` disables the cap for users who explicitly want the
full history. The tool also prints `"listing uploads (scanning N most recent)…"`
then `"examining N upload(s)"` so users can see it working and know it hasn't
hung.

### 6.2 yt-dlp JS runtime detection failure (the "two denos" trap)

**Symptom.** After installing deno and yt-dlp-ejs, yt-dlp still warns:

```
WARNING: [youtube] No supported JavaScript runtime could be found. ...
```

and some formats or metadata may be missing or stale.

**Root cause — yt-dlp probes the first `deno` on `PATH`**, not necessarily the
one just installed. A broken system-installed deno (a common culprit is a distro
`/usr/bin/deno` built against a mismatched `sqlite3` library) crashes immediately:

```
/usr/bin/deno: symbol lookup error: /usr/bin/deno: undefined symbol: sqlite3...
```

yt-dlp detects the non-zero exit and concludes no working runtime exists, even
though a good deno is installed elsewhere (e.g. `~/.deno/bin/deno`).

**Two-part fix:**

1. **`yt-dlp-ejs` Python package** (already listed in
   `requirements-youtube.txt`): ships the challenge-solver script and works
   offline so no `--remote-components` flag is needed.
   ```bash
   pip install yt-dlp-ejs
   ```
2. **Put the good runtime first on `PATH`** (or point directly at it):
   ```bash
   # preferred — fix it globally
   export PATH="$HOME/.deno/bin:$PATH"    # add to ~/.bashrc

   # or — tell this tool specifically
   python tools/fetch_youtube.py --url <CHANNEL> --js-runtimes deno:$HOME/.deno/bin/deno
   ```

**Diagnosis commands:**
```bash
command -v deno     # which deno wins on PATH?
deno --version      # does THAT one actually run? (must exit 0 with a version)
```

If the winning `deno --version` crashes rather than printing a version, it is the
broken one. Remove it, fix its `PATH` position, or use `--js-runtimes` to bypass
detection entirely.

### 6.3 Instagram authentication and rate-limiting

**Authentication challenge.** Instagram blocks or severely rate-limits almost all
unauthenticated requests. Attempting to list a public profile's posts without a
session returns empty results or raises an exception immediately.

**Session file approach.** The tool wraps instaloader's session management in
`load_or_login`:

- `--login` performs an interactive login (password via `getpass.getpass`, never
  echoed or logged), handles 2FA automatically if Instagram raises
  `TwoFactorAuthRequiredException`, and saves the session to
  `instagram/.sessions/<user>.session`.
- Subsequent runs load the session silently; no credentials are requested. A
  missing session file produces a clear error: *"Run once with `--login` first."*
- `--session-file` overrides the default path if needed.

**2FA handling.** The `two_factor_exc` and `twofa_prompt` arguments to
`load_or_login` are injected from `main`, where the real
`TwoFactorAuthRequiredException` is known. This means the pure auth logic is
testable in isolation (passing a fake exception class and a lambda that returns a
canned code) without any real Instagram interaction.

**Rate-limit awareness.** Instagram imposes aggressive rate limits on automated
clients; excessive requests can result in temporary account blocks. Mitigations:

- `--scan-limit` (default 50) bounds how many posts are examined per account per
  run, limiting the number of API calls even on large profiles.
- `--since` filters by upload date so old posts are not re-examined.
- Keep scheduled runs infrequent (e.g. daily, not hourly).
- Use a dedicated login account that does not follow a personal account's contact
  graph.
- instaloader applies its own automatic rate-limit back-off; the tool does not
  bypass or disable it.

The session file is a **credential** and the `instagram/` directory is
gitignored to prevent accidental commits.

### 6.4 Per-platform dedup strategy (archive + on-disk check)

**Problem.** A naive "check the archive, skip if seen" strategy has two failure
modes:

1. **Archive lost or incomplete.** If the archive file is deleted, moved, or was
   never created (e.g. files were copied in manually or from another machine), the
   tool re-downloads everything from scratch — potentially hours of work.
2. **Archive inconsistent with disk.** If a download succeeded but the archive
   write was interrupted (process killed, disk full), the archive says "not seen"
   but the file is already there.

**Solution — two independent mechanisms, working together:**

1. **Download archive** (`{platform}/.download-archive.txt`) — the **source of
   truth**. One line per downloaded item in the format `<platform> <id>` (e.g.
   `youtube dQw4w9WgXcQ`, `instagram CxYzAbcDef1`). Checked first (cheaply, in
   memory from a `set`). Written atomically via append after a successful
   download. Format matches yt-dlp's own `--download-archive` so the file is
   interchangeable with yt-dlp's own tooling.

2. **Filesystem safety net** — before downloading, check whether the destination
   file already exists on disk. If it does but the archive didn't know about it,
   **backfill** the archive entry and skip. This makes the archive **self-healing**:
   re-run after losing the archive and it rebuilds itself from the files on disk
   as they are encountered, without re-downloading any of them.

Both tools implement this pattern identically through the shared
`_media_common.load_archive` / `_media_common.append_archive` helpers, and
the tests (`test_download_new_backfills_when_file_exists`,
`test_download_account_on_disk_check`) verify the backfill behaviour is correct.
