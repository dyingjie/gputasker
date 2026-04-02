import os
import signal
import subprocess
import time
import traceback
import logging
import textwrap
from collections import deque

from gpu_tasker.settings import RUNNING_LOG_DIR
from .models import GPUTask, GPUTaskRunningLog

from notification.email_notification import \
    send_task_start_email, send_task_finish_email, send_task_fail_email


task_logger = logging.getLogger('django.task')
REMOTE_EXIT_MARKER = '__GPUTASKER_REMOTE_EXIT_STATUS__'
TASK_LOG_TAIL_LINES = 40
TASK_LOG_TAIL_CHARS = 4000
KNOWN_FAILURE_PATTERNS = (
    ('Traceback (most recent call last):', '日志尾部包含 Python traceback'),
    ('CUDA out of memory', '日志尾部包含 CUDA out of memory'),
    ('No such file or directory', '日志尾部包含文件或目录不存在'),
    ('Permission denied', '日志尾部包含权限不足'),
    ('KeyboardInterrupt', '日志尾部包含 KeyboardInterrupt'),
    ('Killed', '日志尾部显示进程被杀死'),
)


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


def _shorten_text(text, max_length=240):
    text = ' '.join((text or '').split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + '...'


def _format_return_code(return_code):
    if return_code < 0:
        signal_number = -return_code
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f'SIGNAL_{signal_number}'
        return f'{return_code} ({signal_name})'
    if os.name == 'nt':
        return f'{return_code} (0x{return_code & 0xFFFFFFFF:08X})'
    return str(return_code)


def _read_log_tail(log_file_path, max_lines=TASK_LOG_TAIL_LINES):
    if not log_file_path or not os.path.isfile(log_file_path):
        return []
    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
        return list(deque((line.rstrip('\r\n') for line in handle), maxlen=max_lines))


def _extract_remote_exit_code(log_lines):
    prefix = REMOTE_EXIT_MARKER + ' '
    for line in reversed(log_lines):
        if not line.startswith(prefix):
            continue
        raw_code = line[len(prefix):].strip()
        if raw_code.lstrip('-').isdigit():
            return int(raw_code)
        break
    return None


def _detect_failure_hint(log_lines):
    for line in reversed(log_lines):
        lower_line = line.lower()
        for pattern, hint in KNOWN_FAILURE_PATTERNS:
            if pattern.lower() in lower_line:
                return hint
    return ''


def _format_log_tail(log_lines):
    if not log_lines:
        return '(task log is empty)'
    tail_text = '\n'.join(log_lines).strip()
    if not tail_text:
        return '(task log is empty)'
    if len(tail_text) <= TASK_LOG_TAIL_CHARS:
        return tail_text
    return '...(tail truncated)...\n' + tail_text[-TASK_LOG_TAIL_CHARS:]


def _build_failure_diagnostics(return_code, log_file_path):
    log_lines = _read_log_tail(log_file_path)
    remote_exit_code = _extract_remote_exit_code(log_lines)
    failure_hint = _detect_failure_hint(log_lines)

    last_output = ''
    for line in reversed(log_lines):
        if not line.strip() or line.startswith(REMOTE_EXIT_MARKER):
            continue
        last_output = _shorten_text(line, 240)
        break

    if remote_exit_code is None:
        summary = '未发现远端退出标记，SSH 会话可能被中断，或本地 ssh.exe/远端 bash 被外部终止'
    elif remote_exit_code == 0 and return_code != 0:
        summary = '远端任务命令已返回 0，但本地 ssh 以非零退出，SSH 传输可能在收尾阶段异常中断'
    elif failure_hint:
        summary = failure_hint
    else:
        summary = '远端任务命令以非零状态退出，但日志尾部未发现明确 traceback'

    return {
        'summary': summary,
        'local_return_code': _format_return_code(return_code),
        'remote_exit_code': 'missing' if remote_exit_code is None else _format_return_code(remote_exit_code),
        'failure_hint': failure_hint,
        'last_output': last_output,
        'tail': _format_log_tail(log_lines),
    }


def _append_failure_diagnostics(log_file_path, diagnostics):
    if not log_file_path:
        return
    note_lines = [
        '',
        f"[GPUTASKER] failure_diagnosis={diagnostics['summary']}",
        f"[GPUTASKER] local_ssh_return_code={diagnostics['local_return_code']}",
        f"[GPUTASKER] remote_bash_exit={diagnostics['remote_exit_code']}",
    ]
    if diagnostics['failure_hint']:
        note_lines.append(f"[GPUTASKER] failure_hint={diagnostics['failure_hint']}")
    if diagnostics['last_output']:
        note_lines.append(f"[GPUTASKER] last_output={diagnostics['last_output']}")
    with open(log_file_path, 'a', encoding='utf-8', errors='ignore') as handle:
        handle.write('\n'.join(note_lines) + '\n')


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
            gputasker_on_exit() {
                status=$?
                printf '%s %s\n' '__GPUTASKER_REMOTE_EXIT_STATUS__' "$status"
            }
            trap gputasker_on_exit EXIT

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
        task_logger.info(
            'Task {:d}-{:s} stopped, return_code: {:s}'.format(
                task.id,
                task.name,
                _format_return_code(return_code)
            )
        )
        if return_code != 0:
            diagnostics = _build_failure_diagnostics(return_code, log_file_path)
            _append_failure_diagnostics(log_file_path, diagnostics)
            detail_parts = [
                'local_ssh_return_code=' + diagnostics['local_return_code'],
                'remote_bash_exit=' + diagnostics['remote_exit_code'],
            ]
            if diagnostics['failure_hint']:
                detail_parts.append('failure_hint=' + diagnostics['failure_hint'])
            if diagnostics['last_output']:
                detail_parts.append('last_output=' + diagnostics['last_output'])
            task_logger.error(
                'Task %d-%s failure diagnosis: %s | %s\nTask log tail:\n%s',
                task.id,
                task.name,
                diagnostics['summary'],
                ' | '.join(detail_parts),
                diagnostics['tail']
            )

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
