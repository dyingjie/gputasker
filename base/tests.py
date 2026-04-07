import paramiko
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from base.persistent_ssh import PersistentSSHConnectionError, PersistentSSHSession


class PersistentSSHSessionTests(SimpleTestCase):
    @patch('base.persistent_ssh._load_private_key', return_value='fake-pkey')
    @patch('base.persistent_ssh.paramiko.SSHClient')
    def test_ensure_connected_allows_agent_and_default_key_fallbacks(
        self,
        mock_ssh_client,
        mock_load_private_key,
    ):
        transport = Mock()
        transport.is_active.return_value = True
        client = Mock()
        client.get_transport.return_value = transport
        mock_ssh_client.return_value = client

        session = PersistentSSHSession('gpuuser', '10.0.0.8', 22, 'private_key/tester_pk')

        connected_client = session._ensure_connected()

        self.assertIs(connected_client, client)
        connect_kwargs = client.connect.call_args.kwargs
        self.assertEqual(connect_kwargs['hostname'], '10.0.0.8')
        self.assertEqual(connect_kwargs['username'], 'gpuuser')
        self.assertEqual(connect_kwargs['pkey'], 'fake-pkey')
        self.assertTrue(connect_kwargs['allow_agent'])
        self.assertTrue(connect_kwargs['look_for_keys'])
        mock_load_private_key.assert_called_once_with('private_key/tester_pk')

    @patch('base.persistent_ssh._load_private_key', return_value='fake-pkey')
    @patch('base.persistent_ssh.paramiko.SSHClient')
    def test_ensure_connected_reports_auth_sources_on_auth_failure(
        self,
        mock_ssh_client,
        mock_load_private_key,
    ):
        client = Mock()
        client.connect.side_effect = paramiko.AuthenticationException('Authentication failed.')
        mock_ssh_client.return_value = client

        session = PersistentSSHSession('gpuuser', '10.0.0.8', 22, 'private_key/tester_pk')

        with self.assertRaises(PersistentSSHConnectionError) as context:
            session._ensure_connected()

        self.assertIn('configured private key, ssh-agent, or default ~/.ssh keys', str(context.exception))
        client.close.assert_called_once()
        mock_load_private_key.assert_called_once_with('private_key/tester_pk')
