import logging
import os
import shlex
import socket
import threading
from dataclasses import dataclass

import paramiko


task_logger = logging.getLogger('django.task')

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_COMMAND_TIMEOUT = 60
KEEPALIVE_INTERVAL_SECONDS = 15


class PersistentSSHError(RuntimeError):
    pass


class PersistentSSHConnectionError(PersistentSSHError):
    pass


class PersistentSSHCommandError(PersistentSSHError):
    def __init__(self, message, exit_status=None, stdout=b'', stderr=b''):
        super().__init__(message)
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


@dataclass
class SSHCommandResult:
    stdout: bytes
    stderr: bytes
    exit_status: int


def _load_private_key(private_key_path):
    absolute_path = os.path.abspath(private_key_path)
    key_loaders = (
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.RSAKey,
        paramiko.DSSKey,
    )
    errors = []
    for loader in key_loaders:
        try:
            return loader.from_private_key_file(absolute_path)
        except paramiko.PasswordRequiredException:
            raise PersistentSSHConnectionError(
                'Private key {} is encrypted and requires a passphrase.'.format(private_key_path)
            )
        except Exception as exc:
            errors.append('{}: {}'.format(loader.__name__, exc))
    raise PersistentSSHConnectionError(
        'Failed to load private key {} ({})'.format(private_key_path, '; '.join(errors))
    )


class PersistentSSHSession:
    def __init__(self, user, host, port=22, private_key_path=None):
        self.user = user
        self.host = host
        self.port = port
        self.private_key_path = private_key_path
        self._client = None
        self._lock = threading.RLock()
        self._pkey = None

    @property
    def identity(self):
        return '{}@{}:{}'.format(self.user, self.host, self.port)

    def close(self):
        with self._lock:
            if self._client is None:
                return
            try:
                self._client.close()
            finally:
                self._client = None

    def execute(self, command, timeout=DEFAULT_COMMAND_TIMEOUT):
        return self._execute(command, None, timeout)

    def execute_script(self, script, args=None, timeout=DEFAULT_COMMAND_TIMEOUT):
        args = args or []
        command = 'bash -s -- {}'.format(' '.join(shlex.quote(str(item)) for item in args))
        stdin_data = script.encode('utf-8')
        return self._execute(command, stdin_data, timeout)

    def _execute(self, command, stdin_data, timeout):
        with self._lock:
            last_exc = None
            for attempt in range(2):
                client = self._ensure_connected()
                try:
                    result = self._run_command(client, command, stdin_data, timeout)
                    if attempt > 0:
                        task_logger.info('Recovered SSH session for %s after reconnect.', self.identity)
                    return result
                except PersistentSSHCommandError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    self._mark_disconnected()
                    if attempt == 0:
                        task_logger.warning('SSH command failed on %s, reconnecting once: %s', self.identity, exc)
                        continue
                    raise PersistentSSHConnectionError(
                        'SSH command failed on {} after reconnect: {}'.format(self.identity, exc)
                    ) from exc
            raise PersistentSSHConnectionError(
                'SSH command failed on {}: {}'.format(self.identity, last_exc)
            )

    def _ensure_connected(self):
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
            self._mark_disconnected()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            'hostname': self.host,
            'port': self.port,
            'username': self.user,
            'timeout': DEFAULT_CONNECT_TIMEOUT,
            'auth_timeout': DEFAULT_CONNECT_TIMEOUT,
            'banner_timeout': DEFAULT_CONNECT_TIMEOUT,
            # Match OpenSSH behavior more closely:
            # try the configured key first, then fall back to ssh-agent and
            # standard ~/.ssh/id_* keys when a host accepts a different key.
            'look_for_keys': True,
            'allow_agent': True,
        }
        if self.private_key_path:
            if self._pkey is None:
                self._pkey = _load_private_key(self.private_key_path)
            kwargs['pkey'] = self._pkey
        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            client.close()
            auth_sources = 'ssh-agent or default ~/.ssh keys'
            if self.private_key_path:
                auth_sources = 'configured private key, ssh-agent, or default ~/.ssh keys'
            raise PersistentSSHConnectionError(
                'Failed to authenticate SSH session {} using {}: {}'.format(
                    self.identity,
                    auth_sources,
                    exc,
                )
            ) from exc
        except (paramiko.SSHException, socket.error, OSError) as exc:
            client.close()
            raise PersistentSSHConnectionError(
                'Failed to connect SSH session {}: {}'.format(self.identity, exc)
            ) from exc

        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(KEEPALIVE_INTERVAL_SECONDS)
        self._client = client
        task_logger.info('Established persistent SSH session for %s', self.identity)
        return self._client

    def _mark_disconnected(self):
        if self._client is None:
            return
        try:
            self._client.close()
        finally:
            self._client = None

    def _run_command(self, client, command, stdin_data, timeout):
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            channel = stdout.channel
            if timeout is not None:
                channel.settimeout(timeout)
            if stdin_data:
                stdin.write(stdin_data.decode('utf-8'))
            stdin.flush()
            stdin.channel.shutdown_write()
            stdout_data = stdout.read()
            stderr_data = stderr.read()
            exit_status = channel.recv_exit_status()
        except (socket.timeout, TimeoutError) as exc:
            raise PersistentSSHConnectionError(
                'SSH command timed out on {} after {} seconds'.format(self.identity, timeout)
            ) from exc
        except (paramiko.SSHException, socket.error, EOFError, OSError) as exc:
            raise PersistentSSHConnectionError(
                'SSH transport error on {}: {}'.format(self.identity, exc)
            ) from exc

        if exit_status != 0:
            raise PersistentSSHCommandError(
                'SSH command returned non-zero exit status {} on {}'.format(exit_status, self.identity),
                exit_status=exit_status,
                stdout=stdout_data,
                stderr=stderr_data,
            )
        return SSHCommandResult(stdout=stdout_data, stderr=stderr_data, exit_status=exit_status)


class SSHConnectionManager:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get_session(self, user, host, port=22, private_key_path=None):
        key = (user, host, port, os.path.abspath(private_key_path) if private_key_path else None)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = PersistentSSHSession(user, host, port, private_key_path)
                self._sessions[key] = session
            return session

    def close_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
