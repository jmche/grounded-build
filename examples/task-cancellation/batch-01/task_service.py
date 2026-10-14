from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Callable


@dataclass
class Task:
    operation: Callable[[Event], object]
    status: str = "queued"
    result: object = None
    error: str | None = None
    cancel_event: Event = field(default_factory=Event)
    thread: Thread | None = None


class TaskService:
    def __init__(self):
        self._lock = Lock()
        self._tasks: dict[str, Task] = {}
        self._next_id = 1

    def submit(self, operation):
        with self._lock:
            task_id = str(self._next_id)
            self._next_id += 1
            self._tasks[task_id] = Task(operation=operation)
            return task_id

    def start(self, task_id):
        with self._lock:
            task = self._tasks[task_id]
            if task.status != "queued":
                return False
            task.status = "running"
            task.thread = Thread(target=self._run, args=(task_id,), daemon=True)
            task.thread.start()
            return True

    def _run(self, task_id):
        with self._lock:
            task = self._tasks[task_id]
        try:
            result = task.operation(task.cancel_event)
        except Exception as exc:
            with self._lock:
                if task.status == "running":
                    task.status = "failed"
                    task.error = type(exc).__name__
        else:
            with self._lock:
                if task.status == "running":
                    task.status = "completed"
                    task.result = result

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks[task_id]
            if task.status not in {"queued", "running"}:
                return False
            task.status = "cancelled"
            task.cancel_event.set()
            return True

    def snapshot(self, task_id):
        with self._lock:
            task = self._tasks[task_id]
            return {"id": task_id, "status": task.status, "result": task.result, "error": task.error}

    def join(self, task_id, timeout=2):
        with self._lock:
            thread = self._tasks[task_id].thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("Task did not exit within the join timeout")
