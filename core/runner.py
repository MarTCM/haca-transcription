"""
Transcription runner — the bridge between a :class:`~core.config.TranscribeConfig`
and the existing pipelines in ``transcription/src``.

Responsibilities:
    * Load the heavy models **once** (base faster-whisper / WhisperX model plus the
      optional Darija LoRA pipe) into a :class:`ModelBundle`, so a batch run doesn't
      reload them per file.
    * Transcribe a single file (:func:`run_file`), writing its ``.srt`` to the
      mirrored output path and returning a :class:`~core.summary.FileResult`.
    * Catch per-file failures (CUDA OOM, corrupt/missing file, ...) and report them
      rather than crashing the whole batch.

The heavy ``transcription/src`` modules are imported lazily (inside functions) and
``src/`` is added to ``sys.path`` on demand, so importing this module — and running
the dry-run / selection tests — works without faster-whisper installed.

Tests inject a fake transcribe function via the ``transcribe_fn`` parameter of
:func:`run_file`, exercising all the path/error logic without any model.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from .config import TranscribeConfig, PIPELINE_WHISPERX
from .summary import (
    FileResult,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SKIPPED,
)

# transcription/src holds the existing pipelines + srt_writer.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"


def _ensure_src_on_path() -> None:
    """Add ``transcription/src`` to ``sys.path`` so its modules import cleanly.

    The src modules do ``from srt_writer import write_srt`` (a flat import), so the
    directory itself must be importable.
    """
    p = str(_SRC_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


# --------------------------------------------------------------------------- #
# Model loading (once per run)
# --------------------------------------------------------------------------- #
class ModelBundle:
    """Holds the loaded model(s) for the lifetime of a run.

    Attributes:
        pipeline: the pipeline this bundle was built for.
        model: the loaded faster-whisper / WhisperX model.
        lora_pipe: the Darija LoRA pipeline, or ``None``.
        device: the resolved device string ('cuda' / 'cpu').
    """

    def __init__(self, pipeline: str, model, lora_pipe, device: str):
        self.pipeline = pipeline
        self.model = model
        self.lora_pipe = lora_pipe
        self.device = device


def load_models(config: TranscribeConfig) -> ModelBundle:
    """Load the model(s) described by ``config`` once.

    Picks the faster-whisper or WhisperX backend based on ``config.pipeline`` and
    loads the Darija LoRA pipe when ``config.darija_lora`` is set.
    """
    _ensure_src_on_path()
    if config.pipeline == PIPELINE_WHISPERX:
        import transcribe_whisperx as backend  # type: ignore
    else:
        import transcribe as backend  # type: ignore

    device = config.device
    if device == "auto":
        device = backend._auto_device()

    model = backend.load_model(config.model, device=device, compute_type=None)

    lora_pipe = None
    if config.darija_lora:
        lora_pipe = backend._load_darija_lora(
            config.lora_model, config.lora_base, device
        )

    return ModelBundle(config.pipeline, model, lora_pipe, device)


# --------------------------------------------------------------------------- #
# Transcription dispatch
# --------------------------------------------------------------------------- #
def _default_transcribe(bundle: ModelBundle, path: str,
                        config: TranscribeConfig) -> List[Dict]:
    """Call the appropriate ``transcribe_file`` and return segment dicts.

    Isolated so tests can substitute a stub (see ``transcribe_fn`` on
    :func:`run_file`) and never touch the real models.
    """
    _ensure_src_on_path()
    if bundle.pipeline == PIPELINE_WHISPERX:
        import transcribe_whisperx as backend  # type: ignore
        return backend.transcribe_file(
            path, bundle.model,
            lang=config.language,
            allowed=config.allowed_langs,
            max_chunk_s=config.max_chunk_s,
            batch_size=config.batch_size,
            beam_size=config.beam_size,
            diarize=config.speaker_annotation,
            hf_token=config.hf_token,
            min_speakers=config.min_speakers,
            max_speakers=config.max_speakers,
            device=bundle.device,
            darija_lora=config.darija_lora,
            lora_pipe=bundle.lora_pipe,
        )
    import transcribe as backend  # type: ignore
    return backend.transcribe_file(
        path, bundle.model,
        lang=config.language,
        allowed=config.allowed_langs,
        max_chunk_s=config.max_chunk_s,
        beam_size=config.beam_size,
        darija_lora=config.darija_lora,
        lora_pipe=bundle.lora_pipe,
    )


def _write_srt(segments: List[Dict], out_path: Path) -> None:
    """Write segments to ``out_path`` via the shared srt_writer."""
    _ensure_src_on_path()
    from srt_writer import write_srt  # type: ignore
    write_srt(segments, out_path)


# --------------------------------------------------------------------------- #
# Output path mirroring
# --------------------------------------------------------------------------- #
def srt_output_path(rel_path: str, out_dir: Union[str, Path]) -> Path:
    """Map a relative media path to its mirrored ``.srt`` path under ``out_dir``.

    ``"al-oula/2024/06/01/202406010900.mp3"`` -> ``out_dir/al-oula/2024/06/01/202406010900.srt``
    """
    rel = Path(rel_path)
    return Path(out_dir) / rel.with_suffix(".srt")


def _audio_span(segments: List[Dict]) -> Optional[float]:
    """Approximate transcribed audio length = max segment end time."""
    ends = [float(s["end"]) for s in segments if "end" in s]
    return max(ends) if ends else None


# --------------------------------------------------------------------------- #
# Per-file run
# --------------------------------------------------------------------------- #
def run_file(
    media_root: Union[str, Path],
    rel_path: str,
    config: TranscribeConfig,
    bundle: Optional[ModelBundle] = None,
    *,
    transcribe_fn: Optional[Callable[[ModelBundle, str, TranscribeConfig], List[Dict]]] = None,
    write_fn: Optional[Callable[[List[Dict], Path], None]] = None,
) -> FileResult:
    """Transcribe one file and return its :class:`FileResult`.

    Never raises for ordinary failures (missing/corrupt file, OOM): those are
    captured and returned with ``status == 'failed'``.

    Args:
        media_root: medias root directory.
        rel_path: file path relative to ``media_root``.
        config: run configuration.
        bundle: pre-loaded models. Required unless ``transcribe_fn`` is given.
        transcribe_fn: override for the transcription call (used by tests).
        write_fn: override for the SRT writer (used by tests).
    """
    transcribe = transcribe_fn or _default_transcribe
    write = write_fn or _write_srt

    in_path = Path(media_root) / rel_path
    out_path = srt_output_path(rel_path, config.out_dir)
    rel_srt = str(Path(rel_path).with_suffix(".srt"))

    # Skip already-done files unless overwrite is requested.
    if out_path.exists() and not config.overwrite:
        return FileResult(rel_path, STATUS_SKIPPED, srt_path=rel_srt)

    if not in_path.exists():
        return FileResult(rel_path, STATUS_FAILED,
                          error=f"file not found: {in_path}")

    t0 = time.monotonic()
    try:
        segments = transcribe(bundle, str(in_path), config)
        write(segments, out_path)
    except Exception as exc:  # noqa: BLE001 — per-file isolation is intentional
        return FileResult(rel_path, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")

    return FileResult(
        rel_path,
        STATUS_COMPLETED,
        srt_path=rel_srt,
        audio_seconds=_audio_span(segments),
        processing_seconds=time.monotonic() - t0,
    )
