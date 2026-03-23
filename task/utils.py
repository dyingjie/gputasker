import os
import subprocess
import time
import traceback
import logging
import textwrap

from gpu_tasker.settings import RUNNING_LOG_DIR
from .models import GPUTask, GPUTaskRunningLog

from notification.email_notification import \
    send_task_start_email, send_task_finish_email, send_task_fail_email


task_logger = logging.getLogger('django.task')


def _hidden_window_kwargs():
    if os.name != 'nt':
        return {}
    kwargs = {}
    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if creationflags:
        kwargs['creationflags'] = creationflags
    startupinfo_cls = getattr(subprocess, 'STARTUPINFO', None)
    startf_use_showwindow = getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
    sw_hide = getattr(subprocess, 'SW_HIDE', 0)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= startf_use_showwindow
        startupinfo.wShowWindow = sw_hide
        kwargs['startupinfo'] = startupinfo
    return kwargs


class RemoteProcess:
    def __init__(self, user, host, cmd, workspace="~", port=22, private_key_path=None, output_file=None, args=None):
        self.user = user
        self.host = host
        self.workspace = workspace
        self.remote_cmd = cmd
        self.args = args or []
        self.argv = ['ssh', '-o', 'StrictHostKeyChecking=no', '-p', str(port)]
        if private_key_path is not None:
            self.argv.extend(['-i', private_key_path])
        self.argv.extend([f'{user}@{host}', 'bash', '-s', '--', *self.args])
        self.stdin_data = self.build_remote_script().encode('utf-8')
        task_logger.info('ssh argv:\n%s', self._loggable_argv())
        task_logger.info('remote bash script:\n%s', self.build_remote_script())
        if output_file is not None:
            self.output_file = output_file
            with open(self.output_file, "wb") as out:
                self.proc = subprocess.Popen(
                    self.argv,
                    shell=False,
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=out,
                    **_hidden_window_kwargs()
                )
        else:
            self.proc = subprocess.Popen(
                self.argv,
                shell=False,
                stdin=subprocess.PIPE,
                **_hidden_window_kwargs()
            )
        self.proc.stdin.write(self.stdin_data)
        self.proc.stdin.close()

    def build_remote_script(self):
        return self.remote_cmd

    def _loggable_argv(self):
        argv = list(self.argv)
        if '-i' in argv:
            key_index = argv.index('-i') + 1
            if key_index < len(argv):
                argv[key_index] = '<private-key>'
        return subprocess.list2cmdline(argv)

    def pid(self):
        return self.proc.pid

    def kill(self):
        self.proc.kill()

    def get_return_code(self):
        self.proc.wait()
        return self.proc.returncode


class RemoteGPUProcess(RemoteProcess):
    def __init__(self, user, host, gpus, cmd, workspace="~", port=22, private_key_path=None, output_file=None):
        self.gpus = list(gpus)
        super(RemoteGPUProcess, self).__init__(
            user,
            host,
            cmd,
            workspace,
            port,
            private_key_path,
            output_file,
            [workspace, ','.join(map(str, self.gpus))]
        )

    def build_remote_script(self):
        cmd = self.remote_cmd.replace('\r\n', '\n')
        if cmd and cmd[-1] != '\n':
            cmd = cmd + '\n'
        return textwrap.dedent("""\
            set -e
            workspace="$1"
            visible_devices="$2"

            cd "$workspace"
            export CUDA_VISIBLE_DEVICES="$visible_devices"
        """) + cmd


def run_task(task, available_server):
    server = available_server['server']
    gpus = available_server['gpus']
    index = task.task_logs.all().count()
    log_file_path = os.path.join(
        RUNNING_LOG_DIR,
        '{:d}_{:s}_{:s}_{:d}_{:d}.log'.format(task.id, task.name, server.ip, index, int(time.time()))
    )
    # create running_log
    running_log = GPUTaskRunningLog(
        index=index,
        task=task,
        server=server,
        pid=-1,
        gpus=','.join(map(str, gpus)),
        log_file_path=log_file_path,
        status=1
    )
    running_log.save()
    try:
        # run process
        process = RemoteGPUProcess(
            task.user.config.server_username,
            server.ip,
            gpus,
            task.cmd,
            task.workspace,
            server.port,
            task.user.config.server_private_key_path,
            log_file_path
        )
        pid = process.pid()
        task_logger.info('Task {:d}-{:s} is running, pid: {:d}'.format(task.id, task.name, pid))

        # save process status
        running_log.pid = pid
        running_log.save()
        server.set_gpus_busy(gpus)
        server.save()
        task.status = 1
        task.save()

        # send email
        send_task_start_email(running_log)

        # wait for return
        return_code = process.get_return_code()
        task_logger.info('Task {:d}-{:s} stopped, return_code: {:d}'.format(task.id, task.name, return_code))

        # save process status
        running_log.status = 2 if return_code == 0 else -1
        running_log.save()
        task.status = 2 if return_code == 0 else -1
        task.save()

        # send email
        if return_code == 0:
            send_task_finish_email(running_log)
        else:
            send_task_fail_email(running_log)
    except Exception:
        es = traceback.format_exc()
        task_logger.error(es)
        running_log.status = -1
        running_log.save()
        task.status = -1
        task.save()
        with open(log_file_path, 'a') as f:
            f.write('\n')
            f.write(es)
    finally:
        server.set_gpus_free(gpus)
        server.save()
