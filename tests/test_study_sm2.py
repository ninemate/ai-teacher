from datetime import datetime, timedelta

from teacher_common.study import MIN_EASE, initial_state, schedule

NOW = datetime(2026, 1, 1, 12, 0)


def test_new_card_is_due_immediately():
    assert initial_state(NOW).due_at == NOW


def test_good_reviews_follow_sm2_intervals():
    state = initial_state(NOW)
    state = schedule(state, "good", NOW)
    assert (state.interval_days, state.repetitions) == (1, 1)
    state = schedule(state, "good", NOW)
    assert state.interval_days == 6
    state = schedule(state, "good", NOW)
    assert state.interval_days == round(6 * state.ease) and state.repetitions == 3
    assert state.due_at == NOW + timedelta(days=state.interval_days)


def test_easy_raises_and_hard_lowers_ease():
    base = initial_state(NOW)
    assert schedule(base, "easy", NOW).ease > base.ease
    assert schedule(base, "good", NOW).ease == base.ease
    assert schedule(base, "hard", NOW).ease < base.ease


def test_again_resets_progress_and_counts_a_lapse():
    state = schedule(schedule(initial_state(NOW), "good", NOW), "good", NOW)
    failed = schedule(state, "again", NOW)
    assert (failed.repetitions, failed.interval_days, failed.lapses) == (0, 0, 1)
    assert failed.due_at == NOW + timedelta(minutes=10)
    assert failed.ease < state.ease


def test_ease_never_drops_below_minimum():
    state = initial_state(NOW)
    for _ in range(20):
        state = schedule(state, "again", NOW)
    assert state.ease == MIN_EASE
    for _ in range(20):
        state = schedule(state, "hard", NOW)
    assert state.ease >= MIN_EASE


def test_interval_always_grows_for_passed_cards():
    state = initial_state(NOW)
    last = 0
    for _ in range(6):
        state = schedule(state, "hard", NOW)
        assert state.interval_days > last
        last = state.interval_days
