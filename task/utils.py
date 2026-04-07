import json
import logging
import os
import textwrap
import time
import traceback
from collections import deque

from gpu_tasker.settings import RUNNING_LOG_DIR
from notification.email_notification import (
    send_task_fail_email,
    send_task_finish_email,
    send_task_start_email,
)
from base.persistent_ssh import (
    PersistentSSHCommandError,
    PersistentSSHConnectionError,
)
from .models import GPUTaskRunningLog


task_logger = logging.getLogger('django.task')
TASK_LOG_TAIL_LINES = 40
TASK_LOG_TAIL_CHARS = 4000
TASK_MONITOR_DISCONNECT_NOTE = '监控 SSH 连接中断，任务先保持运行中，等待自动重连。'
TASK_MONITOR_RESTORED_NOTE = '监控 SSH 连接已恢复，继续同步远端日志。'
KNOWN_FAILURE_PATTERNS = (
    ('Traceback (most recent call last):', '日志尾部包含 Python traceback'),
    ('CUDA out of memory', '日志尾部包含 CUDA out of memory'),
    ('No such file or directory', '日志尾部包含文件或目录不存在'),
    ('Permission denied', '日志尾部包含权限不足'),
    ('KeyboardInterrupt', '日志尾部包含 KeyboardInterrupt'),
    ('Killed', '日志尾部显示进程被杀死'),
)
LOG_MARKER = b'__GPUTASKER_LOG__\n'
STATUS_MARKERS = (
    '__GPUTASKER_PID__',
    '__GPUTASKER_EXIT_CODE__',
    '__GPUTASKER_RUNNING__',
    '__GPUTASKER_LOG_SIZE__',
)


def _shorten_text(text, max_length=240):
    text = ' '.join((text or '').split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + '...'


def _format_return_code(return_code):
    if return_code < 0:
        return str(return_code)
    if os.name == 'nt':
        return '{} (0x{:08X})'.format(return_code, return_code & 0xFFFFFFFF)
    return str(return_code)


def _read_log_tail(log_file_path, max_lines=TASK_LOG_TAIL_LINES):
    if not log_file_path or not os.path.isfile(log_file_path):
        return []
    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as handle:
        return list(deque((line.rstrip('\r\n') for line in handle), maxlen=max_lines))


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


def _blank_failure_diagnostics():
    return {
        'failure_summary': '',
        'failure_hint': '',
        'remote_exit_code': '',
        'last_output': '',
    }


def _extract_exception_summary(error_text, default_summary):
    lines = [line.strip() for line in (error_text or '').splitlines() if line.strip()]
    if not lines:
        return default_summary
    for line in reversed(lines):
        if line == 'Traceback (most recent call last):':
            continue
        return _shorten_text(line, 240)
    return default_summary


def _build_failure_diagnostics(return_code, log_file_path, summary_override=None):
    log_lines = _read_log_tail(log_file_path)
    failure_hint = _detect_failure_hint(log_lines)

    last_output = ''
    for line in reversed(log_lines):
        if line.strip():
            last_output = _shorten_text(line, 240)
            break

    if summary_override:
        summary = summary_override
    elif failure_hint:
        summary = failure_hint
    elif return_code in (143, 137):
        summary = '远端任务被终止'
    else:
        summary = '远端任务命令以非零状态退出'

    return {
        'failure_summary': summary,
        'remote_exit_code': _format_return_code(return_code),
        'failure_hint': failure_hint,
        'last_output': last_output,
        'tail': _format_log_tail(log_lines),
    }


def _build_exception_diagnostics(error_text, default_summary):
    diagnostics = _blank_failure_diagnostics()
    summary = _extract_exception_summary(error_text, default_summary)
    if summary and summary != default_summary:
        diagnostics['failure_summary'] = _shorten_text('{}: {}'.format(default_summary, summary), 240)
    else:
        diagnostics['failure_summary'] = default_summary
    diagnostics['last_output'] = summary
    return diagnostics


def _append_failure_diagnostics(log_file_path, diagnostics):
    note_lines = [
        '[GPUTASKER] failure_diagnosis={}'.format(diagnostics['failure_summary']),
        '[GPUTASKER] remote_exit_code={}'.format(diagnostics['remote_exit_code']),
    ]
    if diagnostics['failure_hint']:
        note_lines.append('[GPUTASKER] failure_hint={}'.format(diagnostics['failure_hint']))
    if diagnostics['last_output']:
        note_lines.append('[GPUTASKER] last_output={}'.format(diagnostics['last_output']))
    _append_log_note(log_file_path, '\n'.join(note_lines))


def _persist_failure_diagnostics(running_log, diagnostics):
    running_log.update_failure_diagnostics(diagnostics)
    running_log.save(
        update_fields=[
            'failure_summary',
            'failure_hint',
            'remote_exit_code',
            'last_output',
            'update_at',
        ]
    )


def _state_file_path(log_file_path):
    return log_file_path + '.state.json'


def _default_log_state():
    return {
        'remote_log_offset': 0,
        'disconnect_noted': False,
        'stop_signal_sent': False,
    }


def _load_log_state(log_file_path):
    state_path = _state_file_path(log_file_path)
    if not os.path.isfile(state_path):
        return _default_log_state()
    try:
        with open(state_path, 'r', encoding='utf-8') as handle:
            state = json.load(handle)
    except Exception:
        return _default_log_state()
    default_state = _default_log_state()
    default_state.update(state)
    return default_state


def _save_log_state(log_file_path, state):
    state_path = _state_file_path(log_file_path)
    with open(state_path, 'w', encoding='utf-8') as handle:
        json.dump(state, handle)


def _ensure_local_log_state(log_file_path):
    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
    if not os.path.isfile(log_file_path):
        with open(log_file_path, 'wb'):
            pass
    if not os.path.isfile(_state_file_path(log_file_path)):
        _save_log_state(log_file_path, _default_log_state())


def _append_log_bytes(log_file_path, data):
    if not data:
        return
    _ensure_local_log_state(log_file_path)
    with open(log_file_path, 'ab') as handle:
        handle.write(data)


def _append_log_note(log_file_path, message):
    if not message:
        return
    _ensure_local_log_state(log_file_path)
    encoded = message.encode('utf-8', errors='ignore') + b'\n'
    with open(log_file_path, 'ab') as handle:
        if os.path.getsize(log_file_path) > 0:
            handle.write(b'\n')
        handle.write(encoded)


def _cleanup_log_state(log_file_path):
    state_path = _state_file_path(log_file_path)
    if os.path.isfile(state_path):
        os.remove(state_path)


def _parse_gpu_list(gpus):
    if not gpus:
        return []
    return [int(item) for item in gpus.split(',') if item != '']


def _build_task_launch_script(task_cmd, running_log_id):
    normalized_cmd = task_cmd.replace('\r\n', '\n')
    if normalized_cmd and normalized_cmd[-1] != '\n':
        normalized_cmd += '\n'
    cmd_marker = '__GPUTASKER_CMD_{}_EOF__'.format(running_log_id)
    runner_marker = '__GPUTASKER_RUNNER_{}_EOF__'.format(running_log_id)
    template = textwrap.dedent("""\
        set -e
        run_id="$1"
        workspace="$2"
        visible_devices="$3"
        run_dir="${HOME}/.gputasker/task_runs/${run_id}"
        mkdir -p "$run_dir"
        rm -f "$run_dir/exit_code" "$run_dir/pid"
        : > "$run_dir/stdout.log"
        cat > "$run_dir/cmd.sh" <<'__CMD_MARKER__'
        #!/usr/bin/env bash
        set -e
        workspace="$1"
        visible_devices="$2"
        cd "$workspace"
        export CUDA_VISIBLE_DEVICES="$visible_devices"
        __TASK_BODY____CMD_MARKER__
        chmod +x "$run_dir/cmd.sh"
        cat > "$run_dir/runner.sh" <<'__RUNNER_MARKER__'
        #!/usr/bin/env bash
        set +e
        run_dir="$1"
        workspace="$2"
        visible_devices="$3"
        pid_file="$run_dir/pid"
        exit_file="$run_dir/exit_code"
        stdout_file="$run_dir/stdout.log"
        if command -v setsid >/dev/null 2>&1; then
            setsid bash "$run_dir/cmd.sh" "$workspace" "$visible_devices" >>"$stdout_file" 2>&1 &
        else
            bash "$run_dir/cmd.sh" "$workspace" "$visible_devices" >>"$stdout_file" 2>&1 &
        fi
        child_pid=$!
        printf '%s' "$child_pid" > "$pid_file"
        wait "$child_pid"
        status=$?
        printf '%s' "$status" > "$exit_file"
        exit "$status"
        __RUNNER_MARKER__
        chmod +x "$run_dir/runner.sh"
        nohup bash "$run_dir/runner.sh" "$run_dir" "$workspace" "$visible_devices" >/dev/null 2>&1 &
        attempt=0
        while [ "$attempt" -lt 50 ]; do
            if [ -s "$run_dir/pid" ]; then
                cat "$run_dir/pid"
                exit 0
            fi
            if [ -f "$run_dir/exit_code" ]; then
                break
            fi
            attempt=$((attempt + 1))
            sleep 0.1
        done
        if [ -s "$run_dir/pid" ]; then
            cat "$run_dir/pid"
            exit 0
        fi
        echo "0"
    """)
    return (
        template
        .replace('__CMD_MARKER__', cmd_marker)
        .replace('__RUNNER_MARKER__', runner_marker)
        .replace('__TASK_BODY__', normalized_cmd)
    )


def _build_task_status_script():
    return textwrap.dedent("""\
        set -e
        run_id="$1"
        offset="$2"
        run_dir="${HOME}/.gputasker/task_runs/${run_id}"
        stdout_file="$run_dir/stdout.log"
        pid_file="$run_dir/pid"
        exit_file="$run_dir/exit_code"
        pid=''
        exit_code=''
        if [ -f "$pid_file" ]; then
            pid="$(cat "$pid_file" 2>/dev/null)"
        fi
        if [ -f "$exit_file" ]; then
            exit_code="$(cat "$exit_file" 2>/dev/null)"
        fi
        running='0'
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            running='1'
        fi
        log_size='0'
        if [ -f "$stdout_file" ]; then
            log_size="$(wc -c < "$stdout_file")"
        fi
        if [ "$log_size" -lt "$offset" ]; then
            offset=0
        fi
        echo '__GPUTASKER_PID__'
        printf '%s\n' "$pid"
        echo '__GPUTASKER_EXIT_CODE__'
        printf '%s\n' "$exit_code"
        echo '__GPUTASKER_RUNNING__'
        printf '%s\n' "$running"
        echo '__GPUTASKER_LOG_SIZE__'
        printf '%s\n' "$log_size"
        echo '__GPUTASKER_LOG__'
        if [ -f "$stdout_file" ] && [ "$log_size" -gt "$offset" ]; then
            tail -c +$((offset + 1)) "$stdout_file"
        fi
    """)


def _build_task_stop_script():
    return textwrap.dedent("""\
        set -e
        run_id="$1"
        run_dir="${HOME}/.gputasker/task_runs/${run_id}"
        pid_file="$run_dir/pid"
        if [ ! -f "$pid_file" ]; then
            exit 0
        fi
        pid="$(cat "$pid_file" 2>/dev/null)"
        if [ -z "$pid" ]; then
            exit 0
        fi
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
        fi
    """)


def _parse_remote_pid(output):
    text = output.decode('utf-8', errors='ignore').strip()
    if not text:
        raise RuntimeError('Remote task launch did not return a PID.')
    first_line = text.splitlines()[0].strip()
    if not first_line.isdigit() or int(first_line) <= 0:
        raise RuntimeError('Remote task launch returned invalid PID: {}'.format(text))
    return int(first_line)


def _parse_task_status_output(output):
    if LOG_MARKER not in output:
        raise RuntimeError('Remote task status output is missing log marker.')
    header, log_chunk = output.split(LOG_MARKER, 1)
    header_lines = header.decode('utf-8', errors='ignore').splitlines()
    values = {}
    for index, line in enumerate(header_lines):
        if line not in STATUS_MARKERS:
            continue
        next_line = header_lines[index + 1] if index + 1 < len(header_lines) else ''
        values[line] = next_line.strip()
    remote_pid = values.get('__GPUTASKER_PID__', '')
    exit_code = values.get('__GPUTASKER_EXIT_CODE__', '')
    running = values.get('__GPUTASKER_RUNNING__', '0') == '1'
    log_size = values.get('__GPUTASKER_LOG_SIZE__', '0')
    return {
        'pid': int(remote_pid) if remote_pid.lstrip('-').isdigit() else None,
        'exit_code': int(exit_code) if exit_code.lstrip('-').isdigit() else None,
        'is_running': running,
        'log_size': int(log_size) if log_size.isdigit() else 0,
        'log_chunk': log_chunk,
    }


def _safe_send_notification(send_func, running_log, description):
    try:
        send_func(running_log)
    except Exception:
        task_logger.exception(
            'Failed to send %s notification for task %d-%s',
            description,
            running_log.task_id,
            running_log.task.name
        )


def _finalize_running_log(running_log, return_code, summary_override=None, diagnostics_override=None):
    task = running_log.task
    server = running_log.server
    gpus = _parse_gpu_list(running_log.gpus)
    final_status = 2 if return_code == 0 else -1

    if return_code != 0:
        diagnostics = diagnostics_override or _build_failure_diagnostics(
            return_code,
            running_log.log_file_path,
            summary_override
        )
        if not diagnostics['remote_exit_code']:
            diagnostics['remote_exit_code'] = _format_return_code(return_code)
        _persist_failure_diagnostics(running_log, diagnostics)
        _append_failure_diagnostics(running_log.log_file_path, diagnostics)
        task_logger.error(
            'Task %d-%s failure diagnosis: %s | remote_exit_code=%s%s\nTask log tail:\n%s',
            task.id,
            task.name,
            diagnostics['failure_summary'],
            diagnostics['remote_exit_code'],
            ' | last_output={}'.format(diagnostics['last_output']) if diagnostics['last_output'] else '',
            diagnostics['tail']
        )

    running_log.status = final_status
    running_log.stop_requested = False
    running_log.save(update_fields=['status', 'stop_requested', 'update_at'])

    task.status = final_status
    task.save(update_fields=['status', 'update_at'])

    if server is not None:
        server.set_gpus_free(gpus)
        server.save()

    _cleanup_log_state(running_log.log_file_path)

    if return_code == 0:
        _safe_send_notification(send_task_finish_email, running_log, 'finish')
    else:
        _safe_send_notification(send_task_fail_email, running_log, 'fail')


def _launch_remote_task(session, running_log, task, gpus):
    script = _build_task_launch_script(task.cmd, running_log.id)
    result = session.execute_script(
        script,
        args=[running_log.id, task.workspace, ','.join(map(str, gpus))],
    )
    return _parse_remote_pid(result.stdout)


def _request_remote_stop(session, running_log):
    session.execute_script(_build_task_stop_script(), args=[running_log.id])


def _poll_remote_task(session, running_log, remote_log_offset):
    result = session.execute_script(
        _build_task_status_script(),
        args=[running_log.id, remote_log_offset],
    )
    return _parse_task_status_output(result.stdout)


def run_task(task, available_server, connection_manager):
    server = available_server['server']
    gpus = available_server['gpus']
    index = task.task_logs.count()
    log_file_path = os.path.join(
        RUNNING_LOG_DIR,
        '{:d}_{:s}_{:s}_{:d}_{:d}.log'.format(task.id, task.name, server.ip, index, int(time.time()))
    )
    running_log = GPUTaskRunningLog(
        index=index,
        task=task,
        server=server,
        pid=0,
        gpus=','.join(map(str, gpus)),
        log_file_path=log_file_path,
        status=1,
        stop_requested=False,
    )
    running_log.save()
    _ensure_local_log_state(log_file_path)

    try:
        session = connection_manager.get_session(
            task.user.config.server_username,
            server.ip,
            server.port,
            task.user.config.server_private_key_path,
        )
        remote_pid = _launch_remote_task(session, running_log, task, gpus)
        running_log.pid = remote_pid
        running_log.save(update_fields=['pid', 'update_at'])

        server.set_gpus_busy(gpus)
        server.save()
        task.status = 1
        task.save(update_fields=['status', 'update_at'])

        _append_log_note(log_file_path, '[GPUTASKER] remote_pid={}'.format(remote_pid))
        _safe_send_notification(send_task_start_email, running_log, 'start')
        task_logger.info(
            'Task %d-%s launched via persistent SSH, remote_pid=%s, server=%s, gpus=%s',
            task.id,
            task.name,
            remote_pid,
            server.ip,
            running_log.gpus
        )
    except Exception:
        error_text = traceback.format_exc()
        task_logger.error(error_text)
        _append_log_note(log_file_path, error_text)
        diagnostics = _build_exception_diagnostics(error_text, '任务启动失败')
        _persist_failure_diagnostics(running_log, diagnostics)
        _append_failure_diagnostics(log_file_path, diagnostics)
        running_log.status = -1
        running_log.stop_requested = False
        running_log.save(update_fields=['status', 'stop_requested', 'update_at'])
        task.status = -1
        task.save(update_fields=['status', 'update_at'])
        server.set_gpus_free(gpus)
        server.save()
        _cleanup_log_state(log_file_path)
        _safe_send_notification(send_task_fail_email, running_log, 'fail')


def monitor_running_tasks(connection_manager):
    running_logs = GPUTaskRunningLog.objects.select_related(
        'task',
        'server',
        'task__user',
        'task__user__config',
    ).filter(status=1).order_by('id')

    for running_log in running_logs:
        task = running_log.task
        server = running_log.server
        if server is None:
            _append_log_note(running_log.log_file_path, '[GPUTASKER] monitor_error=任务没有关联服务器，直接标记失败。')
            _finalize_running_log(running_log, 1, summary_override='任务没有关联服务器')
            continue

        gpus = _parse_gpu_list(running_log.gpus)
        server.set_gpus_busy(gpus)
        state = _load_log_state(running_log.log_file_path)

        try:
            session = connection_manager.get_session(
                task.user.config.server_username,
                server.ip,
                server.port,
                task.user.config.server_private_key_path,
            )

            if running_log.stop_requested and not state['stop_signal_sent']:
                _request_remote_stop(session, running_log)
                state['stop_signal_sent'] = True
                _save_log_state(running_log.log_file_path, state)
                _append_log_note(
                    running_log.log_file_path,
                    '[GPUTASKER] stop_signal_sent=remote_pid {}'.format(running_log.pid)
                )

            remote_status = _poll_remote_task(session, running_log, state['remote_log_offset'])
            if state['disconnect_noted']:
                _append_log_note(running_log.log_file_path, '[GPUTASKER] {}'.format(TASK_MONITOR_RESTORED_NOTE))
                state['disconnect_noted'] = False

            if remote_status['pid'] and remote_status['pid'] != running_log.pid:
                running_log.pid = remote_status['pid']
                running_log.save(update_fields=['pid', 'update_at'])

            if remote_status['log_chunk']:
                _append_log_bytes(running_log.log_file_path, remote_status['log_chunk'])
            state['remote_log_offset'] = remote_status['log_size']
            _save_log_state(running_log.log_file_path, state)

            if remote_status['exit_code'] is not None:
                _finalize_running_log(running_log, remote_status['exit_code'])
                continue

            if not remote_status['is_running']:
                summary = '远端任务进程已结束，但未找到 exit_code 文件'
                _append_log_note(running_log.log_file_path, '[GPUTASKER] {}'.format(summary))
                _finalize_running_log(running_log, 1, summary_override=summary)
        except (PersistentSSHConnectionError, PersistentSSHCommandError) as exc:
            if not state['disconnect_noted']:
                _append_log_note(
                    running_log.log_file_path,
                    '[GPUTASKER] {} {}'.format(TASK_MONITOR_DISCONNECT_NOTE, _shorten_text(str(exc), 320))
                )
                state['disconnect_noted'] = True
                _save_log_state(running_log.log_file_path, state)
            task_logger.warning(
                'Monitor connection issue for task %d-%s on %s: %s',
                task.id,
                task.name,
                server.ip,
                exc
            )
        except Exception:
            error_text = traceback.format_exc()
            _append_log_note(running_log.log_file_path, error_text)
            task_logger.error(error_text)
            diagnostics = _build_exception_diagnostics(error_text, '任务监控异常退出')
            _finalize_running_log(
                running_log,
                1,
                summary_override=diagnostics['failure_summary'],
                diagnostics_override=diagnostics
            )
