from task_service import TaskService


class TaskAPI:
    def __init__(self, service: TaskService):
        self.service = service

    def create(self, operation):
        return self.service.submit(operation)

    def start(self, task_id):
        return self.service.start(task_id)

    def cancel(self, task_id):
        return {"accepted": self.service.cancel(task_id), "task": self.service.snapshot(task_id)}

    def get(self, task_id):
        return self.service.snapshot(task_id)
