"""Real PTY and detached backend checks, with only Codex replaced at process boundary."""
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pons_cli.backend import read_endpoint
from pons_cli.client import Client
from pons_cli.render import safe_text
from server.runner import SKILL_COMMIT
from server.store import Problem, Store

FAKE = '''import json,sys
if sys.argv[1:3] == ['login','status']:
 print('Logged in using ChatGPT');sys.exit(0)
if sys.argv[1:3] == ['features','list']:
 print('image_generation stable true');sys.exit(0)
sys.stdin.read()
print(json.dumps({'type':'thread.started','thread_id':'fixture-session'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'fixture-reply'}}),flush=True)
'''


class ShellSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'private'
        self.workspace = self.root / 'videos'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'codex'
        fake.write_text('#!' + sys.executable + '\n' + FAKE)
        fake.chmod(0o700)
        home = self.root / 'home'
        home.mkdir()
        (home / 'config.toml').write_text('')
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'], 'CODEX_HOME': str(home), 'HOME': str(home)}
        store = Store(self.data)
        source = self.data / 'skills' / 'source'
        (source / 'creative-production').mkdir(parents=True)
        (source / 'creative-production' / 'SKILL.md').write_text('---\nname: creative-production\n---\nOffline test fixture.')
        (source / 'installed-commit.txt').write_text(SKILL_COMMIT)
        self.processes = []
        self.masters = []
        self.app = Path(__file__).resolve().parents[1]

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for master in self.masters:
            os.close(master)
        endpoint = self.data / 'desktop-endpoint.json'
        if endpoint.exists():
            info = read_endpoint(self.data)
            client = Client(info.url)
            try:
                client.request('POST', '/api/desktop/shutdown', {}, {'X-Pons-Desktop': info.token})
            except (Problem, OSError):
                # Cleanup-only process ownership: fake backend created by this test.
                os.kill(json.loads(endpoint.read_text())['pid'], signal.SIGTERM)
            deadline = time.monotonic() + 5
            while endpoint.exists() and time.monotonic() < deadline:
                select.select([], [], [], 0.05)
        self.temp.cleanup()

    def start_shell(self, *extra):
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, '-m', 'pons_cli', '--data', str(self.data), '--workspace', str(self.workspace), *extra], cwd=self.app, env=self.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.processes.append(process)
        self.masters.append(master)
        return process, master

    def read_until(self, master, marker, timeout=10):
        result = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                part = os.read(master, 65536)
            except OSError:
                break
            result += part
            if marker.encode() in result:
                decoded = result.decode(errors='replace')
                evidence = os.environ.get('PONS_TEST_EVIDENCE')
                if evidence:
                    folder = Path(evidence)
                    folder.mkdir(parents=True, exist_ok=True)
                    with (folder / (self._testMethodName + '.txt')).open('a') as transcript:
                        transcript.write(safe_text(decoded))
                return decoded
        self.fail(f'PTY did not reach {marker!r}: {result.decode(errors="replace")}')

    def test_authenticated_entry_requires_no_setup_and_delivers_actual_agent_reply(self):
        # Given
        process, master = self.start_shell()
        self.read_until(master, 'pons> ')
        # When
        os.write(master, '/plan 영상 기획만 설명해줘\n'.encode())
        output = self.read_until(master, 'fixture-reply')
        os.write(master, b'/exit\n')
        process.wait(timeout=8)
        # Then
        store = Store(self.data)
        project = store.projects()[0]
        self.assertEqual(project['session_id'], 'fixture-session')
        self.assertEqual(project['request']['user_prompt'], '영상 기획만 설명해줘')
        self.assertFalse(project['request']['execution_authorized'])
        self.assertEqual(store.project_root(project['id']).parent, self.workspace / 'pons-projects')
        self.assertIn('fixture-reply', output)
        self.assertEqual(process.returncode, 0)
        info = read_endpoint(self.data)
        self.assertEqual(Client(info.url).request('GET', '/api/desktop/health', headers={'X-Pons-Desktop': info.token})['data_id'], info.data_id)

    def test_two_cli_connections_share_one_backend(self):
        # Given
        first, master1 = self.start_shell()
        # When: start the other CLI before waiting for the first backend.
        second, master2 = self.start_shell()
        self.read_until(master1, 'pons> ')
        original = json.loads((self.data / 'desktop-endpoint.json').read_text())
        self.read_until(master2, 'pons> ')
        current = json.loads((self.data / 'desktop-endpoint.json').read_text())
        os.write(master1, b'/exit\n')
        os.write(master2, b'/exit\n')
        first.wait(timeout=8)
        second.wait(timeout=8)
        # Then
        self.assertEqual(original['pid'], current['pid'])
        self.assertEqual(original['url'], current['url'])

    def test_execute_requires_real_confirmation_and_does_not_accept_shell_syntax(self):
        # Given
        process, master = self.start_shell()
        self.read_until(master, 'pons> ')
        # When
        os.write(master, '60초 영상을 만들어줘 $(touch NEVER_EXECUTE)\n'.encode())
        self.read_until(master, '[y/N] ')
        os.write(master, b'y\n')
        self.read_until(master, 'fixture-reply')
        os.write(master, b'/exit\n')
        process.wait(timeout=8)
        # Then
        store = Store(self.data)
        self.assertTrue(store.projects()[0]['request']['execution_authorized'])
        self.assertFalse((self.app / 'NEVER_EXECUTE').exists())
        self.assertFalse((self.workspace / 'NEVER_EXECUTE').exists())

    def test_interrupt_cancels_input_but_keeps_backend_alive(self):
        # Given
        process, master = self.start_shell()
        self.read_until(master, 'pons> ')
        info = read_endpoint(self.data)
        # When
        process.send_signal(signal.SIGINT)
        self.read_until(master, '입력을 취소했습니다')
        os.write(master, b'/exit\n')
        process.wait(timeout=8)
        # Then
        self.assertEqual(process.returncode, 0)
        self.assertEqual(Client(info.url).request('GET', '/api/desktop/health', headers={'X-Pons-Desktop': info.token})['data_id'], info.data_id)
        self.assertEqual(Store(self.data).projects(), [])

    def test_eof_detaches_without_shutting_down_worker(self):
        # Given
        process, master = self.start_shell()
        self.read_until(master, 'pons> ')
        info = read_endpoint(self.data)
        # When
        os.write(master, b'\x04')
        process.wait(timeout=8)
        # Then
        self.assertEqual(process.returncode, 0)
        self.assertEqual(Client(info.url).request('GET', '/api/desktop/health', headers={'X-Pons-Desktop': info.token})['data_id'], info.data_id)

    def test_interrupt_cancels_only_pending_login(self):
        # Given
        process, master = self.start_shell()
        self.read_until(master, 'pons> ')
        # When
        os.write(master, b'/settings\n1\n')
        self.read_until(master, '로그인 대기 중입니다')
        process.send_signal(signal.SIGINT)
        self.read_until(master, '로그인만 취소했습니다')
        os.write(master, b'\n/exit\n')
        process.wait(timeout=8)
        # Then
        self.assertEqual(process.returncode, 0)
        self.assertEqual((self.root / 'home/config.toml').read_text(), '')
        self.assertEqual(Store(self.data).projects(), [])

    def test_noninteractive_missing_workspace_returns_actionable_error(self):
        # Given / When
        result = subprocess.run([sys.executable, '-m', 'pons_cli', '--data', str(self.data), '--json'], cwd=self.app, env=self.env, input='', capture_output=True, text=True, timeout=10)
        # Then
        self.assertEqual(result.returncode, 3)
        self.assertIn('--workspace', json.loads(result.stdout)['error'])
        self.assertEqual(Store(self.data).projects(), [])

    def test_terminal_controls_are_removed_but_korean_is_preserved(self):
        # Given / When
        result = safe_text('한글\x1b]52;c;secret\x07\x1b[31mred\x1b[0m\r\x08')
        # Then
        self.assertEqual(result, '한글red')
