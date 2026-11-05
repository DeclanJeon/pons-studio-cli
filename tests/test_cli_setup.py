"""Existing authentication must bypass setup without importing secrets."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.capabilities import authentication, select_system_connections
from server.store import Store, atomic_json
from pons_cli.setup import ensure_ready
from pons_cli.client import Client


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.home = self.root / 'codex'
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_authenticated_existing_account_is_reused_without_importing_connections(self):
        # Given
        with patch('server.capabilities.shutil.which', return_value='/fake/codex'), patch('server.capabilities.system.system_home', return_value=self.home), patch('server.capabilities.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'Logged in using ChatGPT', '')):
            # When
            result = authentication(self.store)
        # Then
        self.assertEqual(result.state, 'authenticated')
        self.assertEqual(result.method, 'chatgpt')
        self.assertEqual(self.store.connections(), [])
        import json
        runtime = json.loads((self.store.root / 'runtime.json').read_text())
        self.assertEqual(runtime['system_codex_home'], str(self.home))

    def test_existing_account_does_not_require_a_codex_config_file(self):
        # Given
        atomic_json(self.store.root / 'runtime.json', {'codex_mode': 'system', 'system_codex_home': str(self.home)})
        # When
        from server.system_integration import system_config
        result = system_config(self.store)
        # Then
        self.assertEqual(result, {})
        self.assertFalse((self.home / 'config.toml').exists())

    def test_explicit_skill_source_seeds_instructions_without_downloading(self):
        # Given
        from server.bootstrap import PIN, seed_skills
        app = self.root / 'export' / 'app'
        app.mkdir(parents=True)
        source = self.root / 'explicit-skills'
        (source / 'creative-production').mkdir(parents=True)
        (source / 'creative-production/SKILL.md').write_text('offline instructions')
        (source / 'installed-commit.txt').write_text(PIN)
        # When
        with patch.dict(os.environ, {'PONS_SKILLS_SOURCE': str(source)}):
            seeded = seed_skills(self.store, app)
        # Then
        self.assertTrue(seeded)
        self.assertEqual((self.store.root / 'skills/source/creative-production/SKILL.md').read_text(), 'offline instructions')
        self.assertFalse(seed_skills(self.store, app))

    def test_timeout_is_unknown_and_existing_credentials_are_preserved(self):
        # Given
        auth = self.home / 'auth.json'
        auth.write_text('do-not-delete')
        with patch('server.capabilities.shutil.which', return_value='/fake/codex'), patch('server.capabilities.system.system_home', return_value=self.home), patch('server.capabilities.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 5)):
            # When
            result = authentication(self.store)
        # Then
        self.assertEqual(result.state, 'unknown')
        self.assertEqual(auth.read_text(), 'do-not-delete')

    def test_authenticated_cli_skips_all_setup_questions(self):
        # Given
        with patch.object(Client, 'request', return_value={'state': 'authenticated', 'method': 'chatgpt', 'installed': True}), patch('builtins.input', side_effect=AssertionError('setup must be skipped')):
            # When
            result = ensure_ready(Client('http://127.0.0.1:1'))
        # Then
        self.assertEqual(result, 'authenticated')

    def test_unknown_auth_does_not_force_login(self):
        # Given
        with patch.object(Client, 'request', return_value={'state': 'unknown', 'method': 'unknown', 'installed': True}), patch('builtins.input', side_effect=AssertionError('must not prompt')):
            # When
            result = ensure_ready(Client('http://127.0.0.1:1'))
        # Then
        self.assertEqual(result, 'unknown')

    def test_selected_system_mcp_import_does_not_read_api_key_files(self):
        # Given
        atomic_json(self.store.root / 'runtime.json', {'codex_mode': 'system', 'system_codex_home': str(self.home)})
        (self.home / 'config.toml').write_text('[mcp_servers.fish-audio]\nurl="https://example.test/mcp"\n[mcp_servers.elevenlabs]\nurl="https://example.test/other"\n')
        # When
        report = select_system_connections(self.store, ['fish-audio'])
        # Then
        self.assertTrue(report)
        self.assertEqual({c['config']['system_mcp_name'] for c in self.store.connections()}, {'fish-audio'})
        self.assertEqual({c['transport'] for c in self.store.connections()}, {'system_mcp'})
        self.assertTrue(all(c['status'] == 'saved' for c in self.store.connections()))
