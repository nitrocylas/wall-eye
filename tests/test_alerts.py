"""Unit tests for the persistence filter (alerts.update_alert_streak)."""
import alerts


def test_clear_check_resets_and_never_delivers():
    assert alerts.update_alert_streak(5, alert_now=False, required=2) == (0, False)


def test_first_alert_does_not_deliver_with_default():
    # required=2: one messy check is not enough yet
    assert alerts.update_alert_streak(0, alert_now=True, required=2) == (1, False)


def test_second_consecutive_alert_delivers():
    assert alerts.update_alert_streak(1, alert_now=True, required=2) == (2, True)


def test_keeps_delivering_while_condition_persists():
    # cooldown (elsewhere) handles repeat-suppression; the filter stays "deliver"
    assert alerts.update_alert_streak(2, alert_now=True, required=2) == (3, True)


def test_required_one_delivers_immediately():
    assert alerts.update_alert_streak(0, alert_now=True, required=1) == (1, True)


def test_required_floored_at_one():
    # a misconfigured 0/negative still delivers rather than never firing
    assert alerts.update_alert_streak(0, alert_now=True, required=0) == (1, True)


def test_default_required_is_two():
    assert alerts.DEFAULT_CONFIRM_CHECKS == 2
    assert alerts.update_alert_streak(0, alert_now=True)[1] is False
    assert alerts.update_alert_streak(1, alert_now=True)[1] is True


def test_one_clear_check_breaks_the_streak():
    # messy, then a clear check, then messy again -> back to square one
    streak, _ = alerts.update_alert_streak(0, True, 2)     # 1
    streak, deliver = alerts.update_alert_streak(streak, False, 2)  # reset
    assert (streak, deliver) == (0, False)
    streak, deliver = alerts.update_alert_streak(streak, True, 2)   # 1 again, no fire
    assert (streak, deliver) == (1, False)
