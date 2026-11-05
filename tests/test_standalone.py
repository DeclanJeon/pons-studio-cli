"""Independent extraction and distribution boundaries, not provider generation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from server.bootstrap import PIN, seed_skills
from server.skill_setup import prepare
from server.store import Store


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "creative-production").mkdir(parents=True)
        (self.source / "creative-production/SKILL.md").write_text("local authorized fixture")
        (self.source / "manifest.json").write_text(json.dumps({"skills": ["creative-production"]}))
        (self.source / "installed-commit.txt").write_text(PIN)

    def tearDown(self):
        self.temp.cleanup()

    def test_parent_studio_reference_is_not_implicitly_imported(self):
        # Given
        app = self.root / "project"
        app.mkdir()
        reference = self.root / "reference/contents-production-skills/creative-production"
        reference.mkdir(parents=True)
        (reference / "SKILL.md").write_text("must not import implicitly")
        (reference.parent.parent / "SOURCE.md").write_text(PIN)
        store = Store(self.root / "private")
        # When
        with patch.dict(os.environ, {}, clear=True):
            copied = seed_skills(store, app)
        # Then
        self.assertFalse(copied)
        self.assertFalse((store.root / "skills/source").exists())

    def test_authorized_local_source_is_copied_without_changes_or_credential_files(self):
        # Given
        (self.source / "auth.json").write_text("NOT_FOR_COPY")
        original = (self.source / "creative-production/SKILL.md").read_bytes()
        # When
        target = prepare(self.source, self.root / "private")
        # Then
        self.assertEqual((target / "creative-production/SKILL.md").read_bytes(), original)
        self.assertEqual((self.source / "creative-production/SKILL.md").read_bytes(), original)
        self.assertFalse((target / "auth.json").exists())
        self.assertEqual((target / "installed-commit.txt").read_text().strip(), PIN)

    def test_existing_skill_install_is_never_overwritten(self):
        # Given
        target = self.root / "private/skills/source"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("keep")
        # When / Then
        with self.assertRaises(SystemExit):
            prepare(self.source, self.root / "private")
        self.assertEqual((target / "keep.txt").read_text(), "keep")

    def test_wrong_pin_is_rejected_before_creating_an_install(self):
        # Given
        (self.source / "installed-commit.txt").write_text("wrong-version")
        # When / Then
        with self.assertRaises(SystemExit):
            prepare(self.source, self.root / "private")
        self.assertFalse((self.root / "private").exists())

    def test_manifest_traversal_is_rejected_before_copying(self):
        # Given
        (self.source / "manifest.json").write_text(json.dumps({"skills": ["../escape"]}))
        # When / Then
        with self.assertRaises(SystemExit):
            prepare(self.source, self.root / "private")
        self.assertFalse((self.root / "private").exists())

    def test_runtime_contracts_are_present_inside_backend_package(self):
        # Given
        import server
        resources = Path(server.__file__).parent / "resources"
        # When / Then
        self.assertTrue((resources / "PRODUCTION_HANDOFF.md").is_file())
        self.assertTrue((resources / "VIDEO_RUNTIME.md").is_file())


class DistributionTests(unittest.TestCase):
    def test_built_wheel_runs_outside_checkout_with_its_own_contracts(self):
        # Given
        wheel = os.environ.get("PONS_TEST_WHEEL")
        if not wheel:
            self.skipTest("Build a wheel and set PONS_TEST_WHEEL for distribution integration.")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unpacked = root / "installed"
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(unpacked)
            env = {**os.environ, "HOME": str(root / "home"),
                   "PYTHONPATH": str(unpacked) + os.pathsep + os.environ.get("PYTHONPATH", "")}
            code = """import json
from pathlib import Path
from importlib.metadata import distribution
import server.app
d=distribution('pons-studio-cli')
resources=Path(server.app.__file__).parent/'resources'
print(json.dumps({'backend':server.app.__file__,
 'contracts':[p.name for p in resources.glob('*.md')],
 'entrypoints':sorted(e.name for e in d.entry_points if e.group=='console_scripts')}))
"""
            # When
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=15)
            help_result = subprocess.run([sys.executable, "-m", "pons_cli", "--help"], cwd=root, env=env, capture_output=True, text=True, timeout=15)
            # Then
            self.assertEqual(result.returncode, 0, result.stderr)
            info = json.loads(result.stdout)
            self.assertTrue(Path(info["backend"]).is_relative_to(unpacked))
            self.assertEqual(set(info["contracts"]), {"PRODUCTION_HANDOFF.md", "VIDEO_RUNTIME.md"})
            self.assertEqual(info["entrypoints"], ["pons", "pons-skills"])
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--workspace", help_result.stdout)
