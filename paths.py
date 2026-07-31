"""Resolve app paths for both `python app.py` and a frozen exe build."""
import shutil
import sys
from pathlib import Path

APP_NAME = "Wall-Eye"   # user-facing app name (window title, toasts, tray)
VERSION = "1.0.0"       # release version (window title, bug reports)
__version__ = VERSION

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent   # config/refs/state live next to the exe
    # PyInstaller lands bundled data files in _internal, not next to the exe
    _BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).parent
    _BUNDLE = ROOT


def _shipped(name: str) -> Path:
    """A read-only file we ship: prefer a copy next to the exe/source, else
    the one PyInstaller bundled, so a bare unzipped exe still starts."""
    local = ROOT / name
    return local if local.exists() else _BUNDLE / name


REFS = ROOT / "refs"
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yaml"          # live config; gitignored (it can
                                            # hold the private ntfy topic)
CONFIG_EXAMPLE = _shipped("config.example.yaml")   # tracked template
ICON_FILE = _shipped("icon.ico")


def ensure_config(config_file: Path = None, example_file: Path = None) -> Path:
    """Create the live config from the shipped example on first run.

    config.yaml is deliberately not tracked in git: the app writes settings
    (including the ntfy topic, which acts as a password) back into it, and a
    committed copy would publish that secret. Returns the config path.
    """
    config_file = CONFIG_FILE if config_file is None else Path(config_file)
    example_file = CONFIG_EXAMPLE if example_file is None else Path(example_file)
    if not config_file.exists():
        if not example_file.exists():
            raise FileNotFoundError(
                f"neither {config_file.name} nor {example_file.name} exists "
                f"in {config_file.parent}")
        shutil.copyfile(example_file, config_file)
    return config_file
LOG_FILE = ROOT / "walleye.log"
REMINDERS_FILE = ROOT / "reminders.json"        # user-set recurring reminders
TODO_FILE = ROOT / "todo.json"                  # To-Do checklist
SHOPPING_FILE = ROOT / "shopping.json"          # Shopping checklist
NOTES_FILE = ROOT / "notes.json"                # quick-capture notes
HABITS_FILE = ROOT / "habits.json"              # daily habits + streaks
SNAP_DIR = ROOT / "snapshots"                   # rolling room-check history
CLEANUP_FILE = ROOT / "cleanup.json"            # cleanup tasks + streak + exclusions
