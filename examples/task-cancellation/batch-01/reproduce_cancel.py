from threading import Event

from task_api import TaskAPI
from task_service import TaskService


def reproduce():
    service = TaskService()
    api = TaskAPI(service)
    entered = Event()
    release = Event()

    def operation(cancel_event):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("Demonstration worker was not released")
        return "late result"

    task_id = api.create(operation)
    api.start(task_id)
    try:
        assert entered.wait(2), "Worker did not start"
        cancellation = api.cancel(task_id)
        assert cancellation["accepted"] is True
        assert cancellation["task"]["status"] == "cancelled"
    finally:
        release.set()
        service.join(task_id)
    final = api.get(task_id)
    print({"accepted": cancellation["accepted"], "after_worker_exit": final})
    assert final["status"] == "cancelled", "Late worker completion replaced accepted cancellation"
    assert final["result"] is None, "A cancelled task published a result"


if __name__ == "__main__":
    reproduce()
