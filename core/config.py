"""
Shared transcription configuration.

``TranscribeConfig`` is the single source of truth for every option that affects
a transcription run. Both the batch CLI (``transcription/cli.py``) and the
FastAPI backend build one of these and hand it to :mod:`core.runner`, so the two
front-ends always behave identically.

Recommended defaults are baked in (faster-whisper ``large-v3`` with the Darija
LoRA on, per-chunk language detection over ``ar,fr,en``). Speaker annotation is
off by default; turning it on requires the WhisperX pipeline and a Hugging Face
token.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Sequence, Tuple

# Allowed pipeline identifiers.
PIPELINE_FASTER_WHISPER = "faster-whisper"
PIPELINE_WHISPERX = "whisperx"
VALID_PIPELINES = (PIPELINE_FASTER_WHISPER, PIPELINE_WHISPERX)

DEFAULT_ALLOWED: Tuple[str, ...] = ("ar", "fr", "en", "es")
DEFAULT_OUT_DIR = "out/srt"

# Darija LoRA adapter (anaszil on large-v3-turbo) — the recommended Darija setup.
DEFAULT_LORA_MODEL = "anaszil/whisper-large-v3-turbo-darija"
DEFAULT_LORA_BASE = "openai/whisper-large-v3-turbo"


class ConfigError(ValueError):
    """Raised when a ``TranscribeConfig`` is internally inconsistent."""


@dataclass
class TranscribeConfig:
    """Everything needed to transcribe a set of files.

    Attributes:
        pipeline: ``"faster-whisper"`` (default) or ``"whisperx"``. Speaker
            annotation requires ``"whisperx"``.
        model: faster-whisper model size or local path.
        darija_lora: route Arabic (``ar``) chunks through the anaszil Darija
            LoRA adapter while French/English chunks stay on ``model``.
        language: ``"auto"`` for per-chunk detection, or a forced code (``ar``,
            ``fr``, ...).
        allowed_langs: allow-list used when ``language == "auto"``.
        max_chunk_s: maximum VAD chunk length in seconds.
        device: ``"auto"`` / ``"cuda"`` / ``"cpu"``.
        overwrite: re-transcribe even if the target ``.srt`` already exists.
        speaker_annotation: enable pyannote diarization (WhisperX only).
        hf_token: Hugging Face token, required when ``speaker_annotation`` is on.
        out_dir: root output directory; SRTs mirror the medias arborescence
            underneath it.
        beam_size: decoder beam size.
        batch_size: WhisperX batch size (ignored by faster-whisper).
        min_speakers / max_speakers: optional diarization hints.
        lora_model / lora_base: LoRA adapter + its base model.
    """

    # Headline options.
    pipeline: str = PIPELINE_FASTER_WHISPER
    speaker_annotation: bool = False
    hf_token: Optional[str] = None

    # Defaulted-but-overridable model options.
    model: str = "large-v3"
    darija_lora: bool = True
    language: str = "auto"
    allowed_langs: Tuple[str, ...] = DEFAULT_ALLOWED
    max_chunk_s: float = 25.0
    device: str = "auto"
    overwrite: bool = False
    out_dir: str = DEFAULT_OUT_DIR
    beam_size: int = 5

    # WhisperX / diarization extras.
    batch_size: int = 8
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None

    # LoRA adapter.
    lora_model: str = DEFAULT_LORA_MODEL
    lora_base: str = DEFAULT_LORA_BASE

    def __post_init__(self) -> None:
        # Normalize allowed_langs to a tuple (callers may pass a list).
        if isinstance(self.allowed_langs, str):
            self.allowed_langs = tuple(
                x.strip() for x in self.allowed_langs.split(",") if x.strip()
            )
        else:
            self.allowed_langs = tuple(self.allowed_langs)

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate(self) -> "TranscribeConfig":
        """Validate the configuration, raising :class:`ConfigError` on problems.

        Returns ``self`` so it can be chained: ``cfg = TranscribeConfig(...).validate()``.
        """
        if self.pipeline not in VALID_PIPELINES:
            raise ConfigError(
                f"pipeline must be one of {VALID_PIPELINES}, got {self.pipeline!r}"
            )
        if self.speaker_annotation:
            if self.pipeline != PIPELINE_WHISPERX:
                raise ConfigError(
                    "speaker_annotation requires pipeline='whisperx' "
                    f"(got {self.pipeline!r})"
                )
            if not self.hf_token:
                raise ConfigError(
                    "speaker_annotation requires a Hugging Face token "
                    "(set hf_token or the HF_TOKEN environment variable)"
                )
        if self.max_chunk_s <= 0:
            raise ConfigError(f"max_chunk_s must be > 0, got {self.max_chunk_s}")
        if self.device not in ("auto", "cuda", "cpu"):
            raise ConfigError(
                f"device must be 'auto', 'cuda' or 'cpu', got {self.device!r}"
            )
        if not self.allowed_langs:
            raise ConfigError("allowed_langs must not be empty")
        return self

    def with_overrides(self, **kwargs) -> "TranscribeConfig":
        """Return a copy with the given fields replaced."""
        return replace(self, **kwargs)

    def summary_str(self) -> str:
        """One-line human summary used in log headers."""
        bits = [
            f"pipeline={self.pipeline}",
            f"model={self.model}",
            f"darija_lora={str(self.darija_lora).lower()}",
            f"language={self.language}",
            f"speaker_annotation={str(self.speaker_annotation).lower()}",
        ]
        return " | ".join(bits)
