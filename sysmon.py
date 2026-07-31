"""System stats for the Wall-Eye dashboard - CPU / RAM / disk / GPU-VRAM.

psutil gives cross-platform CPU/RAM/disk. For the GPU we surface how much model
is resident in VRAM via Ollama's /api/ps - there is no simple portable GPU
util/temp readout on Windows, and "how loaded is the model" is the number that
actually matters here. The parsing and formatting are pure functions so they're
unit-tested without touching hardware.
"""
import logging

import psutil

log = logging.getLogger("walleye")

_GB = 1024 ** 3


def fmt_gb(num_bytes: float) -> str:
    """Bytes -> '11.4 GB'. Pure."""
    return f"{num_bytes / _GB:.1f} GB"


def parse_ollama_ps(ps: dict) -> dict:
    """Turn Ollama's /api/ps JSON into {loaded, vram_gb, names}. Pure."""
    models = (ps or {}).get("models", []) or []
    total = sum(m.get("size_vram", 0) for m in models)
    names = [m.get("name", "?") for m in models]
    return {"loaded": bool(models),
            "vram_gb": round(total / _GB, 1),
            "names": names}


def cpu() -> dict:
    """CPU load. Non-blocking: reads since the last call (call once/sec)."""
    return {"percent": psutil.cpu_percent(interval=None),
            "cores": psutil.cpu_count(logical=True)}


def memory() -> dict:
    m = psutil.virtual_memory()
    return {"used": m.used, "total": m.total, "percent": m.percent,
            "used_gb": round(m.used / _GB, 1), "total_gb": round(m.total / _GB, 1)}


def disk(path: str = "C:\\") -> dict:
    try:
        d = psutil.disk_usage(path)
    except OSError:
        return {"used": 0, "total": 0, "percent": 0, "used_gb": 0, "total_gb": 0}
    return {"used": d.used, "total": d.total, "percent": d.percent,
            "used_gb": round(d.used / _GB, 1), "total_gb": round(d.total / _GB, 1)}


def gpu_vram(url: str = "http://127.0.0.1:11434") -> dict:
    """Model VRAM residency via Ollama. Best-effort - never raises."""
    try:
        import requests
        ps = requests.get(url.rstrip("/") + "/api/ps", timeout=3).json()
        return parse_ollama_ps(ps)
    except Exception:
        return {"loaded": False, "vram_gb": 0.0, "names": [], "error": True}


def snapshot(ollama_url: str = "http://127.0.0.1:11434",
             disk_path: str = "C:\\") -> dict:
    """One combined reading for the dashboard tile."""
    return {"cpu": cpu(), "memory": memory(), "disk": disk(disk_path),
            "gpu": gpu_vram(ollama_url)}
