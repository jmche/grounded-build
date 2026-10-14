from threading import Event
import unittest

from task_api import TaskAPI
from task_service import TaskService


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.service = TaskService()
        self.api = TaskAPI(self.service)

    def _cleanup_task(self, task_id, release):
        release.set()
        self.service.join(task_id, timeout=2)

    def _start_task(self, operation, release):
        task_id = self.api.create(operation)
        started = self.api.start(task_id)
        self.addCleanup(self._cleanup_task, task_id, release)
        self.assertTrue(started)
        return task_id

    def _blocked_task(self, *, raises=False):
        entered = Event()
        release = Event()
        waits = []

        def operation(cancel_event):
            entered.set()
            released = release.wait(2)
            waits.append(released)
            if released and raises:
                raise ValueError("intentional late exception")
            return "late result"

        task_id = self._start_task(operation, release)
        self.assertTrue(entered.wait(2), "Worker did not enter its operation")
        return task_id, release, waits

    def _assert_cancelled(self, task_id):
        self.assertEqual(self.api.get(task_id), {
            "id": task_id, "status": "cancelled", "result": None, "error": None,
        })

    def test_cooperative_cancellation_signals_worker(self):
        entered = Event()
        release = Event()
        waits = []

        def operation(cancel_event):
            entered.set()
            waits.append(cancel_event.wait(1))
            waits.append(release.wait(2))
            return "cooperative exit"

        task_id = self._start_task(operation, release)
        self.assertTrue(entered.wait(2))
        response = self.api.cancel(task_id)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["task"]["status"], "cancelled")
        release.set()
        self.service.join(task_id, timeout=2)
        self.assertEqual(waits, [True, True])
        self._assert_cancelled(task_id)

    def test_noncooperative_late_return_cannot_publish(self):
        task_id, release, waits = self._blocked_task()
        response = self.api.cancel(task_id)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["task"]["status"], "cancelled")
        release.set()
        self.service.join(task_id, timeout=2)
        self.assertEqual(waits, [True])
        self._assert_cancelled(task_id)

    def test_late_exception_cannot_publish(self):
        task_id, release, waits = self._blocked_task(raises=True)
        response = self.api.cancel(task_id)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["task"]["status"], "cancelled")
        release.set()
        self.service.join(task_id, timeout=2)
        self.assertEqual(waits, [True])
        self._assert_cancelled(task_id)

    def test_cancellation_wins_before_worker_completion(self):
        task_id, release, waits = self._blocked_task()
        response = self.api.cancel(task_id)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["task"], self.api.get(task_id))
        release.set()
        self.service.join(task_id, timeout=2)
        self.assertEqual(waits, [True])
        self.assertEqual(self.api.get(task_id), response["task"])
        self._assert_cancelled(task_id)

    def test_completion_wins_before_cancellation(self):
        task_id = self._start_task(lambda event: 42, Event())
        self.service.join(task_id, timeout=2)
        completed = self.api.get(task_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"], 42)
        self.assertEqual(self.api.cancel(task_id), {"accepted": False, "task": completed})
        self.assertEqual(self.api.get(task_id), completed)

    def test_failure_wins_before_cancellation(self):
        def fail(event):
            raise ValueError("ordinary failure")

        task_id = self._start_task(fail, Event())
        self.service.join(task_id, timeout=2)
        failed = self.api.get(task_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "ValueError")
        self.assertEqual(self.api.cancel(task_id), {"accepted": False, "task": failed})
        self.assertEqual(self.api.get(task_id), failed)

    def test_repeated_running_cancellation_before_and_after_exit(self):
        task_id, release, waits = self._blocked_task()
        first = self.api.cancel(task_id)
        self.assertTrue(first["accepted"])
        repeated = {"accepted": False, "task": first["task"]}
        self.assertEqual(self.api.cancel(task_id), repeated)
        release.set()
        self.service.join(task_id, timeout=2)
        self.assertEqual(waits, [True])
        self.assertEqual(self.api.cancel(task_id), repeated)
        self._assert_cancelled(task_id)

    def test_queued_cancellation_never_executes(self):
        executed = []
        task_id = self.api.create(lambda event: executed.append(True))
        self.assertTrue(self.api.cancel(task_id)["accepted"])
        self.assertFalse(self.api.start(task_id))
        self.assertFalse(self.api.cancel(task_id)["accepted"])
        self.assertEqual(executed, [])
        self._assert_cancelled(task_id)

    def test_unknown_ids_raise_key_error(self):
        for method in (self.api.get, self.api.start, self.api.cancel):
            with self.subTest(method=method.__name__), self.assertRaises(KeyError):
                method("missing")
