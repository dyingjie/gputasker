import os
import logging
from collections import deque

from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator

from gpu_info.models import GPUServer, GPUInfo
from django.contrib.auth.models import User


task_logger = logging.getLogger('django.task')
FAILURE_DIAGNOSTIC_LOG_PREFIXES = {
    'failure_summary': '[GPUTASKER] failure_diagnosis=',
    'failure_hint': '[GPUTASKER] failure_hint=',
    'remote_exit_code': '[GPUTASKER] remote_exit_code=',
    'last_output': '[GPUTASKER] last_output=',
}
FAILURE_DIAGNOSTIC_KEYS = tuple(FAILURE_DIAGNOSTIC_LOG_PREFIXES.keys())


def _read_log_tail(log_file_path, max_lines=200):
    if not log_file_path or not os.path.isfile(log_file_path):
        return []
    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
        return list(deque((line.rstrip('\r\n') for line in handle), maxlen=max_lines))


def _parse_failure_diagnostics_from_log(log_file_path):
    diagnostics = {key: '' for key in FAILURE_DIAGNOSTIC_KEYS}
    for line in reversed(_read_log_tail(log_file_path)):
        for key, prefix in FAILURE_DIAGNOSTIC_LOG_PREFIXES.items():
            if diagnostics[key]:
                continue
            if line.startswith(prefix):
                diagnostics[key] = line[len(prefix):].strip()
    return diagnostics


class GPUTask(models.Model):
    STATUS_CHOICE = (
        (-2, '未就绪'),
        (-1, '运行失败'),
        (0, '准备就绪'),
        (1, '运行中'),
        (2, '已完成'),
    )
    IDLE_DELAY_CHOICE = (
        (0, '不延迟'),
        (1, '空闲超过1分钟'),
        (3, '空闲超过3分钟'),
        (5, '空闲超过5分钟'),
        (10, '空闲超过10分钟'),
        (30, '空闲超过30分钟'),
    )
    name = models.CharField('任务名称', max_length=100)
    user = models.ForeignKey(User, verbose_name='用户', on_delete=models.CASCADE, related_name='tasks')
    workspace = models.CharField('工作目录', max_length=200)
    cmd = models.TextField('命令')
    gpu_requirement = models.PositiveSmallIntegerField(
        'GPU数量需求',
        default=1,
        validators=[MaxValueValidator(8), MinValueValidator(0)]
    )
    idle_delay_minutes = models.PositiveSmallIntegerField('空闲等待时间', choices=IDLE_DELAY_CHOICE, default=0)
    exclusive_gpu = models.BooleanField('独占显卡', default=False)
    memory_requirement = models.PositiveSmallIntegerField('显存需求(MB)', default=0)
    utilization_requirement = models.PositiveSmallIntegerField('利用率需求(%)', default=0)
    assign_server = models.ForeignKey(GPUServer, verbose_name='指定服务器', on_delete=models.SET_NULL, blank=True, null=True)
    priority = models.SmallIntegerField('优先级', default=0)
    status = models.SmallIntegerField('状态', choices=STATUS_CHOICE, default=0)
    create_at = models.DateTimeField('创建时间', auto_now_add=True)
    update_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = 'GPU任务'
        verbose_name_plural = 'GPU任务'

    def __str__(self):
        return self.name

    def find_available_server(self):
        # TODO(Yuhao Wang): 优化算法，找最优server
        available_server = None
        if self.assign_server is None:
            for server in GPUServer.objects.all():
                available_gpus = server.get_available_gpus(
                    self.gpu_requirement,
                    self.exclusive_gpu,
                    self.memory_requirement,
                    self.utilization_requirement,
                    self.idle_delay_minutes
                )
                if available_gpus is not None:
                    available_server = {
                        'server': server,
                        'gpus': available_gpus[:self.gpu_requirement]
                    }
                    break
        else:
            available_gpus = self.assign_server.get_available_gpus(
                self.gpu_requirement,
                self.exclusive_gpu,
                self.memory_requirement,
                self.utilization_requirement,
                self.idle_delay_minutes
            )
            if available_gpus is not None:
                available_server = {
                    'server': self.assign_server,
                    'gpus': available_gpus[:self.gpu_requirement]
                }

        return available_server


class GPUTaskRunningLog(models.Model):
    STATUS_CHOICE = (
        (-1, '运行失败'),
        (1, '运行中'),
        (2, '已完成'),
    )
    index = models.PositiveSmallIntegerField('序号')
    task = models.ForeignKey(GPUTask, verbose_name='任务', on_delete=models.CASCADE, related_name='task_logs')
    server = models.ForeignKey(GPUServer, verbose_name='服务器', on_delete=models.SET_NULL, related_name='task_logs', null=True)
    pid = models.IntegerField('PID')
    gpus = models.CharField('GPU', max_length=20)
    log_file_path = models.FilePathField(path='running_log', match='.*\.log$', verbose_name="日志文件")
    stop_requested = models.BooleanField('已请求停止', default=False)
    failure_summary = models.CharField('失败摘要', max_length=255, blank=True, default='')
    failure_hint = models.CharField('失败提示', max_length=255, blank=True, default='')
    remote_exit_code = models.CharField('远端退出码', max_length=64, blank=True, default='')
    last_output = models.CharField('最后输出', max_length=255, blank=True, default='')
    status = models.SmallIntegerField('状态', choices=STATUS_CHOICE, default=1)
    start_at = models.DateTimeField('开始时间', auto_now_add=True)
    update_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        ordering = ('-id',)
        verbose_name = 'GPU任务运行记录'
        verbose_name_plural = 'GPU任务运行记录'

    def __str__(self):
        return self.task.name + '-' + str(self.index)

    def get_failure_diagnostics(self, force_refresh=False):
        if not force_refresh and hasattr(self, '_failure_diagnostics_cache'):
            return dict(self._failure_diagnostics_cache)

        diagnostics = {
            'failure_summary': (self.failure_summary or '').strip(),
            'failure_hint': (self.failure_hint or '').strip(),
            'remote_exit_code': (self.remote_exit_code or '').strip(),
            'last_output': (self.last_output or '').strip(),
        }
        if any(not diagnostics[key] for key in FAILURE_DIAGNOSTIC_KEYS):
            fallback = _parse_failure_diagnostics_from_log(self.log_file_path)
            for key in FAILURE_DIAGNOSTIC_KEYS:
                if not diagnostics[key]:
                    diagnostics[key] = fallback[key]

        self._failure_diagnostics_cache = dict(diagnostics)
        return diagnostics

    def update_failure_diagnostics(self, diagnostics):
        normalized = {key: (diagnostics.get(key, '') or '').strip() for key in FAILURE_DIAGNOSTIC_KEYS}
        self.failure_summary = normalized['failure_summary']
        self.failure_hint = normalized['failure_hint']
        self.remote_exit_code = normalized['remote_exit_code']
        self.last_output = normalized['last_output']
        self._failure_diagnostics_cache = dict(normalized)

    def kill(self, reason='manual kill', actor=None):
        reason_text = reason
        if actor:
            reason_text = '{} by {}'.format(reason, actor)
        task_logger.warning(
            'Requested remote stop for running task log %d (task %d-%s), remote_pid=%s, server=%s, gpus=%s, reason=%s',
            self.id,
            self.task_id,
            self.task.name,
            self.pid,
            self.server.ip if self.server_id else '-',
            self.gpus,
            reason_text
        )
        self.stop_requested = True
        self.save(update_fields=['stop_requested', 'update_at'])
        try:
            with open(self.log_file_path, 'a', encoding='utf-8', errors='ignore') as handle:
                handle.write('\n[GPUTASKER] local_termination_reason={}\n'.format(reason_text))
        except Exception:
            pass
    
    def delete_log_file(self):
        if os.path.isfile(self.log_file_path):
            os.remove(self.log_file_path)
        state_path = self.log_file_path + '.state.json'
        if os.path.isfile(state_path):
            os.remove(state_path)
