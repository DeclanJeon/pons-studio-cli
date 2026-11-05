"""Persistent workflow contracts use real files and real SQLite transactions."""
import tempfile
import unittest
from pathlib import Path

from server.store import Problem, Store
from server.workflow import Workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'data')
        self.project = self.store.create({'user_prompt': 'film', 'workflow_enabled': True}, 'one')['id']
        self.root = self.store.project_root(self.project)
        self.root.mkdir(parents=True)
        (self.root / 'request.json').write_text('{"user_prompt":"film"}')
        self.workflow = Workflow(self.store, self.project)

    def tearDown(self):
        self.temp.cleanup()

    def test_nodes_survive_reopen_without_being_marked_complete(self):
        # Given / When
        result = Workflow(Store(self.store.root), self.project).snapshot()
        # Then
        self.assertEqual(result['recipe'], 'narrated_stills')
        self.assertTrue(all(n['state'] == 'pending' for n in result['nodes']))

    def test_stage_cannot_start_before_dependencies_finish(self):
        # Given / When / Then
        with self.assertRaises(Problem):
            self.workflow.start('render')
        self.assertTrue(all(n['state'] == 'pending' for n in self.workflow.snapshot()['nodes']))

    def test_completion_requires_current_lease_and_real_files(self):
        # Given
        lease = self.workflow.start('intake')
        # When / Then
        with self.assertRaises(Problem):
            self.workflow.complete('intake', 'stale', ['request.json'])
        with self.assertRaises(Problem):
            self.workflow.complete('intake', lease, ['missing.md'])
        self.workflow.complete('intake', lease, ['request.json'])
        self.assertEqual(self.workflow.snapshot()['nodes'][0]['state'], 'succeeded')
        self.assertTrue(self.workflow.start('story'))

    def test_modified_dependency_output_invalidates_next_start(self):
        # Given
        lease = self.workflow.start('intake')
        self.workflow.complete('intake', lease, ['request.json'])
        (self.root / 'request.json').write_text('changed after completion')
        # When / Then
        with self.assertRaises(Problem):
            self.workflow.start('story')

    def test_failed_node_cannot_be_completed_with_old_lease(self):
        # Given
        lease = self.workflow.start('intake')
        self.workflow.block('intake', lease, 'missing runtime')
        # When / Then
        with self.assertRaises(Problem):
            self.workflow.complete('intake', lease, ['request.json'])
        self.assertEqual(self.workflow.snapshot()['nodes'][0]['state'], 'failed')

    def test_only_optional_character_stage_can_be_skipped(self):
        # Given / When / Then
        with self.assertRaises(Problem):
            self.workflow.skip('review', 'no observer')
        with self.assertRaises(Problem):
            self.workflow.skip('characters', '')
        self.workflow.skip('characters', 'no characters in this documentary')
        node = next(n for n in self.workflow.snapshot()['nodes'] if n['id'] == 'characters')
        self.assertEqual(node['state'], 'skipped')

    def test_restart_reconciles_running_node_and_preserves_lease_history(self):
        # Given
        self.workflow.start('intake')
        # When
        self.store.recover()
        # Then
        self.assertEqual(self.workflow.snapshot()['nodes'][0]['state'], 'reconciling')
        with self.assertRaises(Problem):
            self.workflow.start('intake')
