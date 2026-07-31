"""Error reporting shared by the GUI and the headless engine.

Two jobs:
1. Build a paste-ready bug report for GitHub issues (build_bug_report).
2. Install a global excepthook that logs every unhandled exception to the
   app log and hands the finished report to a presenter (the GUI's crash
   dialog) or, headless, prints one concise line to stderr.
"""
import logging
import platform
import sys
import threading
import traceback

from paths import APP_NAME, LOG_FILE, VERSION

log = logging.getLogger("walleye")

GITHUB_ISSUE_NOTE = ("Please open a GitHub issue and paste this whole "
                     "report into it.")

# The exact prefix of the friendly "Ollama is down" message; the GUI/engine
# match on it to tell a setup problem apart from a real bug.
_OLLAMA_UNREACHABLE_PREFIX = "Ollama is not reachable"


def format_platform_info() -> str:
    """One line describing the interpreter and OS, e.g.
    'Python 3.11.9 on Windows 11 (10.0.26200)'."""
    return (f"Python {platform.python_version()} on "
            f"{platform.system()} {platform.release()} "
            f"({platform.version()})")


def ollama_unreachable_message(url: str) -> str:
    """The friendly setup hint shown when the Ollama server cannot be reached."""
    return (f"{_OLLAMA_UNREACHABLE_PREFIX} at {url} - is it installed and "
            "running? (ollama serve, then: ollama pull qwen3-vl:8b-instruct)")


def is_ollama_unreachable(message: object) -> bool:
    """True if `message` is the friendly Ollama-down hint (not a real bug)."""
    return str(message or "").startswith(_OLLAMA_UNREACHABLE_PREFIX)


def build_bug_report(version: str, exc_type: type, exc: BaseException,
                     tb_text: str, platform_info: str) -> str:
    """Assemble the text a user pastes into a GitHub issue.

    Pure function: adds only the arguments it is given (version, error,
    traceback, platform line) - it never mixes in the working directory,
    home folder, or other personal paths beyond what the traceback itself
    carries.
    """
    exc_name = getattr(exc_type, "__name__", str(exc_type))
    return "\n".join([
        f"{APP_NAME} bug report",
        f"Version: {version}",
        f"Platform: {platform_info}",
        f"Error: {exc_name}: {exc}",
        "",
        "Traceback:",
        str(tb_text or "").rstrip(),
        "",
        GITHUB_ISSUE_NOTE,
    ])


def install_excepthook(show_report=None) -> None:
    """Route unhandled exceptions (main thread AND worker threads) through
    one handler that logs the full traceback to the app log, then either
    presents the report via `show_report(report_text)` (the GUI dialog) or
    prints a concise line to stderr (headless / presenter unavailable).

    The handler itself must never raise: every step is wrapped, ending in a
    bare stderr fallback.
    """
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        try:
            tb_text = "".join(traceback.format_exception(exc_type, exc, tb))
        except Exception:
            tb_text = f"(traceback unavailable) {exc_type}: {exc}"
        try:
            report = build_bug_report(VERSION, exc_type, exc, tb_text,
                                      format_platform_info())
        except Exception:
            report = f"{APP_NAME} {VERSION} crashed: {exc_type}: {exc}\n{tb_text}"
        try:
            log.critical("unhandled exception:\n%s", report)
        except Exception:
            pass
        if show_report is not None:
            try:
                show_report(report)
                return
            except Exception:
                pass   # fall through to the stderr line
        try:
            exc_name = getattr(exc_type, "__name__", str(exc_type))
            sys.stderr.write(
                f"{APP_NAME} {VERSION} crashed: {exc_name}: {exc} "
                f"- full report in {LOG_FILE}\n")
        except Exception:
            pass

    def thread_hook(args):
        hook(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = hook
    threading.excepthook = thread_hook
