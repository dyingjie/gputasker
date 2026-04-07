import os
import subprocess
import json
import logging

from django.utils import timezone

from .models import GPUServer, GPUInfo
from base.persistent_ssh import (
    PersistentSSHCommandError,
    PersistentSSHConnectionError,
)

task_logger = logging.getLogger('django.task')

HOSTNAME_MARKER = '__GPUTASKER_HOSTNAME__'
GPU_MARKER = '__GPUTASKER_GPU__'
APPS_MARKER = '__GPUTASKER_APPS__'
USERS_MARKER = '__GPUTASKER_USERS__'
SSH_CLEANUP_PREVIEW_LENGTH = 220


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


def _shorten_text(text, max_length=SSH_CLEANUP_PREVIEW_LENGTH):
    text = ' '.join((text or '').split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + '...'


def _sanitize_command_line(command_line, private_key_path=None):
    sanitized = command_line or ''
    if private_key_path:
        candidates = {
            private_key_path,
            private_key_path.replace('\\', '/'),
            private_key_path.replace('/', '\\'),
        }
        for candidate in candidates:
            sanitized = sanitized.replace(candidate, '<private-key>')
    return _shorten_text(sanitized)


def _classify_ssh_command(command_line):
    command_line = (command_line or '').lower()
    if 'bash -s --' in command_line:
        return 'task-runner'
    if '__gputasker_gpu__' in command_line or 'nvidia-smi --query-gpu' in command_line:
        return 'gpu-poll'
    return 'generic-ssh'


def _kill_process_tree(pid):
    if not pid or os.name != 'nt':
        return False
    result = subprocess.run(
        ['taskkill', '/T', '/F', '/PID', str(pid)],
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        **_hidden_window_kwargs()
    )
    return result.returncode == 0


def _cleanup_timed_out_ssh_processes(host, user, private_key_path=None):
    if os.name != 'nt':
        return []

    filters = [f'{user}@{host}']
    if private_key_path:
        filters.append(private_key_path)

    ps_script = """
$filters = $args | ForEach-Object { $_.ToLower() }
$matches = @(
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
        Where-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return $false }
            $cmd = $cmd.ToLower()
            foreach ($filter in $filters) {
                if (-not $cmd.Contains($filter)) { return $false }
            }
            return $true
        } |
        Select-Object ProcessId, CommandLine
)
$matches | ConvertTo-Json -Compress
"""

    cleanup = subprocess.run(
        ['powershell', '-NoProfile', '-Command', ps_script, *filters],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
        **_hidden_window_kwargs()
    )

    if cleanup.returncode != 0:
        task_logger.warning(
            'Failed to enumerate timed-out ssh.exe processes for %s: return_code=%s stderr=%s',
            host,
            cleanup.returncode,
            _shorten_text(cleanup.stderr)
        )
        return []

    raw_output = cleanup.stdout.strip()
    if not raw_output:
        return []

    try:
        cleanup_targets = json.loads(raw_output)
    except json.JSONDecodeError:
        task_logger.warning('Failed to parse timed-out ssh.exe cleanup output for %s: %s', host, _shorten_text(raw_output))
        return []

    if isinstance(cleanup_targets, dict):
        cleanup_targets = [cleanup_targets]

    cleanup_results = []
    for target in cleanup_targets:
        pid = target.get('ProcessId')
        if not str(pid).isdigit():
            continue
        command_line = target.get('CommandLine', '')
        cleanup_results.append({
            'pid': int(pid),
            'command_kind': _classify_ssh_command(command_line),
            'command_line': _sanitize_command_line(command_line, private_key_path),
            'killed': _kill_process_tree(pid),
        })
    return cleanup_results


def _format_update_exception(exc):
    message = str(exc)
    stdout = getattr(exc, 'stdout', None)
    stderr = getattr(exc, 'stderr', None)
    output = getattr(exc, 'output', None)

    def _normalize(value):
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='ignore').strip()
        if isinstance(value, str):
            return value.strip()
        return ''

    details = []
    if message:
        details.append(message)
    if stdout:
        details.append('stdout=' + _normalize(stdout))
    if stderr:
        details.append('stderr=' + _normalize(stderr))
    elif output and output is not stdout:
        details.append('output=' + _normalize(output))

    return ' | '.join(filter(None, details))


def _split_server_status_sections(output):
    sections = {
        HOSTNAME_MARKER: [],
        GPU_MARKER: [],
        APPS_MARKER: [],
        USERS_MARKER: [],
    }
    current_marker = None

    for line in output.splitlines():
        if line in sections:
            current_marker = line
            continue
        if current_marker is not None:
            sections[current_marker].append(line)

    return sections


def _parse_gpu_info(gpu_info_raw, app_info_raw='', pid_raw=''):
    if gpu_info_raw.find('Error') != -1:
        raise RuntimeError(gpu_info_raw)

    gpu_info_list = []
    gpu_info_dict = {}
    for index, gpu_info_line in enumerate(gpu_info_raw.split('\n')):
        gpu_info_line = gpu_info_line.strip()
        if not gpu_info_line:
            continue
        try:
            gpu_info_items = gpu_info_line.split(',')
            gpu_info = {}
            gpu_info['index'] = index
            gpu_info['uuid'] = gpu_info_items[0].strip()
            gpu_info['name'] = gpu_info_items[1].strip()
            gpu_info['utilization.gpu'] = int(gpu_info_items[2].strip().split(' ')[0])
            gpu_info['memory.total'] = int(gpu_info_items[3].strip().split(' ')[0])
            gpu_info['memory.used'] = int(gpu_info_items[4].strip().split(' ')[0])
            gpu_info['processes'] = []
            gpu_info_list.append(gpu_info)
            gpu_info_dict[gpu_info['uuid']] = gpu_info
        except Exception:
            continue

    for app_info_line in app_info_raw.split('\n'):
        app_info_line = app_info_line.strip()
        if not app_info_line:
            continue
        try:
            app_info_items = app_info_line.split(',')
            app_info = {}
            uuid = app_info_items[0].strip()
            app_info['pid'] = int(app_info_items[1].strip())
            app_info['command'] = app_info_items[2].strip()
            app_info['gpu_memory_usage'] = int(app_info_items[3].strip().split(' ')[0])
            if app_info['gpu_memory_usage'] != 0 and uuid in gpu_info_dict:
                gpu_info_dict[uuid]['processes'].append(app_info)
        except Exception:
            continue

    pid_username_dict = {}
    for pid_line in pid_raw.split('\n'):
        pid_line = pid_line.strip()
        if not pid_line:
            continue
        try:
            username, pid = pid_line.split()
            pid_username_dict[int(pid)] = username.strip()
        except Exception:
            continue

    for gpu_info in gpu_info_list:
        for process in gpu_info['processes']:
            process['username'] = pid_username_dict.get(process['pid'], '')

    return gpu_info_list


def _build_server_status_command():
    return """
set -e
echo '__GPUTASKER_HOSTNAME__'
hostname
echo '__GPUTASKER_GPU__'
nvidia-smi --query-gpu=uuid,gpu_name,utilization.gpu,memory.total,memory.used --format=csv,noheader,nounits
echo '__GPUTASKER_APPS__'
app_output="$(nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader,nounits)"
printf '%s\\n' "$app_output"
echo '__GPUTASKER_USERS__'
pid_list="$(printf '%s\\n' "$app_output" | awk -F',' 'NF >= 2 {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); if ($2 != "") print $2}' | sort -u | tr '\\n' ' ')"
if [ -n "$pid_list" ]; then
    ps -o ruser= -o pid= -p $pid_list
fi
""".strip()


def ssh_execute(host, user, exec_cmd, port=22, private_key_path=None):
    exec_cmd = exec_cmd.replace('\r\n', '\n')
    if exec_cmd and exec_cmd[-1] != '\n':
        exec_cmd = exec_cmd + '\n'
    cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-p', str(port)]
    if private_key_path is not None:
        cmd.extend(['-i', private_key_path])
    cmd.extend([f'{user}@{host}', exec_cmd])
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            timeout=60,
            check=True,
            **_hidden_window_kwargs()
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        cleanup_results = _cleanup_timed_out_ssh_processes(host, user, private_key_path)
        if cleanup_results:
            cleanup_summary = '; '.join(
                'pid={pid} kind={command_kind} killed={killed} cmd={command_line}'.format(**result)
                for result in cleanup_results
            )
            task_logger.warning(
                'SSH to %s timed out after 60s. Cleanup targeted %d local ssh.exe process(es): %s',
                host,
                len(cleanup_results),
                cleanup_summary
            )
            risky_cleanup = [result for result in cleanup_results if result['command_kind'] != 'gpu-poll']
            if risky_cleanup:
                risky_summary = '; '.join(
                    'pid={pid} kind={command_kind} cmd={command_line}'.format(**result)
                    for result in risky_cleanup
                )
                task_logger.error(
                    'SSH timeout cleanup for %s matched non-poll ssh.exe process(es). This may interrupt running tasks: %s',
                    host,
                    risky_summary
                )
        else:
            task_logger.warning('SSH to %s timed out after 60s. No matching local ssh.exe process was found during cleanup.', host)
        raise


def _parse_server_status_output(status_output):
    status_output = status_output.decode('utf-8', errors='ignore')

    sections = _split_server_status_sections(status_output)
    hostname = '\n'.join(sections[HOSTNAME_MARKER]).strip()
    gpu_info_list = _parse_gpu_info(
        '\n'.join(sections[GPU_MARKER]).strip(),
        '\n'.join(sections[APPS_MARKER]).strip(),
        '\n'.join(sections[USERS_MARKER]).strip()
    )
    return hostname, gpu_info_list


def get_server_status(host, user, port=22, private_key_path=None):
    status_output = ssh_execute(
        host,
        user,
        _build_server_status_command(),
        port,
        private_key_path
    )
    return _parse_server_status_output(status_output)


def get_server_status_via_session(session):
    result = session.execute_script(_build_server_status_command())
    return _parse_server_status_output(result.stdout)


def get_hostname(host, user, port=22, private_key_path=None):
    hostname, _ = get_server_status(host, user, port, private_key_path)
    return hostname


def add_hostname(server, user, private_key_path=None):
    hostname = get_hostname(server.ip, user, server.port, private_key_path)
    server.hostname = hostname
    server.save()


def get_gpu_status(host, user, port=22, private_key_path=None):
    _, gpu_info_list = get_server_status(host, user, port, private_key_path)
    return gpu_info_list


class GPUInfoUpdater:
    def __init__(self, user=None, private_key_path=None, connection_manager=None):
        self.user = user
        self.private_key_path = private_key_path
        self.connection_manager = connection_manager
        self.utilization_history = {}

    def _get_server_status(self, server):
        if self.connection_manager is None:
            return get_server_status(server.ip, self.user, server.port, self.private_key_path)
        session = self.connection_manager.get_session(self.user, server.ip, server.port, self.private_key_path)
        return get_server_status_via_session(session)
    
    def update_utilization(self, uuid, utilization):
        if self.utilization_history.get(uuid) is None:
            self.utilization_history[uuid] = [utilization]
            return utilization
        else:
            self.utilization_history[uuid].append(utilization)
            if len(self.utilization_history[uuid]) > 10:
                self.utilization_history[uuid].pop(0)
            return max(self.utilization_history[uuid])

    def update_gpu_info(self):
        server_list = GPUServer.objects.all()
        for server in server_list:
            try:
                hostname, gpu_info_json = self._get_server_status(server)
                if (server.hostname is None or server.hostname == '') and hostname:
                    server.hostname = hostname
                    server.save()
                if not server.valid:
                    server.valid = True
                    server.save()
                live_gpu_uuids = set()
                for gpu in gpu_info_json:
                    live_gpu_uuids.add(gpu['uuid'])
                    is_complete_free = len(gpu['processes']) == 0
                    observed_at = timezone.now()
                    if GPUInfo.objects.filter(uuid=gpu['uuid']).count() == 0:
                        gpu_info = GPUInfo(
                            uuid=gpu['uuid'],
                            name=gpu['name'],
                            index=gpu['index'],
                            utilization=self.update_utilization(gpu['uuid'], gpu['utilization.gpu']),
                            memory_total=gpu['memory.total'],
                            memory_used=gpu['memory.used'],
                            processes='\n'.join(map(lambda x: json.dumps(x), gpu['processes'])),
                            complete_free=is_complete_free,
                            busy_since=None if is_complete_free else observed_at,
                            free_since=observed_at if is_complete_free else None,
                            server=server
                        )
                        gpu_info.save()
                    else:
                        gpu_info = GPUInfo.objects.get(uuid=gpu['uuid'])
                        gpu_info.index = gpu['index']
                        gpu_info.name = gpu['name']
                        gpu_info.server = server
                        gpu_info.utilization = self.update_utilization(gpu['uuid'], gpu['utilization.gpu'])
                        gpu_info.memory_total = gpu['memory.total']
                        gpu_info.memory_used = gpu['memory.used']
                        if is_complete_free:
                            gpu_info.busy_since = None
                            if gpu_info.free_since is None:
                                gpu_info.free_since = observed_at
                        else:
                            if gpu_info.busy_since is None:
                                gpu_info.busy_since = observed_at
                            gpu_info.free_since = None
                        gpu_info.complete_free = is_complete_free
                        gpu_info.processes = '\n'.join(map(lambda x: json.dumps(x), gpu['processes']))
                        gpu_info.save()
                stale_gpu_qs = server.gpus.exclude(uuid__in=live_gpu_uuids)
                stale_gpu_uuids = list(stale_gpu_qs.values_list('uuid', flat=True))
                if stale_gpu_uuids:
                    stale_gpu_qs.delete()
                    task_logger.warning(
                        'Removed %d stale GPU cache entry(s) for %s to match realtime inventory: %s',
                        len(stale_gpu_uuids),
                        server.ip,
                        ', '.join(stale_gpu_uuids)
                    )
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                RuntimeError,
                PersistentSSHCommandError,
                PersistentSSHConnectionError,
            ) as exc:
                task_logger.exception('Update %s failed: %s', server.ip, _format_update_exception(exc))
                server.valid = False
                server.save()
