from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Generic, Iterable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ScheduledTask(Generic[T]):
    item: T
    platform: str
    host: str


@dataclass(frozen=True, slots=True)
class SchedulerAdjustment:
    platform: str
    limit: int
    reason: str
    cooldown_seconds: float = 0.0


class PlatformAwareScheduler(Generic[T]):
    """Fair scheduler with platform, host, and adaptive throttle limits."""

    def __init__(
        self,
        *,
        platform_limits: dict[str, int],
        default_platform_limit: int,
        host_limit: int = 1,
        rate_limit_cooldown_seconds: float = 15.0,
        blocked_cooldown_seconds: float = 8.0,
        service_cooldown_seconds: float = 8.0,
        recovery_successes: int = 4,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._configured_limits = {
            platform: max(int(limit), 1)
            for platform, limit in platform_limits.items()
        }
        self._default_platform_limit = max(int(default_platform_limit), 1)
        self._host_limit = max(int(host_limit), 1)
        self._rate_limit_cooldown_seconds = max(rate_limit_cooldown_seconds, 0.0)
        self._blocked_cooldown_seconds = max(blocked_cooldown_seconds, 0.0)
        self._service_cooldown_seconds = max(service_cooldown_seconds, 0.0)
        self._recovery_successes = max(int(recovery_successes), 1)
        self._clock = clock
        self._condition = threading.Condition()
        self._queues: dict[str, deque[ScheduledTask[T]]] = {}
        self._platform_order: list[str] = []
        self._next_platform_index = 0
        self._active_platforms: dict[str, int] = {}
        self._active_hosts: dict[str, int] = {}
        self._base_limits: dict[str, int] = {}
        self._current_limits: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        self._success_streaks: dict[str, int] = {}
        self._service_failures: dict[str, int] = {}

    def load_phase(self, tasks: Iterable[ScheduledTask[T]]) -> None:
        with self._condition:
            if any(self._active_platforms.values()):
                raise RuntimeError("Cannot load a scheduler phase while tasks are active")
            self._queues = {}
            self._platform_order = []
            self._next_platform_index = 0
            for task in tasks:
                self._ensure_platform(task.platform)
                if task.platform not in self._queues:
                    self._queues[task.platform] = deque()
                    self._platform_order.append(task.platform)
                self._queues[task.platform].append(task)
            self._condition.notify_all()

    def acquire(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScheduledTask[T] | None:
        with self._condition:
            while self._has_pending_tasks():
                if should_stop and should_stop():
                    return None
                now = self._clock()
                task = self._next_eligible_task(now)
                if task is not None:
                    self._active_platforms[task.platform] = self._active_platforms.get(task.platform, 0) + 1
                    self._active_hosts[task.host] = self._active_hosts.get(task.host, 0) + 1
                    return task
                self._condition.wait(timeout=self._wait_timeout(now))
            return None

    def release(
        self,
        task: ScheduledTask[T],
        *,
        signal: str = "success",
        affected_platform: str | None = None,
    ) -> SchedulerAdjustment | None:
        with self._condition:
            self._active_platforms[task.platform] = max(
                self._active_platforms.get(task.platform, 0) - 1,
                0,
            )
            self._active_hosts[task.host] = max(self._active_hosts.get(task.host, 0) - 1, 0)
            platform = affected_platform or task.platform
            self._ensure_platform(platform)
            adjustment = self._apply_signal(platform, signal)
            self._condition.notify_all()
            return adjustment

    def current_limit(self, platform: str) -> int:
        with self._condition:
            self._ensure_platform(platform)
            return self._current_limits[platform]

    def _next_eligible_task(self, now: float) -> ScheduledTask[T] | None:
        platform_count = len(self._platform_order)
        if not platform_count:
            return None
        for offset in range(platform_count):
            index = (self._next_platform_index + offset) % platform_count
            platform = self._platform_order[index]
            queue = self._queues.get(platform)
            if not queue:
                continue
            if self._blocked_until.get(platform, 0.0) > now:
                continue
            if self._active_platforms.get(platform, 0) >= self._current_limits[platform]:
                continue
            task = self._pop_host_eligible_task(queue)
            if task is None:
                continue
            self._next_platform_index = (index + 1) % platform_count
            return task
        return None

    def _pop_host_eligible_task(
        self,
        queue: deque[ScheduledTask[T]],
    ) -> ScheduledTask[T] | None:
        for _ in range(len(queue)):
            task = queue.popleft()
            if self._active_hosts.get(task.host, 0) < self._host_limit:
                return task
            queue.append(task)
        return None

    def _apply_signal(self, platform: str, signal: str) -> SchedulerAdjustment | None:
        if signal == "success":
            self._service_failures[platform] = 0
            if self._current_limits[platform] >= self._base_limits[platform]:
                self._success_streaks[platform] = 0
                return None
            self._success_streaks[platform] = self._success_streaks.get(platform, 0) + 1
            if self._success_streaks[platform] < self._recovery_successes:
                return None
            self._success_streaks[platform] = 0
            self._current_limits[platform] += 1
            return SchedulerAdjustment(
                platform=platform,
                limit=self._current_limits[platform],
                reason="capacity restored after successful searches",
            )

        self._success_streaks[platform] = 0
        if signal == "service_unavailable":
            self._service_failures[platform] = self._service_failures.get(platform, 0) + 1
            if self._service_failures[platform] < 2:
                return None
            self._service_failures[platform] = 0
            return self._reduce_platform(
                platform,
                reason="repeated HTTP 503 responses",
                cooldown_seconds=self._service_cooldown_seconds,
            )
        if signal == "rate_limited":
            return self._reduce_platform(
                platform,
                reason="rate limiting",
                cooldown_seconds=self._rate_limit_cooldown_seconds,
            )
        if signal == "blocked":
            return self._reduce_platform(
                platform,
                reason="portal blocking response",
                cooldown_seconds=self._blocked_cooldown_seconds,
            )
        return None

    def _reduce_platform(
        self,
        platform: str,
        *,
        reason: str,
        cooldown_seconds: float,
    ) -> SchedulerAdjustment:
        self._current_limits[platform] = max(self._current_limits[platform] - 1, 1)
        self._blocked_until[platform] = max(
            self._blocked_until.get(platform, 0.0),
            self._clock() + cooldown_seconds,
        )
        return SchedulerAdjustment(
            platform=platform,
            limit=self._current_limits[platform],
            reason=reason,
            cooldown_seconds=cooldown_seconds,
        )

    def _ensure_platform(self, platform: str) -> None:
        if platform in self._base_limits:
            return
        limit = self._configured_limits.get(platform, self._default_platform_limit)
        self._base_limits[platform] = limit
        self._current_limits[platform] = limit
        self._active_platforms[platform] = 0
        self._success_streaks[platform] = 0
        self._service_failures[platform] = 0

    def _has_pending_tasks(self) -> bool:
        return any(self._queues.values())

    def _wait_timeout(self, now: float) -> float:
        future_cooldowns = [
            blocked_until - now
            for platform, blocked_until in self._blocked_until.items()
            if self._queues.get(platform) and blocked_until > now
        ]
        if future_cooldowns:
            return max(min(min(future_cooldowns), 0.5), 0.05)
        return 0.25
