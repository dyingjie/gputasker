import os
from datetime import datetime
import time
import threading
import logging
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gpu_tasker.settings")
django.setup()

from base.utils import get_admin_config
from gpu_tasker.settings import SERVER_LOG_DIR
from task.models import GPUTask
from task.utils import run_task
from gpu_info.utils import GPUInfoUpdater

task_logger = logging.getLogger('django.task')
POLL_INTERVAL_SECONDS = 30
SCHEDULER_LOCK_PATH = os.path.join(SERVER_LOG_DIR, 'main_scheduler.lock')


class SchedulerInstanceLock:
    def __init__(self, lock_path):
        self.lock_path = lock_path
        self.handle = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        self.handle = open(self.lock_path, 'a+', encoding='utf-8')
        self.handle.seek(0)
        try:
            self._lock_file()
        except OSError:
            owner = self._read_owner_info()
            self.handle.close()
            self.handle = None
            return owner

        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(
            'pid={:d}\nstarted_at={}\n'.format(
                os.getpid(),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
        )
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return None

    def release(self):
        if self.handle is None:
            return

        try:
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def _lock_file(self):
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _read_owner_info(self):
        try:
            with open(self.lock_path, 'r', encoding='utf-8') as lock_file:
                return lock_file.read().strip()
        except Exception:
            return ''


if __name__ == '__main__':
    scheduler_lock = SchedulerInstanceLock(SCHEDULER_LOCK_PATH)
    owner_info = scheduler_lock.acquire()
    if owner_info is not None:
        if owner_info:
            task_logger.warning('Another main.py instance is already running. Lock owner:\n%s', owner_info)
        else:
            task_logger.warning('Another main.py instance is already running. Exiting current process.')
        sys.exit(0)

    task_logger.info('Scheduler lock acquired, pid: {:d}'.format(os.getpid()))

    try:
        while True:
            start_time = time.time()
            try:
                server_username, server_private_key_path = get_admin_config()
                gpu_updater = GPUInfoUpdater(server_username, server_private_key_path)

                task_logger.info('Running processes: {:d}'.format(
                    threading.active_count() - 1
                ))

                gpu_updater.update_gpu_info()
                for task in GPUTask.objects.filter(status=0):
                    available_server = task.find_available_server()
                    if available_server is not None:
                        t = threading.Thread(target=run_task, args=(task, available_server))
                        t.start()
                        time.sleep(5)
            except Exception as e:
                task_logger.error(str(e))
            finally:
                end_time = time.time()
                # 确保至少间隔三十秒，减少服务器负担
                duration = end_time - start_time
                if duration < POLL_INTERVAL_SECONDS:
                    time.sleep(POLL_INTERVAL_SECONDS - duration)
    finally:
        scheduler_lock.release()
