"""Tests for core.runner.run_file using injected fakes (no real models)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import TranscribeConfig  # noqa: E402
from core import runner  # noqa: E402
from core.summary import (  # noqa: E402
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SKIPPED,
)


SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": "salam", "lang": "ar"},
    {"start": 2.5, "end": 4.0, "text": "bonjour", "lang": "fr"},
]


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "medias"
    d = root / "al-oula/2024/06/01"
    d.mkdir(parents=True)
    (d / "20240601090000.mp3").write_bytes(b"\x00")
    return root


def _cfg(out_dir, **kw):
    return TranscribeConfig(out_dir=str(out_dir), **kw)


def test_srt_output_path_mirrors():
    p = runner.srt_output_path("al-oula/2024/06/01/20240601090000.mp3", "out/srt")
    assert p == Path("out/srt/al-oula/2024/06/01/20240601090000.srt")


def test_run_file_success_writes_mirrored_srt(media_root, tmp_path):
    out_dir = tmp_path / "out/srt"
    written = {}

    def fake_transcribe(bundle, path, config):
        return SEGMENTS

    def fake_write(segments, out_path):
        written["path"] = Path(out_path)
        written["segments"] = segments
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("srt")

    res = runner.run_file(
        media_root, "al-oula/2024/06/01/20240601090000.mp3", _cfg(out_dir),
        transcribe_fn=fake_transcribe, write_fn=fake_write,
    )
    assert res.status == STATUS_COMPLETED
    assert res.srt_path == "al-oula/2024/06/01/20240601090000.srt"
    assert written["path"] == out_dir / "al-oula/2024/06/01/20240601090000.srt"
    assert res.audio_seconds == 4.0  # max segment end
    assert res.processing_seconds is not None


def test_run_file_missing_input_is_failed(media_root, tmp_path):
    res = runner.run_file(
        media_root, "al-oula/2024/06/01/nope.mp3", _cfg(tmp_path / "out"),
        transcribe_fn=lambda b, p, c: SEGMENTS, write_fn=lambda s, o: None,
    )
    assert res.status == STATUS_FAILED
    assert "not found" in res.error


def test_run_file_transcribe_exception_is_failed(media_root, tmp_path):
    def boom(bundle, path, config):
        raise RuntimeError("CUDA out of memory")

    res = runner.run_file(
        media_root, "al-oula/2024/06/01/20240601090000.mp3", _cfg(tmp_path / "out"),
        transcribe_fn=boom, write_fn=lambda s, o: None,
    )
    assert res.status == STATUS_FAILED
    assert "CUDA out of memory" in res.error
    assert "RuntimeError" in res.error


def test_run_file_skips_existing(media_root, tmp_path):
    out_dir = tmp_path / "out/srt"
    existing = out_dir / "al-oula/2024/06/01/20240601090000.srt"
    existing.parent.mkdir(parents=True)
    existing.write_text("already here")

    calls = []
    res = runner.run_file(
        media_root, "al-oula/2024/06/01/20240601090000.mp3", _cfg(out_dir),
        transcribe_fn=lambda b, p, c: calls.append(1) or SEGMENTS,
        write_fn=lambda s, o: calls.append("w"),
    )
    assert res.status == STATUS_SKIPPED
    assert calls == []  # transcription never invoked


def test_run_file_overwrite_reprocesses(media_root, tmp_path):
    out_dir = tmp_path / "out/srt"
    existing = out_dir / "al-oula/2024/06/01/20240601090000.srt"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")

    res = runner.run_file(
        media_root, "al-oula/2024/06/01/20240601090000.mp3",
        _cfg(out_dir, overwrite=True),
        transcribe_fn=lambda b, p, c: SEGMENTS,
        write_fn=lambda s, o: Path(o).write_text("new"),
    )
    assert res.status == STATUS_COMPLETED
    assert existing.read_text() == "new"


def test_config_flows_through_to_transcribe(media_root, tmp_path):
    """darija_lora / language / pipeline reach the transcribe call unchanged."""
    captured = {}

    def fake_transcribe(bundle, path, config):
        captured["darija_lora"] = config.darija_lora
        captured["language"] = config.language
        return SEGMENTS

    runner.run_file(
        media_root, "al-oula/2024/06/01/20240601090000.mp3",
        _cfg(tmp_path / "out", darija_lora=True, language="auto"),
        transcribe_fn=fake_transcribe, write_fn=lambda s, o: None,
    )
    assert captured == {"darija_lora": True, "language": "auto"}
