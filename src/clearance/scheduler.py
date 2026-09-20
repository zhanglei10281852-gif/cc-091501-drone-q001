"""后台调度:周期执行到期的定时工作(如通告到点生效扫描)。

所有工作都持久化在 scheduled_jobs 表:进程重启后由
ClearanceService.recover() 立即补跑到期任务,调度线程继续后续周期,
因此"重启后仍能继续定时生效工作"。
"""
from __future__ import annotations

import threading


class Scheduler(threading.Thread):
    def __init__(self, service, poll_interval=1.0):
        super().__init__(name="clearance-scheduler", daemon=True)
        self._service = service
        self._poll_interval = poll_interval
        self._stopped = threading.Event()

    def run(self):
        while not self._stopped.is_set():
            try:
                self._service.run_due_jobs()
            except Exception:  # 调度器不允许因单个任务失败而退出
                import traceback

                traceback.print_exc()
            self._stopped.wait(self._poll_interval)

    def stop(self):
        self._stopped.set()
