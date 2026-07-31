"""Unit tests for errors.py - bug-report building and the global excepthook."""
import os
import sys
import threading

import pytest

import errors
from paths import APP_NAME, VERSION

# A traceback with deliberately impersonal paths, so the "no personal data"
# assertions test what build_bug_report ADDS, not what the traceback carries.
_TB_TEXT = ('Traceback (most recent call last):\n'
            '  File "app.py", line 10, in check_task\n'
            '    verdict = run_vision(task)\n'
            "ValueError: boom\n")
_PLATFORM = "Python 3.11.9 on Windows 11 (10.0.26200)"


def _report(exc=None):
    exc = ValueError("boom") if exc is None else exc
    return errors.build_bug_report("1.2.3", type(exc), exc, _TB_TEXT, _PLATFORM)


class TestBuildBugReport:
    def test_contains_version(self):
        assert "Version: 1.2.3" in _report()

    def test_contains_platform_info(self):
        assert _PLATFORM in _report()

    def test_contains_error_type_and_message(self):
        assert "ValueError: boom" in _report()

    def test_contains_full_traceback(self):
        assert _TB_TEXT.rstrip() in _report()

    def test_mentions_github_issue(self):
        assert "GitHub issue" in _report()

    def test_no_personal_paths_beyond_the_traceback(self):
        report = _report()
        home = os.path.expanduser("~")
        assert home not in report
        assert os.getcwd() not in report

    def test_handles_odd_exc_type(self):
        # A junk exc_type must not break report building.
        report = errors.build_bug_report("1.2.3", "weird", ValueError("x"),
                                         "", _PLATFORM)
        assert "weird" in report


class TestOllamaUnreachableMessage:
    def test_contains_url_and_fix_instructions(self):
        msg = errors.ollama_unreachable_message("http://127.0.0.1:11434")
        assert "http://127.0.0.1:11434" in msg
        assert "ollama serve" in msg
        assert "ollama pull qwen3-vl:8b-instruct" in msg

    def test_is_ollama_unreachable_roundtrip(self):
        msg = errors.ollama_unreachable_message("http://localhost:11434")
        assert errors.is_ollama_unreachable(msg)

    def test_is_ollama_unreachable_rejects_other_errors(self):
        assert not errors.is_ollama_unreachable("KeyError: 'message'")
        assert not errors.is_ollama_unreachable(None)
        assert not errors.is_ollama_unreachable("")


@pytest.fixture
def restore_hooks():
    """Put the process-wide hooks back however a test leaves them."""
    old_sys, old_thread = sys.excepthook, threading.excepthook
    yield
    sys.excepthook = old_sys
    threading.excepthook = old_thread


class TestInstallExcepthook:
    def test_installs_sys_and_threading_hooks(self, restore_hooks):
        errors.install_excepthook()
        assert sys.excepthook is not sys.__excepthook__
        assert threading.excepthook is not threading.__excepthook__

    def test_headless_hook_writes_concise_stderr_line(self, restore_hooks,
                                                      capsys):
        errors.install_excepthook()
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        err = capsys.readouterr().err
        assert f"{APP_NAME} {VERSION} crashed: RuntimeError: kaboom" in err

    def test_presenter_gets_full_report(self, restore_hooks):
        shown = []
        errors.install_excepthook(shown.append)
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        assert len(shown) == 1
        assert "RuntimeError: kaboom" in shown[0]
        assert f"Version: {VERSION}" in shown[0]

    def test_broken_presenter_falls_back_to_stderr(self, restore_hooks,
                                                   capsys):
        def bad_presenter(report):
            raise RuntimeError("dialog exploded")
        errors.install_excepthook(bad_presenter)
        try:
            raise ValueError("original")
        except ValueError:
            sys.excepthook(*sys.exc_info())   # must not raise
        assert "ValueError: original" in capsys.readouterr().err
