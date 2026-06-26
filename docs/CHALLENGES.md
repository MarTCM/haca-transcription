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
