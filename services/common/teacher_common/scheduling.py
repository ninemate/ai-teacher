"""Schedule definition and an in-process scheduler for periodic indexing."""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from croniter import croniter
from pydantic import BaseModel, model_validator

from teacher_common.watch import WatchConfig

logger = logging.getLogger("teacher.scheduler")

MIN_INTERVAL_SECONDS = 60


class ScheduleConfig(BaseModel):
    """Either a fixed interval or a cron expression; disabled by default."""

    enabled: bool = False
    interval_seconds: int | None = None
    cron: str | None = None
    timezone: str = "UTC"
    run_on_startup: bool = False

    @model_validator(mode="after")
    def _validate(self) -> "ScheduleConfig":
        if self.interval_seconds is not None and self.cron:
            raise ValueError("schedule: set either interval_seconds or cron, not both")
        if self.interval_seconds is not None and self.interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"schedule.interval_seconds must be at least {MIN_INTERVAL_SECONDS}")
        if self.cron and not croniter.is_valid(self.cron):
            raise ValueError(f"schedule.cron is not a valid cron expression: {self.cron!r}")
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"schedule.timezone is not a known timezone: {self.timezone!r}") from exc
        if self.enabled and self.interval_seconds is None and not self.cron:
            raise ValueError("schedule is enabled but neither interval_seconds nor cron is set")
        return self

    def next_run(self, after: datetime) -> datetime | None:
        """Next fire time strictly after ``after`` (timezone-aware), or None when disabled."""
        if not self.enabled:
            return None
        if self.cron:
            local = after.astimezone(ZoneInfo(self.timezone))
            return croniter(self.cron, local).get_next(datetime).astimezone(UTC)
        assert self.interval_seconds is not None
        return after + timedelta(seconds=self.interval_seconds)


class IngestFileConfig(BaseModel):
    schedule: ScheduleConfig = ScheduleConfig()
    watch: WatchConfig = WatchConfig()


def load_ingest_config(path: str | Path, fallback_interval_seconds: int = 0) -> IngestFileConfig:
    """Load the YAML config. A missing file means defaults: nothing watched, no schedule.

    ``fallback_interval_seconds`` (the legacy INGEST_SCHEDULE_SECONDS) is only used
    when the file has no ``schedule`` section.
    """
    data: dict = {}
    config_path = Path(path)
    if config_path.is_file():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = IngestFileConfig.model_validate({k: v for k, v in data.items() if k in ("schedule", "watch")})
    if "schedule" not in data and fallback_interval_seconds > 0:
        config.schedule = ScheduleConfig(enabled=True, interval_seconds=fallback_interval_seconds)
    return config


class IndexScheduler:
    """Calls ``trigger`` whenever the schedule fires, from one daemon thread.

    ``trigger`` returns False when it declined to run (e.g. a run is already in
    progress); the scheduler just waits for the next slot, so runs never pile up.
    """

    def __init__(
        self,
        schedule: ScheduleConfig,
        trigger: Callable[[], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.schedule = schedule
        self._trigger = trigger
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.next_run_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_tick_started_run: bool | None = None

    def start(self) -> None:
        if not self.schedule.enabled or self._thread is not None:
            return
        now = self._clock()
        with self._lock:
            self.next_run_at = now if self.schedule.run_on_startup else self.schedule.next_run(now)
        self._thread = threading.Thread(target=self._loop, name="index-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> bool:
        """Fire the trigger if the next run is due. Returns True when it fired."""
        now = self._clock()
        with self._lock:
            if self.next_run_at is None or now < self.next_run_at:
                return False
            # Compute the next slot from now so a long run skips missed slots instead of queueing them.
            self.next_run_at = self.schedule.next_run(now)
        try:
            started = self._trigger()
        except Exception:  # noqa: BLE001 - the scheduler thread must survive a failing trigger
            logger.exception("Scheduled indexing trigger failed")
            started = False
        self.last_tick_at = now
        self.last_tick_started_run = started
        if not started:
            logger.info("Scheduled indexing skipped: a run is already in progress")
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            with self._lock:
                target = self.next_run_at
            wait = 60.0 if target is None else max((target - self._clock()).total_seconds(), 0.0)
            self._stop.wait(min(wait, 60.0))
