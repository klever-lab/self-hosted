"""Exercise the shipped NickelMenu commands without a reader, network or secrets."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def menu_entries():
    entries = {}
    for line in (ROOT / 'integrations/kobo/nickelmenu').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        kind, location, label, action, argument = [part.strip() for part in line.split(':', 4)]
        if kind != 'menu_item' or location != 'main' or label in entries:
            raise ValueError('Unexpected or duplicate NickelMenu entry')
        entries[label] = (action, argument)
    return entries


class MenuContractTests(unittest.TestCase):
    def test_output_timeouts_are_accepted_by_nickelmenu(self):
        # NickelMenu v0.6.0 res/doc:79 and src/action_cc.cc:879 require 0 < t < 10000.
        outputs = [entry for entry in menu_entries().values() if entry[0] == 'cmd_output']
        self.assertTrue(outputs, 'No output commands were checked')
        for _, argument in outputs:
            with self.subTest(argument=argument):
                value, separator, command = argument.partition(':')
                self.assertTrue(separator and command.strip())
                self.assertGreater(int(value), 0)
                self.assertLess(int(value), 10000)


class KoboCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ('bwrap', 'awk'):
            if not shutil.which(command):
                raise RuntimeError('Kobo command tests require bubblewrap and awk; install them before running the suite')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kobo-commands-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.onboard = self.root / 'onboard'
        self.setup = self.onboard / '.adds/self-hosted-kobo'
        self.local = self.root / 'local'
        self.wifi = self.root / 'sys/class/net/wlan0'
        self.tools = self.root / 'tools'
        for path in (self.setup, self.local / 'tailscale/bin', self.wifi, self.tools):
            path.mkdir(parents=True)
        # Debian's awk link uses /etc/alternatives, outside the isolated filesystem.
        (self.tools / 'awk').symlink_to(Path(shutil.which('awk')).resolve())
        shutil.copyfile(ROOT / 'integrations/kobo/status.sh', self.setup / 'status.sh')
        shutil.copyfile(ROOT / 'integrations/kobo/maintenance.sh', self.setup / 'maintenance.sh')
        (self.setup / 'books-host').write_text('100.64.0.1\nbooks.fixture.ts.net\n')
        (self.setup / 'status.log').write_text('Previous setup completed.\n')
        (self.wifi / 'carrier').write_text('1\n')
        (self.onboard / 'daemon-mode').write_text('running')
        (self.onboard / 'https-mode').write_text('reachable')
        self.executable(self.local / 'tailscale/bin/tailscale', '''
printf '%s\\n' "$@" > /mnt/onboard/daemon-arguments
printf '%s\\n' "$*" >> /mnt/onboard/daemon-calls
case "$(cat /mnt/onboard/daemon-mode)" in
  unavailable) exit 1 ;;
  hung) exec sleep 30 ;;
esac
if [ "$1" = set ]; then
  exit 0
fi
printf 'Tailscale daemon running.\\n'
''')
        self.executable(self.root / 'wget', '''
printf '%s\\n' "$@" > /mnt/onboard/https-arguments
case "$(cat /mnt/onboard/https-mode)" in
  unreachable) exit 1 ;;
  hung) exec sleep 30 ;;
esac
''')
        self.executable(self.root / 'df', '''
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
printf 'system 1000000 335424 664576 34%% /\\n'
printf 'books 13394984 978000 12417024 8%% /mnt/onboard\\n'
''')

    def executable(self, path, body):
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def run_menu(self, label):
        action, argument = menu_entries()[label]
        self.assertEqual(action, 'cmd_output')
        timeout, _, command = argument.partition(':')
        args = [
            'bwrap', '--unshare-all', '--die-with-parent', '--uid', '0', '--gid', '0',
            '--clearenv', '--setenv', 'PATH', '/test-tools:/usr/bin:/bin',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/sbin', '/sbin', '--symlink', 'usr/lib', '/lib',
            '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--tmpfs', '/usr/local',
            '--ro-bind', str(self.local), '/usr/local',
            '--ro-bind', str(self.tools), '/test-tools',
            '--ro-bind', str(self.root / 'sys'), '/sys',
            '--ro-bind', str(self.root / 'wget'), '/usr/bin/wget',
            '--ro-bind', str(self.root / 'df'), '/usr/bin/df',
            '--bind', str(self.onboard), '/mnt/onboard', '--chdir', '/',
            '/bin/sh', '-c', command,
        ]
        # Enforce the menu's real deadline, including commands it launches.
        return subprocess.run(args, capture_output=True, text=True, timeout=int(timeout) / 1000)

    def check_output(self, result, message):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stdout)

    def test_wifi_off_does_not_report_a_connection_or_probe_the_server(self):
        (self.wifi / 'carrier').write_text('0\n')
        result = self.run_menu('Tailscale status')
        self.check_output(result, 'Wi-Fi disconnected.')
        self.assertNotIn('Private library reachable', result.stdout)
        self.assertFalse((self.onboard / 'daemon-arguments').exists())
        self.assertFalse((self.onboard / 'https-arguments').exists())

    def test_daemon_failure_does_not_report_a_connection(self):
        (self.onboard / 'daemon-mode').write_text('unavailable')
        result = self.run_menu('Tailscale status')
        self.check_output(result, 'Tailscale is not ready.')
        self.assertFalse((self.onboard / 'https-arguments').exists())

    def test_hung_daemon_finishes_before_the_menu_deadline(self):
        (self.onboard / 'daemon-mode').write_text('hung')
        self.check_output(self.run_menu('Tailscale status'), 'Tailscale is not ready.')

    def test_unreachable_library_is_not_reported_as_connected(self):
        (self.onboard / 'https-mode').write_text('unreachable')
        self.check_output(self.run_menu('Tailscale status'), 'Private library unreachable.')

    def test_hung_https_probe_finishes_before_the_menu_deadline(self):
        (self.onboard / 'https-mode').write_text('hung')
        self.check_output(self.run_menu('Tailscale status'), 'Private library unreachable.')

    def test_reachable_library_uses_https_with_certificate_checks(self):
        self.check_output(self.run_menu('Tailscale status'), 'Private library reachable over Tailscale.')
        arguments = (self.onboard / 'https-arguments').read_text().splitlines()
        self.assertEqual(arguments[-1], 'https://books.fixture.ts.net/login')
        self.assertNotIn('--no-check-certificate', arguments)
        self.assertEqual((self.onboard / 'daemon-arguments').read_text().splitlines(),
                         ['status', '--peers=false'])

    def test_setup_result_shows_the_saved_result(self):
        result = self.run_menu('Setup result')
        self.check_output(result, 'Previous setup completed.')
        self.assertFalse((self.onboard / 'https-arguments').exists())

    def test_missing_setup_result_returns_a_readable_message(self):
        (self.setup / 'status.log').unlink()
        self.check_output(self.run_menu('Setup result'), 'Saved setup result unavailable.')

    def test_storage_status_displays_and_saves_both_partitions(self):
        result = self.run_menu('Storage status')
        self.check_output(result, 'System free: 649.0 MiB')
        self.assertIn('Books free: 12126.0 MiB', result.stdout)
        self.assertEqual((self.setup / 'storage-check.log').read_text(), result.stdout)
        self.assertFalse((self.setup / 'storage-check.txt').exists(), 'Diagnostics must not appear as TXT books')

    def test_enable_laptop_access_only_changes_ssh_and_incoming_connections(self):
        self.check_output(self.run_menu('Enable laptop access'), 'Laptop access enabled over Tailscale.')
        self.assertEqual((self.onboard / 'daemon-arguments').read_text().splitlines(),
                         ['set', '--ssh=true', '--shields-up=false'])
        self.assertEqual((self.setup / 'books-host').read_text(), '100.64.0.1\nbooks.fixture.ts.net\n')
        self.assertFalse((self.onboard / 'https-arguments').exists())

    def test_disable_laptop_access_blocks_incoming_connections(self):
        self.check_output(self.run_menu('Disable laptop access'), 'Laptop access disabled.')
        self.assertEqual((self.onboard / 'daemon-arguments').read_text().splitlines(),
                         ['set', '--ssh=false', '--shields-up=true'])

    def test_failed_enable_attempts_to_restore_blocked_incoming_connections(self):
        (self.onboard / 'daemon-mode').write_text('unavailable')
        result = self.run_menu('Enable laptop access')
        self.check_output(result, 'Could not enable laptop access.')
        self.assertNotIn('Laptop access enabled', result.stdout)
        self.assertEqual((self.onboard / 'daemon-calls').read_text().splitlines(), [
            'set --ssh=true --shields-up=false',
            'set --ssh=false --shields-up=true',
        ])

    def test_hung_enable_and_rollback_finish_before_the_menu_deadline(self):
        (self.onboard / 'daemon-mode').write_text('hung')
        self.check_output(self.run_menu('Enable laptop access'), 'Could not enable laptop access.')
        self.assertEqual(len((self.onboard / 'daemon-calls').read_text().splitlines()), 2)

    def test_failed_disable_does_not_claim_access_is_blocked(self):
        (self.onboard / 'daemon-mode').write_text('unavailable')
        result = self.run_menu('Disable laptop access')
        self.check_output(result, 'Incoming access may still be enabled.')
        self.assertNotIn('Laptop access disabled.', result.stdout)


if __name__ == '__main__':
    unittest.main()
