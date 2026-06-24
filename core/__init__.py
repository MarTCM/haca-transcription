"""Shared transcription core — imported by both the batch CLI and the FastAPI backend.

Modules:
    config    — TranscribeConfig dataclass + defaults + validation
    selection — range/list parsing, medias scan, expand_selections()
    runner    — load models once, run_file(), mirrored SRT output, error capture
    summary   — structured run log (JOB START/OK/FAIL/JOB END) + aggregation
"""

from .config import (  # noqa: F401
    ConfigError,
    TranscribeConfig,
    PIPELINE_FASTER_WHISPER,
    PIPELINE_WHISPERX,
)

__all__ = [
    "ConfigError",
    "TranscribeConfig",
    "PIPELINE_FASTER_WHISPER",
    "PIPELINE_WHISPERX",
]
