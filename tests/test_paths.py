"""Unit tests for paths.ensure_config - the first-run config bootstrap."""
import pytest

from paths import ensure_config


def test_creates_config_from_example_on_first_run(tmp_path):
    example = tmp_path / "config.example.yaml"
    example.write_text("cameras: {}\n", encoding="utf-8")
    config = tmp_path / "config.yaml"

    result = ensure_config(config, example)

    assert result == config
    assert config.read_text(encoding="utf-8") == "cameras: {}\n"


def test_existing_config_is_left_untouched(tmp_path):
    example = tmp_path / "config.example.yaml"
    example.write_text("template\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("my private settings\n", encoding="utf-8")

    ensure_config(config, example)

    assert config.read_text(encoding="utf-8") == "my private settings\n"


def test_missing_both_files_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ensure_config(tmp_path / "config.yaml", tmp_path / "config.example.yaml")
