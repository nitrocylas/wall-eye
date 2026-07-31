"""Persistence filter for camera alerts.

A momentary mess - someone setting a mug down, walking through the frame, a
shadow - should not fire an alarm. We only notify once the watched condition has
held for several consecutive checks. The decision is a tiny pure function so it
can be unit-tested without the camera or the vision model.
"""
from typing import Tuple

DEFAULT_CONFIRM_CHECKS = 2   # alert only after the condition holds this many checks


def update_alert_streak(prev_streak: int, alert_now: bool,
                        required: int = DEFAULT_CONFIRM_CHECKS) -> Tuple[int, bool]:
    """Advance the consecutive-alert streak and decide whether to notify.

    Args:
        prev_streak: how many checks in a row have alerted so far.
        alert_now:   whether this check's verdict is an alert.
        required:    consecutive alerts needed before we notify (floored at 1).

    Returns:
        (new_streak, should_deliver). ``should_deliver`` is True only once the
        streak reaches ``required``; a clear check resets the streak to 0.
    """
    if not alert_now:
        return (0, False)
    new_streak = prev_streak + 1
    return (new_streak, new_streak >= max(1, required))
