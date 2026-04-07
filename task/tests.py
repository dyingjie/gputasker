import os
from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import TestCase

from base.models import UserConfig
from gpu_info.models import GPUServer, GPUInfo
from gpu_tasker.settings import RUNNING_LOG_DIR
from notification.email_notification import send_task_fail_email
from task.admin import GPUTaskAdmin, GPUTaskRunningLogAdmin
from task.models import GPUTask, GPUTaskRunningLog
from task.utils import (
    PersistentSSHConnectionError,
    _load_log_state,
    _state_file_path,
    monitor_running_tasks,
    run_task,
)


class DummyConnectionManager:
    def __init__(self, session=None):
        self.session = session or object()
        self.calls = []

    def get_session(self, *args):
        self.calls.append(args)
        return self.session


class TaskRuntimeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='secret', email='tester@example.com')
        UserConfig.objects.create(
            user=self.user,
            server_username='gpuuser',
            server_private_key='dummy-key\n',
            server_private_key_path='private_key/tester_pk',
        )
        self.server = GPUServer.objects.create(ip='10.0.0.8', hostname='node-a', port=22, valid=True, can_use=True)
        self.gpu = GPUInfo.objects.create(
            uuid='GPU-1',
            index=0,
            name='RTX 4090',
            utilization=0,
            memory_total=24576,
            memory_used=0,
            processes='',
            server=self.server,
            use_by_self=False,
            complete_free=True,
        )
        self.task = GPUTask.objects.create(
            name='demo-task',
            user=self.user,
            workspace='/workspace/demo',
            cmd='python train.py\n',
            gpu_requirement=1,
            status=0,
        )
        self.task_admin = GPUTaskAdmin(GPUTask, AdminSite())
        self.running_log_admin = GPUTaskRunningLogAdmin(GPUTaskRunningLog, AdminSite())

    def tearDown(self):
        for running_log in GPUTaskRunningLog.objects.all():
            running_log.delete_log_file()

    def _create_running_log(self, status=1, stop_requested=False):
        log_file_path = os.path.join(RUNNING_LOG_DIR, 'test_running_{}.log'.format(self.task.id))
        return GPUTaskRunningLog.objects.create(
            index=0,
            task=self.task,
            server=self.server,
            pid=4321,
            gpus='0',
            log_file_path=log_file_path,
            status=status,
            stop_requested=stop_requested,
        )

    def _write_log(self, log_file_path, content):
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        with open(log_file_path, 'w', encoding='utf-8', errors='ignore') as handle:
            handle.write(content)

    @patch('task.utils.send_task_start_email')
    @patch('task.utils._launch_remote_task', return_value=4321)
    def test_run_task_launches_remote_process_and_marks_running(self, mock_launch_remote_task, mock_send_start_email):
        connection_manager = DummyConnectionManager()

        run_task(self.task, {'server': self.server, 'gpus': [0]}, connection_manager)

        running_log = GPUTaskRunningLog.objects.get(task=self.task)
        self.task.refresh_from_db()
        self.gpu.refresh_from_db()

        self.assertEqual(running_log.status, 1)
        self.assertEqual(running_log.pid, 4321)
        self.assertFalse(running_log.stop_requested)
        self.assertEqual(self.task.status, 1)
        self.assertTrue(self.gpu.use_by_self)
        self.assertTrue(os.path.isfile(running_log.log_file_path))
        with open(running_log.log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            self.assertIn('[GPUTASKER] remote_pid=4321', handle.read())
        self.assertTrue(os.path.isfile(_state_file_path(running_log.log_file_path)))
        mock_launch_remote_task.assert_called_once()
        mock_send_start_email.assert_called_once_with(running_log)

    @patch('task.utils.send_task_finish_email')
    @patch('task.utils._poll_remote_task')
    def test_monitor_running_tasks_syncs_log_and_marks_success(self, mock_poll_remote_task, mock_send_finish_email):
        running_log = self._create_running_log(status=1, stop_requested=False)
        self.task.status = 1
        self.task.save(update_fields=['status', 'update_at'])
        self.server.set_gpus_busy([0])

        mock_poll_remote_task.return_value = {
            'pid': 4321,
            'exit_code': 0,
            'is_running': False,
            'log_size': 6,
            'log_chunk': b'hello\n',
        }

        monitor_running_tasks(DummyConnectionManager())

        running_log.refresh_from_db()
        self.task.refresh_from_db()
        self.gpu.refresh_from_db()

        self.assertEqual(running_log.status, 2)
        self.assertFalse(running_log.stop_requested)
        self.assertEqual(self.task.status, 2)
        self.assertFalse(self.gpu.use_by_self)
        with open(running_log.log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            self.assertEqual(handle.read(), 'hello\n')
        self.assertFalse(os.path.exists(_state_file_path(running_log.log_file_path)))
        mock_send_finish_email.assert_called_once_with(running_log)

    @patch('task.utils._poll_remote_task', side_effect=PersistentSSHConnectionError('connection lost'))
    def test_monitor_running_tasks_keeps_running_when_connection_breaks(self, mock_poll_remote_task):
        running_log = self._create_running_log(status=1, stop_requested=False)
        self.task.status = 1
        self.task.save(update_fields=['status', 'update_at'])
        self.server.set_gpus_busy([0])

        monitor_running_tasks(DummyConnectionManager())

        running_log.refresh_from_db()
        self.task.refresh_from_db()

        self.assertEqual(running_log.status, 1)
        self.assertEqual(self.task.status, 1)
        state = _load_log_state(running_log.log_file_path)
        self.assertTrue(state['disconnect_noted'])
        with open(running_log.log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            self.assertIn('等待自动重连', handle.read())
        mock_poll_remote_task.assert_called_once()

    @patch('task.utils.send_task_fail_email')
    @patch('task.utils._request_remote_stop')
    @patch('task.utils._poll_remote_task')
    def test_monitor_running_tasks_honors_stop_request(self, mock_poll_remote_task, mock_request_remote_stop, mock_send_fail_email):
        running_log = self._create_running_log(status=1, stop_requested=True)
        self.task.status = 1
        self.task.save(update_fields=['status', 'update_at'])
        self.server.set_gpus_busy([0])

        mock_poll_remote_task.return_value = {
            'pid': 4321,
            'exit_code': 143,
            'is_running': False,
            'log_size': 0,
            'log_chunk': b'',
        }

        monitor_running_tasks(DummyConnectionManager())

        running_log.refresh_from_db()
        self.task.refresh_from_db()
        self.gpu.refresh_from_db()

        self.assertEqual(running_log.status, -1)
        self.assertFalse(running_log.stop_requested)
        self.assertEqual(self.task.status, -1)
        self.assertFalse(self.gpu.use_by_self)
        self.assertEqual(running_log.failure_summary, '远端任务被终止')
        self.assertEqual(running_log.remote_exit_code, '143 (0x0000008F)')
        mock_request_remote_stop.assert_called_once()
        mock_send_fail_email.assert_called_once_with(running_log)

    def test_running_log_kill_sets_stop_requested(self):
        running_log = self._create_running_log(status=1, stop_requested=False)

        running_log.kill(reason='admin kill action', actor='tester')

        running_log.refresh_from_db()
        self.assertTrue(running_log.stop_requested)
        with open(running_log.log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            self.assertIn('local_termination_reason=admin kill action by tester', handle.read())

    @patch('task.utils.send_task_fail_email')
    @patch('task.utils._launch_remote_task', side_effect=RuntimeError('launch exploded'))
    def test_run_task_persists_failure_diagnostics_when_launch_fails(self, mock_launch_remote_task, mock_send_fail_email):
        connection_manager = DummyConnectionManager()

        run_task(self.task, {'server': self.server, 'gpus': [0]}, connection_manager)

        running_log = GPUTaskRunningLog.objects.get(task=self.task)
        self.task.refresh_from_db()
        self.gpu.refresh_from_db()

        self.assertEqual(running_log.status, -1)
        self.assertEqual(self.task.status, -1)
        self.assertFalse(self.gpu.use_by_self)
        self.assertEqual(running_log.failure_summary, '任务启动失败: RuntimeError: launch exploded')
        self.assertEqual(running_log.last_output, 'RuntimeError: launch exploded')
        self.assertEqual(running_log.remote_exit_code, '')
        with open(running_log.log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
            content = handle.read()
        self.assertIn('[GPUTASKER] failure_diagnosis=任务启动失败: RuntimeError: launch exploded', content)
        self.assertIn('[GPUTASKER] last_output=RuntimeError: launch exploded', content)
        mock_launch_remote_task.assert_called_once()
        mock_send_fail_email.assert_called_once_with(running_log)

    def test_failure_diagnostics_fall_back_to_log_markers_in_admin(self):
        running_log = self._create_running_log(status=-1, stop_requested=False)
        self.task.status = -1
        self.task.save(update_fields=['status', 'update_at'])
        self._write_log(
            running_log.log_file_path,
            '\n'.join([
                'Traceback (most recent call last):',
                'RuntimeError: boom',
                '[GPUTASKER] failure_diagnosis=日志回退失败摘要',
                '[GPUTASKER] remote_exit_code=2 (0x00000002)',
                '[GPUTASKER] failure_hint=日志回退失败提示',
                '[GPUTASKER] last_output=日志回退最后输出',
            ]) + '\n'
        )

        diagnostics = running_log.get_failure_diagnostics(force_refresh=True)

        self.assertEqual(diagnostics['failure_summary'], '日志回退失败摘要')
        self.assertEqual(diagnostics['remote_exit_code'], '2 (0x00000002)')
        self.assertEqual(self.task_admin.failure_summary_display(self.task), '日志回退失败摘要')
        self.assertEqual(self.running_log_admin.failure_hint_display(running_log), '日志回退失败提示')
        self.assertEqual(self.running_log_admin.last_output_display(running_log), '日志回退最后输出')

    @patch('notification.email_notification.send_email')
    @patch('notification.email_notification.EMAIL_NOTIFICATION', True)
    def test_send_task_fail_email_includes_failure_diagnostics(self, mock_send_email):
        running_log = self._create_running_log(status=-1, stop_requested=False)
        running_log.failure_summary = '任务运行失败: CUDA out of memory'
        running_log.remote_exit_code = '1 (0x00000001)'
        running_log.last_output = 'RuntimeError: CUDA out of memory'
        running_log.save(update_fields=['failure_summary', 'remote_exit_code', 'last_output', 'update_at'])

        send_task_fail_email(running_log)

        self.assertTrue(mock_send_email.called)
        address, title, content = mock_send_email.call_args[0]
        self.assertEqual(address, 'tester@example.com')
        self.assertEqual(title, '任务运行失败')
        self.assertIn('失败摘要：任务运行失败: CUDA out of memory', content)
        self.assertIn('远端退出码：1 (0x00000001)', content)
        self.assertIn('最后输出：RuntimeError: CUDA out of memory', content)
