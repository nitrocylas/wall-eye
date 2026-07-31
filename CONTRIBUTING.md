# Contributing to Wall-Eye

Thanks for helping. PRs are welcome - Linux and macOS support is the biggest
one (tray, toast notifications, and audio playback are the Windows-specific
parts; the rest is portable Python).

## Ground rules

- **No cloud dependencies, ever.** Wall-Eye's whole point is that frames and
  data never leave the user's machine. Anything that phones home, requires an
  account, or adds a hosted service will not be merged. The single exception
  is the existing opt-in ntfy push, which is self-hostable.
- **PRs adding cloud upload, telemetry, or remote access will not be
  accepted.**
- **Run the tests** before opening a PR, and add tests for new non-trivial
  logic:

  ```
  python -m pytest tests -q
  ```

  Pure logic (DSP, parsing, schedulers) is tested directly; hardware and
  network boundaries are mocked - see `tests/` for the pattern.
- **Keep modules small.** One module, one responsibility. Extract testable
  logic out of GUI/hardware glue rather than growing the glue.
- **Keep changes minimal and reviewable.** Don't reformat unrelated code.
- No emojis in code, comments, or UI strings.

## Getting started

```
pip install -r requirements.txt
python gui.py
```

`config.yaml` is the configuration reference; `firmware/FIRMWARE.md` covers
the ESP32 camera.
