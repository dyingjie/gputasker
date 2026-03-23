from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.utils import timezone

from gpu_info.admin import GPUInfoAdmin
from gpu_info.models import GPUInfo, GPUServer
from gpu_info.utils import GPUInfoUpdater


class GPUInfoUpdaterTests(TestCase):
    def setUp(self):
        self.server = GPUServer.objects.create(ip='10.0.0.1', alias='3090-01', port=22)
        self.updater = GPUInfoUpdater(user='gpuuser', private_key_path='private_key/test_pk')
        self.base_time = datetime(2026, 3, 23, 12, 0, 0)

    def _busy_gpu_status(self):
        return 'node-a', [{
            'uuid': 'GPU-1',
            'index': 0,
            'name': 'RTX 3090',
            'utilization.gpu': 80,
            'memory.total': 24576,
            'memory.used': 8192,
            'processes': [{
                'pid': 1234,
                'command': 'python train.py',
                'gpu_memory_usage': 8192,
                'username': 'gpuuser',
            }],
        }]

    def _free_gpu_status(self):
        return 'node-a', [{
            'uuid': 'GPU-1',
            'index': 0,
            'name': 'RTX 3090',
            'utilization.gpu': 0,
            'memory.total': 24576,
            'memory.used': 0,
            'processes': [],
        }]

    @patch('gpu_info.utils.get_server_status')
    @patch('gpu_info.utils.timezone.now')
    def test_new_busy_gpu_sets_busy_since(self, mock_now, mock_get_server_status):
        observed_at = self.base_time
        mock_now.return_value = observed_at
        mock_get_server_status.return_value = self._busy_gpu_status()

        self.updater.update_gpu_info()

        gpu = GPUInfo.objects.get(uuid='GPU-1')
        self.assertFalse(gpu.complete_free)
        self.assertEqual(gpu.busy_since, observed_at)
        self.assertIsNone(gpu.free_since)

    @patch('gpu_info.utils.get_server_status')
    @patch('gpu_info.utils.timezone.now')
    def test_busy_gpu_keeps_existing_busy_since(self, mock_now, mock_get_server_status):
        busy_since = self.base_time - timedelta(minutes=10)
        mock_now.return_value = self.base_time
        mock_get_server_status.return_value = self._busy_gpu_status()
        GPUInfo.objects.create(
            uuid='GPU-1',
            index=0,
            name='RTX 3090',
            utilization=50,
            memory_total=24576,
            memory_used=4096,
            processes='',
            server=self.server,
            complete_free=False,
            busy_since=busy_since,
            free_since=None,
        )

        self.updater.update_gpu_info()

        gpu = GPUInfo.objects.get(uuid='GPU-1')
        self.assertEqual(gpu.busy_since, busy_since)
        self.assertIsNone(gpu.free_since)

    @patch('gpu_info.utils.get_server_status')
    @patch('gpu_info.utils.timezone.now')
    def test_busy_gpu_becoming_free_clears_busy_since(self, mock_now, mock_get_server_status):
        free_since = self.base_time
        mock_now.return_value = free_since
        mock_get_server_status.return_value = self._free_gpu_status()
        GPUInfo.objects.create(
            uuid='GPU-1',
            index=0,
            name='RTX 3090',
            utilization=50,
            memory_total=24576,
            memory_used=4096,
            processes='{"pid": 1234}',
            server=self.server,
            complete_free=False,
            busy_since=self.base_time - timedelta(minutes=5),
            free_since=None,
        )

        self.updater.update_gpu_info()

        gpu = GPUInfo.objects.get(uuid='GPU-1')
        self.assertTrue(gpu.complete_free)
        self.assertIsNone(gpu.busy_since)
        self.assertEqual(gpu.free_since, free_since)


class GPUInfoAdminTests(TestCase):
    def setUp(self):
        self.admin = GPUInfoAdmin(GPUInfo, AdminSite())
        self.server = GPUServer.objects.create(ip='10.0.0.1', alias='3090-01', port=22)

    def test_busy_since_duration_shows_relative_time_for_busy_gpu(self):
        busy_since = timezone.now() - timedelta(minutes=7)
        gpu = GPUInfo.objects.create(
            uuid='GPU-1',
            index=0,
            name='RTX 3090',
            utilization=75,
            memory_total=24576,
            memory_used=4096,
            processes='{"pid": 1234}',
            server=self.server,
            complete_free=False,
            busy_since=busy_since,
            free_since=None,
        )

        rendered = self.admin.busy_since_duration(gpu)

        self.assertIn('gpuinfo-busy-duration', rendered)
        self.assertIn('data-relative-mode="duration"', rendered)
        self.assertEqual(self.admin.busy_since_duration.admin_order_field, 'busy_since')

    def test_busy_since_duration_shows_dash_for_free_gpu(self):
        gpu = GPUInfo.objects.create(
            uuid='GPU-1',
            index=0,
            name='RTX 3090',
            utilization=0,
            memory_total=24576,
            memory_used=0,
            processes='',
            server=self.server,
            complete_free=True,
            busy_since=None,
            free_since=timezone.now() - timedelta(minutes=3),
        )

        self.assertEqual(self.admin.busy_since_duration(gpu), '-')
