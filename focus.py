"""Focus / Pomodoro session logic - the pure state machine behind the tab.

work (25) -> short break (5), and every 4th completed work session earns a long
break (15). The GUI owns the actual ticking; this module owns the decisions so
they're unit-tested.
"""
WORK = "work"
SHORT_BREAK = "break"
LONG_BREAK = "long break"

WORK_MINS = 25
SHORT_BREAK_MINS = 5
LONG_BREAK_MINS = 15
SESSIONS_BEFORE_LONG = 4


def fmt_mmss(seconds: int) -> str:
    """90 -> '01:30'. Clamps negatives to zero."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def phase_minutes(phase: str) -> int:
    return {WORK: WORK_MINS, SHORT_BREAK: SHORT_BREAK_MINS,
            LONG_BREAK: LONG_BREAK_MINS}.get(phase, WORK_MINS)


def next_phase(phase: str, completed_work_sessions: int) -> str:
    """What follows a finished phase. `completed_work_sessions` counts work
    sessions finished INCLUDING the one that just ended (when phase==WORK)."""
    if phase == WORK:
        if completed_work_sessions % SESSIONS_BEFORE_LONG == 0:
            return LONG_BREAK
        return SHORT_BREAK
    return WORK


def phase_line(phase: str) -> str:
    """A short spoken line for a phase transition."""
    if phase == WORK:
        return "Break's over. Back to it."
    if phase == LONG_BREAK:
        return "Four sessions down. Take a proper break - you earned it."
    return "Time's up. Stretch those legs for five."
