"""Tests for core.config.TranscribeConfig."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import (  # noqa: E402
    ConfigError,
    TranscribeConfig,
    PIPELINE_FASTER_WHISPER,
    PIPELINE_WHISPERX,
)


def test_defaults_are_recommended():
    cfg = TranscribeConfig()
    assert cfg.pipeline == PIPELINE_FASTER_WHISPER
    assert cfg.model == "large-v3"
    assert cfg.darija_lora is True
    assert cfg.language == "auto"
    assert cfg.allowed_langs == ("ar", "fr", "en", "es")
    assert cfg.speaker_annotation is False
    assert cfg.out_dir == "out/srt"


def test_default_config_validates():
    assert TranscribeConfig().validate() is not None


def test_allowed_langs_string_is_normalized():
    cfg = TranscribeConfig(allowed_langs="ar, fr ,en")
    assert cfg.allowed_langs == ("ar", "fr", "en")


def test_annotation_requires_whisperx():
    cfg = TranscribeConfig(speaker_annotation=True,
                           pipeline=PIPELINE_FASTER_WHISPER,
                           hf_token="hf_x")
    with pytest.raises(ConfigError, match="whisperx"):
        cfg.validate()


def test_annotation_requires_token():
    cfg = TranscribeConfig(speaker_annotation=True,
                           pipeline=PIPELINE_WHISPERX,
                           hf_token=None)
    with pytest.raises(ConfigError, match="token"):
        cfg.validate()


def test_annotation_ok_with_whisperx_and_token():
    cfg = TranscribeConfig(speaker_annotation=True,
                           pipeline=PIPELINE_WHISPERX,
                           hf_token="hf_x")
    assert cfg.validate() is cfg


def test_invalid_pipeline():
    with pytest.raises(ConfigError, match="pipeline"):
        TranscribeConfig(pipeline="banana").validate()


def test_invalid_device():
    with pytest.raises(ConfigError, match="device"):
        TranscribeConfig(device="tpu").validate()


def test_invalid_max_chunk():
    with pytest.raises(ConfigError, match="max_chunk_s"):
        TranscribeConfig(max_chunk_s=0).validate()


def test_with_overrides():
    base = TranscribeConfig()
    derived = base.with_overrides(model="large-v3-turbo", overwrite=True)
    assert derived.model == "large-v3-turbo"
    assert derived.overwrite is True
    assert base.model == "large-v3"  # original untouched


def test_summary_str_contains_key_fields():
    s = TranscribeConfig().summary_str()
    assert "pipeline=faster-whisper" in s
    assert "darija_lora=true" in s
    assert "speaker_annotation=false" in s
