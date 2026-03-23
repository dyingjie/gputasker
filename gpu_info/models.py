import json
from datetime import timedelta

from django.db import models
from django.utils import timezone


class GPUServer(models.Model):
    ip = models.CharField('IP地址', max_length=50)
    alias = models.CharField('别名', max_length=50, blank=True, null=True)
    hostname = models.CharField('主机名', max_length=50, blank=True, null=True)
    port = models.PositiveIntegerField('端口', default=22)
    valid = models.BooleanField('是否可用', default=True)
    can_use = models.BooleanField('是否可调度', default=True)
    # TODO(Yuhao Wang): CPU使用率

    class Meta:
        ordering = ('ip',)
        verbose_name = 'GPU服务器'
        verbose_name_plural = 'GPU服务器'
        unique_together = (('ip', 'port'),)

    @property
    def display_name(self):
        alias = (self.alias or '').strip()
        if alias:
            return alias

        hostname = (self.hostname or '').strip()
        if hostname:
            return hostname

        return '{}:{:d}'.format(self.ip, self.port)

    def __str__(self):
        return self.display_name

    def get_available_gpus(self, gpu_num, exclusive, memory, utilization, idle_delay_minutes=0):
        available_gpu_list = []
        if self.valid and self.can_use:
            for gpu in self.gpus.all():
                if gpu.check_available(exclusive, memory, utilization, idle_delay_minutes):
                    available_gpu_list.append(gpu.index)
            if len(available_gpu_list) >= gpu_num:
                return available_gpu_list
            else:
                return None
        else:
            return None
    
    def set_gpus_busy(self, gpu_list):
        self.gpus.filter(index__in=gpu_list).update(use_by_self=True)

    def set_gpus_free(self, gpu_list):
        self.gpus.filter(index__in=gpu_list).update(use_by_self=False)


class GPUInfo(models.Model):
    uuid = models.CharField('UUID', max_length=40, primary_key=True)
    index = models.PositiveSmallIntegerField('序号')
    name = models.CharField('名称', max_length=40)
    utilization = models.PositiveSmallIntegerField('利用率')
    memory_total = models.PositiveIntegerField('总显存')
    memory_used = models.PositiveIntegerField('已用显存')
    processes = models.TextField('进程')
    server = models.ForeignKey(GPUServer, verbose_name='服务器', on_delete=models.CASCADE, related_name='gpus')
    use_by_self = models.BooleanField('是否被gputasker进程占用', default=False)
    complete_free = models.BooleanField('完全空闲', default=False)
    busy_since = models.DateTimeField('占用开始时间', blank=True, null=True)
    free_since = models.DateTimeField('空闲开始时间', blank=True, null=True)
    update_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        ordering = ('server', 'index',)
        verbose_name = 'GPU信息'
        verbose_name_plural = 'GPU信息'

    def __str__(self):
        return self.name + '[' + str(self.index) + '-' + self.server.display_name + ']'
    
    @property
    def memory_available(self):
        return self.memory_total - self.memory_used

    @property
    def utilization_available(self):
        return 100 - self.utilization

    def check_available(self, exclusive, memory, utilization, idle_delay_minutes=0):
        if exclusive:
            available = not self.use_by_self and self.complete_free
        else:
            available = not self.use_by_self and self.memory_available > memory and self.utilization_available > utilization

        if not available:
            return False

        if idle_delay_minutes <= 0:
            return True

        if not self.complete_free or self.free_since is None:
            return False

        return timezone.now() - self.free_since >= timedelta(minutes=idle_delay_minutes)

    def usernames(self):
        r"""
        convert processes string to usernames string array.
        :return: string array of usernames.
        """
        if self.processes != '':
            arr = self.processes.split('\n')
            username_arr = [json.loads(item)['username'] for item in arr]
            return ', '.join(username_arr)
        else:
            return '-'
