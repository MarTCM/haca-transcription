"""Tests for cli.py: parsing, config building, dry-run, and a stubbed run."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402
from core import runner, summary  # noqa: E402
from core.config import PIPELINE_WHISPERX  # noqa: E402
from core.summary import FileResult, STATUS_COMPLETED, STATUS_FAILED  # noqa: E402


@pytest.fixture
def medias(tmp_path):
    layout = {
        "al-oula/2024/06/01": ["202406010900", "202406011000", "202406012300"],
        "2m/2024/06/02": ["202406020900", "202406021800"],
    }
    for rel, stamps in layout.items():
        d = tmp_path / "medias" / rel
        d.mkdir(parents=True)
        for stamp in stamps:
            (d / f"{stamp}.mp3").write_bytes(b"\x00")
    return tmp_path / "medias"


# --------------------------------------------------------------------------- #
# Parsing & config building
# --------------------------------------------------------------------------- #
def test_parser_collects_filters():
    args = cli.build_parser().parse_args(
        ["--channel", "al-oula", "--channel", "2m",
         "--year", "2024", "--hours", "9-18,21"]
    )
    assert args.channel == ["al-oula", "2m"]
    assert args.year == "2024"
    assert args.hours == "9-18,21"


def test_hour_alias():
    args = cli.build_parser().parse_args(["--hour", "9"])
    assert args.hours == "9"


def test_no_darija_lora_flag():
    args = cli.build_parser().parse_args(["--no-darija-lora"])
    assert args.darija_lora is False
    args2 = cli.build_parser().parse_args([])
    assert args2.darija_lora is True


def test_config_annotation_sets_whisperx(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    args = cli.build_parser().parse_args(["--speaker-annotation", "--hf-token", "hf_x"])
    cfg = cli.config_from_args(args)
    assert cfg.pipeline == PIPELINE_WHISPERX
    assert cfg.speaker_annotation is True


def test_config_annotation_without_token_errors(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    args = cli.build_parser().parse_args(["--speaker-annotation"])
    with pytest.raises(cli.ConfigError):
        cli.config_from_args(args)


def test_config_annotation_uses_env_token(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_from_env")
    args = cli.build_parser().parse_args(["--speaker-annotation"])
    cfg = cli.config_from_args(args)
    assert cfg.hf_token == "hf_from_env"


# --------------------------------------------------------------------------- #
# main() — dry-run and error paths
# --------------------------------------------------------------------------- #
def test_dry_run_lists_files(medias, capsys):
    rc = cli.main(["--medias", str(medias), "--channel", "al-oula",
                   "--hours", "9-12", "--dry-run"])
    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out.splitlines()
    assert "al-oula/2024/06/01/202406010900.mp3" in out
    assert "al-oula/2024/06/01/202406011000.mp3" in out
    # 23:00 excluded by the hour filter
    assert "al-oula/2024/06/01/202406012300.mp3" not in out


def test_missing_medias_dir(tmp_path):
    rc = cli.main(["--medias", str(tmp_path / "nope"), "--dry-run"])
    assert rc == cli.EXIT_USAGE


def test_no_match_returns_usage(medias):
    rc = cli.main(["--medias", str(medias), "--channel", "ghost", "--dry-run"])
    assert rc == cli.EXIT_USAGE


def test_bad_range_returns_usage(medias):
    rc = cli.main(["--medias", str(medias), "--hours", "18-9", "--dry-run"])
    assert rc == cli.EXIT_USAGE


def test_annotation_without_token_returns_usage(medias, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    rc = cli.main(["--medias", str(medias), "--speaker-annotation", "--dry-run"])
    assert rc == cli.EXIT_USAGE


# --------------------------------------------------------------------------- #
# main() — full run with stubbed models
# --------------------------------------------------------------------------- #
def test_full_run_all_ok(medias, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "load_models", lambda cfg: object())
    monkeypatch.setattr(
        runner, "run_file",
        lambda root, rel, cfg, bundle: FileResult(
            rel, STATUS_COMPLETED, srt_path=rel.replace(".mp3", ".srt"),
            processing_seconds=1.0,
        ),
    )
    log_file = tmp_path / "run.log"
    rc = cli.main(["--medias", str(medias), "--channel", "2m",
                   "--out-dir", str(tmp_path / "out/srt"),
                   "--log-file", str(log_file)])
    assert rc == cli.EXIT_OK
    text = log_file.read_text()
    assert "[JOB START]" in text
    assert "[OK]" in text
    assert "[JOB END]" in text and "2 ok, 0 failed" in text


def test_full_run_with_failure_sets_exit_code(medias, tmp_path, monkeypatch):
    def stub_run(root, rel, cfg, bundle):
        if rel.endswith("202406020900.mp3"):
            return FileResult(rel, STATUS_FAILED, error="boom")
        return FileResult(rel, STATUS_COMPLETED, processing_seconds=1.0)

    monkeypatch.setattr(runner, "load_models", lambda cfg: object())
    monkeypatch.setattr(runner, "run_file", stub_run)
    log_file = tmp_path / "run.log"
    rc = cli.main(["--medias", str(medias), "--channel", "2m",
                   "--out-dir", str(tmp_path / "out/srt"),
                   "--log-file", str(log_file)])
    assert rc == cli.EXIT_SOME_FAILED
    text = log_file.read_text()
    assert "[FAIL]" in text and "boom" in text
    assert "1 ok, 1 failed" in text
