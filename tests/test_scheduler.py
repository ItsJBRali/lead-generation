from __future__ import annotations

import unittest

from lead_generator.planning.scheduler import PlatformAwareScheduler, ScheduledTask


class PlatformAwareSchedulerTest(unittest.TestCase):
    def test_round_robin_respects_platform_and_host_limits(self) -> None:
        scheduler = PlatformAwareScheduler[str](
            platform_limits={"idox": 3, "arcus": 2, "civica": 2},
            default_platform_limit=2,
        )
        scheduler.load_phase(
            [
                ScheduledTask("Idox A", "idox", "shared.idox.test"),
                ScheduledTask("Idox B", "idox", "shared.idox.test"),
                ScheduledTask("Idox C", "idox", "other.idox.test"),
                ScheduledTask("Arcus A", "arcus", "a.arcus.test"),
                ScheduledTask("Arcus B", "arcus", "b.arcus.test"),
                ScheduledTask("Civica A", "civica", "a.civica.test"),
            ]
        )

        first = scheduler.acquire()
        second = scheduler.acquire()
        third = scheduler.acquire()
        fourth = scheduler.acquire()
        fifth = scheduler.acquire()

        self.assertEqual(
            [task.item for task in (first, second, third, fourth, fifth) if task],
            ["Idox A", "Arcus A", "Civica A", "Idox C", "Arcus B"],
        )
        self.assertIsNotNone(first)
        scheduler.release(first)
        sixth = scheduler.acquire()
        self.assertIsNotNone(sixth)
        self.assertEqual(sixth.item, "Idox B")

    def test_rate_limit_reduces_capacity_then_successes_restore_it(self) -> None:
        scheduler = PlatformAwareScheduler[str](
            platform_limits={"idox": 3},
            default_platform_limit=2,
            rate_limit_cooldown_seconds=0,
            recovery_successes=2,
        )
        tasks = [
            ScheduledTask("A", "idox", "a.test"),
            ScheduledTask("B", "idox", "b.test"),
            ScheduledTask("C", "idox", "c.test"),
        ]
        scheduler.load_phase(tasks)

        first = scheduler.acquire()
        self.assertIsNotNone(first)
        adjustment = scheduler.release(first, signal="rate_limited")

        self.assertIsNotNone(adjustment)
        self.assertEqual(scheduler.current_limit("idox"), 2)
        second = scheduler.acquire()
        third = scheduler.acquire()
        self.assertIsNotNone(second)
        self.assertIsNotNone(third)
        scheduler.release(second)
        recovery = scheduler.release(third)
        self.assertIsNotNone(recovery)
        self.assertEqual(scheduler.current_limit("idox"), 3)

    def test_repeated_service_failures_reduce_platform_capacity(self) -> None:
        scheduler = PlatformAwareScheduler[str](
            platform_limits={"arcus": 2},
            default_platform_limit=2,
            service_cooldown_seconds=0,
        )
        scheduler.load_phase(
            [
                ScheduledTask("A", "arcus", "a.test"),
                ScheduledTask("B", "arcus", "b.test"),
            ]
        )

        first = scheduler.acquire()
        second = scheduler.acquire()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(scheduler.release(first, signal="service_unavailable"))
        adjustment = scheduler.release(second, signal="service_unavailable")

        self.assertIsNotNone(adjustment)
        self.assertEqual(scheduler.current_limit("arcus"), 1)

    def test_acquire_can_stop_while_tasks_are_waiting(self) -> None:
        scheduler = PlatformAwareScheduler[str](
            platform_limits={"idox": 1},
            default_platform_limit=1,
        )
        scheduler.load_phase([ScheduledTask("A", "idox", "a.test")])

        self.assertIsNone(scheduler.acquire(should_stop=lambda: True))


if __name__ == "__main__":
    unittest.main()
