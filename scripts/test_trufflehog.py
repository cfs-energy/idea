"""Run the one secret scan the way hk and CI do, against planted and clean trees."""

import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCAN = ['bash', 'scripts/trufflehog-scan.sh']
# Split so this file does not match the search it runs.
REMOVED_SCANNER = 'git' + 'leaks'


def lint_build_steps():
    workflow = yaml.safe_load((ROOT / '.github/workflows/lint_build.yaml').read_text())
    return workflow['jobs']['lint_and_build']['steps']


class ScanCommandTests(unittest.TestCase):
    def test_hk_and_ci_run_the_same_scan(self):
        hook = (ROOT / 'hk.pkl').read_text().split('["trufflehog"]', 1)[1]
        argv = re.search(r'argv = List\(([^\n]+)\)', hook)[1]
        self.assertEqual(json.loads(f'[{argv}]'), SCAN)
        commands = [
            shlex.split(step['run'])
            for step in lint_build_steps()
            if 'trufflehog' in step.get('run', '')
        ]
        self.assertEqual(commands, [SCAN])

    def test_ci_installs_the_pinned_scanner(self):
        self.assertIn('trufflehog = ', (ROOT / 'mise.toml').read_text())
        steps = lint_build_steps()
        names = [step.get('name') for step in steps]
        mise = names.index('Set up pinned tools')
        self.assertLess(mise, names.index('Scan checkout for secrets'))
        install_args = steps[mise].get('with', {}).get('install_args')
        # No install_args installs every mise.toml tool.
        if install_args is not None:
            self.assertIn('trufflehog', install_args.split())

    def test_scan_keeps_its_flags(self):
        script = (ROOT / 'scripts/trufflehog-scan.sh').read_text()
        for flag in [
            '--no-update',
            '--fail',
            '--results=verified,unknown,unverified',
            '--config=.github/trufflehog-config.yaml',
            '--exclude-paths=.github/trufflehog-exclude-paths.txt',
        ]:
            self.assertIn(flag, script)

    def test_no_references_to_the_removed_scanner(self):
        if not (ROOT / '.git').exists():
            self.skipTest('needs a git checkout')
        result = subprocess.run(
            ['git', 'grep', '-n', '-i', REMOVED_SCANNER],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which('trufflehog'), 'mise install provides trufflehog')
class PositiveControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for path in [
            '.github/trufflehog-config.yaml',
            '.github/trufflehog-exclude-paths.txt',
        ]:
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / path, self.root / path)
        (self.root / 'clean.py').write_text('timeout = 30\nname = "cluster"\n')

    def scan(self):
        return subprocess.run(
            ['bash', str(ROOT / 'scripts/trufflehog-scan.sh'), str(self.root)],
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_clean_tree_passes(self):
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_planted_secrets_fail(self):
        # Custom detectors only, so nothing depends on network verification.
        value = hashlib.sha256(b'probe').hexdigest()
        short = value[:22]
        planted = {
            'settings.py': f'api_key = "{value}"\n',
            'config.json': f'{{"db_password": "{short}"}}\n',
            'deploy.env': f'SECRET_KEY="{value}"\n',
            'square.txt': f'sq0atp-{short}\n',
        }
        for name, text in planted.items():
            with self.subTest(name=name):
                target = self.root / name
                target.write_text(text)
                result = self.scan()
                target.unlink()
                self.assertEqual(result.returncode, 183, result.stdout + result.stderr)
                self.assertIn(name, result.stdout)

    def test_excluded_paths_are_not_scanned(self):
        value = hashlib.sha256(b'excluded').hexdigest()
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules/vendor.js').write_text(f'api_key = "{value}"\n')
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
