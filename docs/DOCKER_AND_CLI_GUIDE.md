# Docker & CLI Guide

A practical guide to running the batch transcription CLI — both natively and via
the GPU Docker image. For internals/design see `docs/CLI_ARCHITECTURE.md`.

---

## 1. What the CLI does

It walks a `medias/` tree, picks the files matching your filters, and writes one
mirrored `.srt` per input:

```
medias/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.mp3   # input
out/srt/{channel}/{year}/{month}/{day}/{YYYYMMDDHHMMSS}.srt  # output (mirrors input)
out/logs/cli-<timestamp>.log                                 # run log
```

Filenames are a 14-digit `YYYYMMDDHHMMSS` stamp + a media extension (audio or
video). The broadcast hour (positions 8–9) is what `--hours` matches.

---

## 2. CLI command guide

General shape:

```bash
python cli.py [--medias DIR] [filters] [model options] [output options]
```

### Filters (omit any flag to mean "all")

| Flag | Meaning | Example |
|------|---------|---------|
| `--medias DIR` | Root of the medias tree (default `medias`). | `--medias /data/medias` |
| `--channel` | Channel name(s); repeatable and/or comma-separated. | `--channel al-oula,2m` |
| `--year` | Year list and/or range. | `--year 2024-2025` |
| `--month` | Month `1-12`, list and/or range. | `--month 1-6` |
| `--day` | Day `1-31`, list and/or range. | `--day 1,15,30` |
| `--hours` / `--hour` | Hour `0-23`, list and/or range. | `--hours 9-18,21` |

Range/list grammar: comma-separated tokens, each a single value (`21`) or an
inclusive range (`9-18`). Reversed ranges (`18-9`) are rejected.

### Headline option

| Flag | Meaning |
|------|---------|
| `--speaker-annotation` | Enable speaker diarization. Implies `--pipeline whisperx` and **requires** `--hf-token` or `$HF_TOKEN`. Off by default. |

### Model options (sensible defaults)

| Flag | Default | Meaning |
|------|---------|---------|
| `--pipeline` | `faster-whisper` | Backend: `faster-whisper` or `whisperx`. |
| `--model` | `large-v3` | faster-whisper model size or local path. |
| `--darija-lora` / `--no-darija-lora` | on | Route Arabic chunks through the Darija LoRA. |
| `--lang` | `auto` | `auto` per-chunk detection, or a forced code (`ar`/`fr`/`en`). |
| `--allowed` | `ar,fr,en,es` | Allow-list for auto language detection. |
| `--max-chunk-s` | `25.0` | Max VAD chunk length (seconds). |
| `--device` | `auto` | `auto` / `cuda` / `cpu`. |
| `--overwrite` | off | Re-transcribe even if the `.srt` exists (otherwise skipped). |
| `--hf-token` | — | Hugging Face token for diarization (or set `$HF_TOKEN`). |

### Output & behavior

| Flag | Default | Meaning |
|------|---------|---------|
| `--out-dir` | `out/srt` | Output root; `.srt` files mirror the medias tree. |
| `--log-file` | `out/logs/cli-<timestamp>.log` | Run log path. |
| `--dry-run` | off | List the matched files and exit without transcribing. |
| `-v`, `--verbose` | off | Print each per-file log line to stderr live. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All matched files completed (or dry-run succeeded). |
| `1` | Run finished but at least one file failed. |
| `2` | Usage error (bad range, no medias dir, no matches, annotation without token). |

### Examples

```bash
# Dry-run: list what would be transcribed, run no models.
python cli.py --channel al-oula --year 2024 --month 6 --hours 9-18 --dry-run

# Transcribe two channels, June 2024, all hours, with the defaults.
python cli.py --channel al-oula,2m --year 2024 --month 6

# Speaker annotation (WhisperX + diarization); needs an HF token.
python cli.py --channel 2m --year 2024 --month 6 --day 1 \
    --speaker-annotation --hf-token hf_xxx

# Force CPU, disable the Darija LoRA, custom output dir.
python cli.py --channel al-oula --device cpu --no-darija-lora --out-dir /tmp/srt
```

---

## 3. Run natively (no Docker)

If you have Python + an NVIDIA driver on the machine, you can clone the repo and
run the CLI directly — no image build needed. `core/runner.py` adds `src/` to
`sys.path` automatically, so only the Python deps are required.

### Prerequisites

- NVIDIA driver for GPU runs (`nvidia-smi` should work). The CUDA channel you install
  torch from must match the driver: **cu128** needs driver ~570+, **cu126** suits older drivers.
- `git` and **Python 3.10–3.12** with `pip` (64-bit). **Not 3.14**: PyTorch publishes wheels
  only up to Python 3.13, so a too-new Python fails with *"Could not find a version that
  satisfies the requirement torch"*. Python 3.12 is the safe choice.
- **`ffmpeg` on `PATH`.** faster-whisper's own decode uses bundled PyAV (no FFmpeg, video
  included), **but** the default **Darija LoRA** path and `--speaker-annotation` go through
  `transformers`/`torchcodec`, which needs the system FFmpeg libraries. Install it —
  Linux `apt install ffmpeg`, macOS `brew install ffmpeg`, Windows `winget install Gyan.FFmpeg`
  (reopen the shell afterwards). Without it you hit *"Could not load libtorchcodec"*.

### Linux / macOS

```bash
git clone https://github.com/MarTCM/haca-transcription.git
cd haca-transcription

python3.12 -m venv .venv   # use a 3.10-3.12 interpreter (not 3.13+/3.14)
source .venv/bin/activate
python -m pip install --upgrade pip

# CUDA-matched torch FIRST, pinned to 2.8.0 so it satisfies whisperx/pyannote
# (which require torch~=2.8.0). Pick the channel for your driver: cu128 (>=570) or cu126.
pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 \
    --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
pip install -r requirements_whisperx.txt      # only for --speaker-annotation
pip install "torchcodec==0.7.*"               # decode backend for the LoRA + WhisperX paths

export HF_TOKEN=hf_xxx                         # only for --speaker-annotation

# Dry-run first, then the real run (GPU auto-detected):
python cli.py --medias /data/medias --channel al-oula --year 2024 --month 6 --dry-run
python cli.py --medias /data/medias --channel al-oula --year 2024 --month 6
```

For a **CPU-only** machine, install the same pinned versions without the CUDA index
(`pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0`) and pass `--device cpu`
(slow — fine for a smoke test with `--model tiny`).

### Windows (PowerShell)

```powershell
git clone https://github.com/MarTCM/haca-transcription.git
cd haca-transcription

py -3.12 -m venv .venv     # 3.12 recommended; `py -0p` lists installed versions
.\.venv\Scripts\Activate.ps1
python --version           # confirm it says 3.12.x (or 3.10/3.11), not 3.13+/3.14
python -m pip install --upgrade pip

# CUDA-matched torch FIRST, pinned to 2.8.0 (satisfies whisperx/pyannote which need
# torch~=2.8.0). Pick the channel for your driver: cu128 (>=570) or cu126. One line each.
pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
pip install -r requirements_whisperx.txt       # only for --speaker-annotation
pip install "torchcodec==0.7.*"                # decode backend for the LoRA + WhisperX paths

$env:HF_TOKEN = "hf_xxx"                        # only for --speaker-annotation

# Dry-run first, then the real run:
python cli.py --medias C:\data\medias --channel al-oula --year 2024 --month 6 --dry-run
python cli.py --medias C:\data\medias --channel al-oula --year 2024 --month 6
```

Windows notes:
- If `Activate.ps1` is blocked by execution policy, either run it once via
  `powershell -ExecutionPolicy Bypass -File .\.venv\Scripts\Activate.ps1`, or use
  the Command Prompt activator `.\.venv\Scripts\activate.bat`.
- In **cmd.exe**, set the token with `set HF_TOKEN=hf_xxx` (no `$env:`).
- Paths may use `C:\...` (backslashes) or forward slashes — both work. Keep the
  CLI flags identical to Linux.
- The multi-line commands above are one-per-line; don't add `\` continuations
  (that's a shell-ism, not Windows).
- `ffmpeg` isn't bundled and **is needed by the Darija LoRA and `--speaker-annotation`
  paths** (via torchcodec) — install it with `winget install Gyan.FFmpeg` and reopen the
  shell so it's on `PATH`.
- After installing torch, **verify CUDA is actually on** — check the build tag, not just
  the bool: `python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`
  should print e.g. `2.8.0+cu128 True`. If it ends in `+cpu`, see the troubleshooting table.

### What to expect

- **First run downloads models** (large-v3 ~3 GB + the Darija LoRA) from Hugging
  Face into the HF cache (`~/.cache/huggingface`, or
  `%USERPROFILE%\.cache\huggingface` on Windows). Later runs reuse them. Set
  `HF_HOME` to relocate that cache.
- Outputs land in `out/srt/...` (mirroring the tree) and a log in
  `out/logs/cli-<timestamp>.log`. Add `-v` to stream per-file progress.
- **Sanity check without a GPU**: `python -m pytest tests/ -q` runs the selection
  /CLI tests with stubs — no models or GPU needed — to confirm the checkout is
  intact.
- Unlike the `--no-cache-dir` Docker build, native `pip` caches downloads, so an
  interrupted install resumes when you re-run the same command.

---

## 4. Docker process

The GPU image bakes the CLI as its entrypoint: **everything after the image name
is a CLI flag**. The image is built from `Dockerfile.gpu` on a
`nvidia/cuda:12.4.1-cudnn-runtime` base, installs the requirements + WhisperX +
torch (CUDA 12.4) + ffmpeg, and downloads models to `$HF_HOME=/cache/huggingface`.

### Host prerequisites

- NVIDIA driver compatible with CUDA 12.4.
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  (needed for `--gpus all`).
- A driver new enough for the `cu124` torch wheel and the CUDA 12.4 base image.

### Volumes you mount

| Container path | Purpose | Mode |
|----------------|---------|------|
| `/app/medias` | Input medias tree (compose default). | read-only |
| `/app/out` | Output `.srt` files + run logs (written back to host). | read-write |
| `/cache/huggingface` | Persistent model cache (multi-GB; download once). | read-write (named volume) |

### A. Build the image

```bash
# From the repo root (build context = transcription/):
docker build -f transcription/Dockerfile.gpu -t haca-transcribe:gpu transcription/
```

### B. Run with plain `docker run`

```bash
docker run --rm --gpus all \
  -v /data/medias:/data/medias:ro \
  -v /data/out:/app/out \
  -v haca-model-cache:/cache/huggingface \
  -e HF_TOKEN=hf_xxx \
  haca-transcribe:gpu \
  --medias /data/medias --channel al-oula --year 2024 --month 6
```

- `--gpus all` exposes the GPU; `--device auto` (default) then picks CUDA.
- `-e HF_TOKEN` is only needed for `--speaker-annotation`.
- The named volume `haca-model-cache` keeps downloaded models across runs.
- Run with no flags to print `--help`.

### C. Run with Docker Compose (recommended for repeat runs)

`docker-compose.yml` wires up the GPU reservation, the three mounts, and the
`HF_TOKEN` passthrough. The `IMAGE` env var selects which image to run (defaults
to the locally-built `haca-transcribe:gpu`).

Local machine (build, then run):

```bash
export MEDIAS_DIR=/data/medias          # host path to your medias tree
docker compose build
docker compose run --rm transcribe --channel al-oula --year 2024 --month 6
docker compose run --rm transcribe --dry-run   # any CLI flag works
```

GPU box (pull a published image — no build needed):

```bash
export IMAGE=<DOCKERHUB_USER>/haca-transcribe:gpu
export MEDIAS_DIR=/data/medias
export HF_TOKEN=hf_xxx                   # only for --speaker-annotation
docker compose pull
docker compose run --rm transcribe --channel al-oula --year 2024 --month 6
```

Notes:
- Medias is mounted at the CLI's default `/app/medias`, so you don't pass
  `--medias`. Outputs land in `./out` on the host.
- Args after `transcribe` **replace** the service command and go straight to
  `python3 cli.py`, so flags are identical to a native run.

### D. Publish to Docker Hub (optional)

```bash
docker tag haca-transcribe:gpu <DOCKERHUB_USER>/haca-transcribe:gpu
docker login
docker push <DOCKERHUB_USER>/haca-transcribe:gpu
```

Then on the GPU box, set `IMAGE=<DOCKERHUB_USER>/haca-transcribe:gpu` and
`docker compose pull` as shown above.

---

## 5. Reading the run log

Every run writes a log whose lines match the format shared with the web UI:

```
[JOB START]  2026-06-24T11:40:00 | 3 files | pipeline=faster-whisper | ...
[OK]         2026-06-24T11:40:46 | al-oula/2024/06/01/20240601090000.mp3 | 44.8s
[FAIL]       2026-06-24T11:41:02 | al-oula/2024/06/01/20240601100000.mp3 | RuntimeError: CUDA out of memory
[SKIP]       2026-06-24T11:41:02 | al-oula/2024/06/01/20240601230000.mp3 | exists (use --overwrite)
[JOB END]    2026-06-24T11:41:30 | failed | 2/3 | 1 ok, 1 failed
```

- `[OK]` — transcribed; shows processing seconds.
- `[SKIP]` — `.srt` already existed (re-run with `--overwrite` to redo).
- `[FAIL]` — error message captured; the run continues and exits `1`.

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `cannot load libcudnn / libcublas` (Docker) | Use the `-cudnn-` CUDA base (already in `Dockerfile.gpu`); ensure `--gpus all` and the NVIDIA Container Toolkit. |
| `cannot load libcudnn / libcublas` (native, Linux) | CTranslate2 can't find the cuDNN/cuBLAS shipped in the torch wheel. Add them to the loader path: `export LD_LIBRARY_PATH=$(python -c "import os,nvidia.cudnn,nvidia.cublas; print(':'.join(os.path.join(os.path.dirname(m.__file__),'lib') for m in [nvidia.cudnn, nvidia.cublas]))"):$LD_LIBRARY_PATH`. Or verify the pipeline first with `--device cpu`. |
| `cannot load cudnn*.dll` (native, Windows) | Add the wheel's CUDA DLL dirs to `PATH`, e.g. `.venv\Lib\site-packages\nvidia\cudnn\bin` and `...\nvidia\cublas\bin`, then reopen the shell. Or test with `--device cpu` first. |
| `Activate.ps1 cannot be loaded` (Windows) | PowerShell execution policy. Use `.\.venv\Scripts\activate.bat` (cmd) or run `powershell -ExecutionPolicy Bypass -File .\.venv\Scripts\Activate.ps1`. |
| `Could not find a version that satisfies the requirement torch` (native) | No torch wheel for your interpreter. Usually Python is too new (no torch wheels for 3.14) or it's 32-bit. Recreate the venv with 64-bit **Python 3.10–3.12** (`py -3.12 -m venv .venv` on Windows). Confirm with `python --version` and `python -c "import struct;print(struct.calcsize('P')*8)"`. |
| `Could not load libtorchcodec` (native) | The Darija LoRA / WhisperX path uses `torchcodec`, which needs the system **FFmpeg** libraries (faster-whisper's own PyAV decode does not). Install FFmpeg (`winget install Gyan.FFmpeg` on Windows; `apt/brew install ffmpeg` otherwise) and reopen the shell. Quick check it's the LoRA: a run with `--no-darija-lora` uses only faster-whisper (no torchcodec) and should succeed. |
| `torch.cuda.is_available()` is False / torch shows `+cpu` | The CPU wheel is installed. `pip install torch …` is a **no-op when torch is already present**, so the CUDA wheel never replaced it. Force it: `python -m pip uninstall -y torch torchvision torchaudio` then reinstall pinned from the CUDA channel (below). Confirm the printed version ends in `+cu128`/`+cu126`, and that `nvidia-smi` works (no NVIDIA GPU → CUDA can't be enabled). Use `python -m pip` so it hits the same interpreter you test with. |
| `pip ... dependency conflicts: whisperx requires torch~=2.8.0 but you have 2.11.0` | The unpinned install pulled the newest torch; whisperx/pyannote pin the stack to 2.8.0. Reinstall pinned: `python -m pip install --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0` then `python -m pip install "torchcodec==0.7.*"`. |
| Build fails with `ReadTimeoutError` / `Read timed out` during `pip install` | A large wheel timed out on a slow/flaky link (the cu124 torch stack is ~2.5 GB). The Dockerfile uses a BuildKit pip **cache mount** + split layers, so downloads are **resumable**: just re-run `docker build` and it continues from the cached wheels instead of restarting. Requires BuildKit (default in modern Docker; otherwise prefix `DOCKER_BUILDKIT=1`). On a very slow link, raise `--timeout` further. For a **native** install, pip caches by default — just re-run the same `pip install`. |
| `0 files matched the given filters.` (exit 2) | Filters too narrow or wrong `--medias` path; try `--dry-run`. |
| `--speaker-annotation` errors about token (exit 2) | Pass `--hf-token` or set `$HF_TOKEN` (`$env:HF_TOKEN` / `set HF_TOKEN=` on Windows). |
| Models re-download every run | Docker: mount a persistent volume at `/cache/huggingface` (compose does this via `haca-model-cache`). Native: keep the same `HF_HOME` / default `~/.cache/huggingface`. |
| Slow / runs on CPU | Docker: confirm `--gpus all`, the toolkit, and `--device auto`/`cuda`. Native: confirm the cu124 torch is installed (`python -c "import torch; print(torch.cuda.is_available())"`). |
| WhisperX can't read audio | Needs the `ffmpeg` binary on `PATH` (faster-whisper doesn't; WhisperX does). The Docker image installs it; for native runs install ffmpeg yourself. |
