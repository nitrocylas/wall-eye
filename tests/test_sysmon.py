"""Unit tests for the pure parts of sysmon (formatting + Ollama /api/ps parse)."""
import sysmon

_GB = 1024 ** 3


def test_fmt_gb():
    assert sysmon.fmt_gb(11.4 * _GB) == "11.4 GB"
    assert sysmon.fmt_gb(0) == "0.0 GB"


def test_parse_ollama_ps_loaded():
    ps = {"models": [{"name": "qwen3-vl:8b-instruct", "size_vram": 6 * _GB},
                     {"name": "qwen2.5vl:3b", "size_vram": 1 * _GB}]}
    out = sysmon.parse_ollama_ps(ps)
    assert out["loaded"] is True
    assert out["vram_gb"] == 7.0
    assert out["names"] == ["qwen3-vl:8b-instruct", "qwen2.5vl:3b"]


def test_parse_ollama_ps_idle():
    out = sysmon.parse_ollama_ps({"models": []})
    assert out == {"loaded": False, "vram_gb": 0.0, "names": []}


def test_parse_ollama_ps_handles_junk():
    assert sysmon.parse_ollama_ps({})["loaded"] is False
    assert sysmon.parse_ollama_ps(None)["loaded"] is False


def test_live_readings_have_expected_shape():
    # touches psutil but only asserts structure, so it's stable on any machine
    for fn, keys in ((sysmon.cpu, ("percent", "cores")),
                     (sysmon.memory, ("used", "total", "percent")),
                     (sysmon.disk, ("used", "total", "percent"))):
        out = fn()
        for k in keys:
            assert k in out
