import unittest

from task_api import TaskAPI
from task_service import TaskService


class ExistingBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.service = TaskService()
        self.api = TaskAPI(self.service)

    def test_success(self):
        task_id = self.api.create(lambda event: 42)
        self.assertTrue(self.api.start(task_id))
        self.service.join(task_id)
        self.assertEqual(self.api.get(task_id), {
            "id": task_id, "status": "completed", "result": 42, "error": None,
        })
        self.assertFalse(self.api.start(task_id))
        self.assertFalse(self.api.cancel(task_id)["accepted"])

    def test_failure(self):
        def fail(event):
            raise ValueError("synthetic failure")

        task_id = self.api.create(fail)
        self.api.start(task_id)
        self.service.join(task_id)
        self.assertEqual(self.api.get(task_id)["status"], "failed")
        self.assertEqual(self.api.get(task_id)["error"], "ValueError")

    def test_queued_cancellation(self):
        executed = []
        task_id = self.api.create(lambda event: executed.append(True))
        self.assertTrue(self.api.cancel(task_id)["accepted"])
        self.assertFalse(self.api.start(task_id))
        self.assertFalse(self.api.cancel(task_id)["accepted"])
        self.assertEqual(executed, [])

    def test_unknown_task(self):
        for method in (self.api.get, self.api.start, self.api.cancel):
            with self.subTest(method=method.__name__), self.assertRaises(KeyError):
                method("missing")

    def test_snapshot_dictionary_is_detached(self):
        task_id = self.api.create(lambda event: 42)
        snapshot = self.api.get(task_id)
        snapshot["status"] = "completed"
        self.assertEqual(self.api.get(task_id)["status"], "queued")
