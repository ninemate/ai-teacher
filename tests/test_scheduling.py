import threading
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from teacher_common.scheduling import IndexScheduler, ScheduleConfig, load_ingest_config

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_disabled_by_default():
    schedule = ScheduleConfig()
    assert not schedule.enabled
    assert schedule.next_run(T0) is None


def test_interval_next_run():
    schedule = ScheduleConfig(enabled=True, interval_seconds=3600)
    assert schedule.next_run(T0) == T0 + timedelta(hours=1)


def test_cron_next_run_in_timezone():
    schedule = ScheduleConfig(enabled=True, cron="0 3 * * *", timezone="Europe/Budapest")
    # 03:00 CET == 02:00 UTC in January
    assert schedule.next_run(T0) == datetime(2026, 1, 2, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": True},
        {"enabled": True, "interval_seconds": 600, "cron": "* * * * *"},
        {"enabled": True, "interval_seconds": 5},
        {"enabled": True, "cron": "not a cron"},
        {"enabled": True, "cron": "0 3 * * *", "timezone": "Mars/Base"},
    ],
)
def test_invalid_schedules_rejected(kwargs):
    with pytest.raises((ValidationError, ValueError)):
        ScheduleConfig(**kwargs)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def make_scheduler(trigger, **kwargs):
    clock = Clock()
    scheduler = IndexScheduler(ScheduleConfig(enabled=True, interval_seconds=3600, **kwargs), trigger, clock)
    scheduler.next_run_at = clock.now + timedelta(hours=1)
    return scheduler, clock


def test_tick_not_due_does_nothing():
    calls = []
    scheduler, clock = make_scheduler(lambda: calls.append(1) or True)
    clock.now = T0 + timedelta(minutes=59)
    assert scheduler.tick() is False
    assert calls == []


def test_tick_fires_and_schedules_next_slot():
    calls = []
    scheduler, clock = make_scheduler(lambda: calls.append(1) or True)
    clock.now = T0 + timedelta(hours=1)
    assert scheduler.tick() is True
    assert calls == [1]
    assert scheduler.next_run_at == T0 + timedelta(hours=2)
    assert scheduler.last_tick_started_run is True
    assert scheduler.tick() is False


def test_declined_run_is_not_retried_until_next_slot():
    calls = []
    scheduler, clock = make_scheduler(lambda: calls.append(1) or False)  # a run is in progress
    clock.now = T0 + timedelta(hours=1)
    scheduler.tick()
    assert scheduler.last_tick_started_run is False
    assert scheduler.next_run_at == T0 + timedelta(hours=2)
    scheduler.tick()
    assert calls == [1]


def test_missed_slots_are_not_queued():
    calls = []
    scheduler, clock = make_scheduler(lambda: calls.append(1) or True)
    clock.now = T0 + timedelta(hours=10)  # e.g. a very long run
    scheduler.tick()
    scheduler.tick()
    assert calls == [1]
    assert scheduler.next_run_at == T0 + timedelta(hours=11)


def test_failing_trigger_does_not_kill_scheduler():
    def boom():
        raise RuntimeError("x")

    scheduler, clock = make_scheduler(boom)
    clock.now = T0 + timedelta(hours=1)
    assert scheduler.tick() is True
    assert scheduler.last_tick_started_run is False


def test_start_does_nothing_when_disabled():
    scheduler = IndexScheduler(ScheduleConfig(), lambda: True)
    scheduler.start()
    assert scheduler.next_run_at is None
    assert scheduler._thread is None


def test_run_on_startup_fires_immediately():
    fired = threading.Event()
    scheduler = IndexScheduler(
        ScheduleConfig(enabled=True, interval_seconds=3600, run_on_startup=True),
        lambda: fired.set() or True,
        Clock(),
    )
    scheduler.start()
    try:
        assert fired.wait(timeout=5)
    finally:
        scheduler.stop()


def test_without_run_on_startup_first_run_is_one_interval_away():
    scheduler = IndexScheduler(ScheduleConfig(enabled=True, interval_seconds=3600), lambda: True, Clock())
    scheduler.start()
    scheduler.stop()
    assert scheduler.next_run_at == T0 + timedelta(hours=1)


def test_load_config_missing_file_means_defaults(tmp_path):
    config = load_ingest_config(tmp_path / "none.yml")
    assert not config.schedule.enabled
    assert config.watch.folders == []


def test_load_config_from_yaml(tmp_path):
    path = tmp_path / "i.yml"
    path.write_text(
        """
scan_extensions: [.pdf]
schedule:
  enabled: true
  cron: "30 2 * * *"
watch:
  exclude: ["#recycle"]
  folders:
    - path: Books/Physics
    - path: Scans
      recursive: false
      extensions: [pdf]
"""
    )
    config = load_ingest_config(path)
    assert config.schedule.cron == "30 2 * * *"
    assert [f.path for f in config.watch.folders] == ["Books/Physics", "Scans"]
    assert config.watch.folders[1].extensions == [".pdf"]


def test_legacy_interval_used_only_without_schedule_section(tmp_path):
    assert load_ingest_config(tmp_path / "none.yml", 900).schedule.interval_seconds == 900
    path = tmp_path / "i.yml"
    path.write_text("schedule:\n  enabled: false\n")
    assert not load_ingest_config(path, 900).schedule.enabled
