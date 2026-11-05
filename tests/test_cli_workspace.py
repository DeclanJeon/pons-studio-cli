"""Workspace boundaries use real temporary directories and SQLite."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.store import Problem, Store
from pons_cli.paths import data_directory, workspace_directory


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'private')

    def tearDown(self):
        self.temp.cleanup()

    def test_new_project_uses_selected_workspace_without_moving_legacy(self):
        # Given
        legacy = self.store.create({'user_prompt': 'old'}, 'legacy')
        workspace = self.root / 'videos'
        # When
        project = self.store.create({'user_prompt': 'new', 'workspace': str(workspace)}, 'new')
        reopened = Store(self.store.root)
        # Then
        self.assertEqual(reopened.project_root(project['id']), workspace / 'pons-projects' / project['id'])
        self.assertTrue(reopened.project_root(project['id']).is_dir())
        self.assertEqual(reopened.project_root(legacy['id']), self.store.root / 'projects' / legacy['id'])
        self.assertFalse((workspace / 'vault.key').exists())

    def test_workspace_rejects_control_data_overlap(self):
        # Given
        candidates = [self.store.root, self.store.root / 'outputs', self.root]
        # When / Then
        for path in candidates:
            with self.subTest(path=path), self.assertRaises(Problem):
                self.store.create({'user_prompt': 'no', 'workspace': str(path)}, str(path))
        self.assertEqual(self.store.projects(), [])

    def test_workspace_rejects_symlink_even_when_target_is_a_directory(self):
        # Given
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        # When / Then
        with self.assertRaises(Problem):
            self.store.create({'user_prompt': 'no', 'workspace': str(link)}, 'link')

    def test_removed_workspace_is_not_silently_recreated(self):
        # Given
        p = self.store.create({'user_prompt': 'new', 'workspace': str(self.root / 'work')}, 'one')
        project_root = self.store.project_root(p['id'])
        project_root.rmdir()
        # When / Then
        with self.assertRaises(Problem) as error:
            self.store.project_root(p['id'])
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(project_root.exists())

    def test_data_precedence_uses_explicit_then_environment_then_legacy(self):
        # Given
        legacy = self.root / 'app' / '.studio'
        legacy.mkdir(parents=True)
        # When / Then
        with patch.dict(os.environ, {'STUDIO_DATA': str(self.root / 'env')}, clear=True):
            self.assertEqual(data_directory(str(self.root / 'explicit'), self.root / 'app'), self.root / 'explicit')
            self.assertEqual(data_directory(None, self.root / 'app'), self.root / 'env')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(data_directory(None, self.root / 'app'), legacy)

    def test_workspace_rejects_existing_file(self):
        # Given
        file = self.root / 'file'
        file.write_text('keep')
        # When / Then
        with self.assertRaises(Problem):
            workspace_directory(str(file), self.store.root)
        self.assertEqual(file.read_text(), 'keep')
