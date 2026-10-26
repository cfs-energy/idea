"""Exercise the actual CI and staged scanner commands with synthetic secrets."""

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


@unittest.skipUnless(shutil.which('gitleaks'), 'mise install provides gitleaks')
class GitleaksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / '.github').mkdir()
        shutil.copy(ROOT / '.github/.gitleaks.toml', self.root / '.github')
        self.git('init', '-q')

    def git(self, *args):
        return subprocess.run(
            ['git', *args], cwd=self.root, capture_output=True, check=True
        )

    def secret(self, path, line=1):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        value = hashlib.sha256(path.encode()).hexdigest()
        target.write_text('\n' * (line - 1) + f'api_key = "{value}"\n')

    def scan(self, command, expected):
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_ci_detects_a_secret_in_a_clean_checkout(self):
        workflow = yaml.safe_load(
            (ROOT / '.github/workflows/lint_build.yaml').read_text()
        )
        commands = [
            step['run']
            for step in workflow['jobs']['lint_and_build']['steps']
            if step.get('run', '').startswith('gitleaks ')
        ]
        self.assertEqual(len(commands), 1, 'CI needs a scan independent of the index')
        command = shlex.split(commands[0])
        path = Path(
            'source/idea/idea-cluster-manager/webapp/src/components/list-view/list-view.tsx'
        )
        (self.root / path).parent.mkdir(parents=True)
        shutil.copy(ROOT / path, self.root / path)
        self.scan(command, 0)
        self.secret('probe.txt')
        self.git('add', '.')
        self.git(
            '-c',
            'user.name=Scanner Test',
            '-c',
            'user.email=test@example.invalid',
            '-c',
            'core.hooksPath=/dev/null',
            'commit',
            '--no-gpg-sign',
            '-qm',
            'fixture',
        )
        self.assertEqual(self.git('status', '--porcelain').stdout, b'')
        self.scan(command, 1)

    def test_line_moves_do_not_hide_new_secrets(self):
        command = [
            'gitleaks',
            'dir',
            '--redact',
            '--config',
            '.github/.gitleaks.toml',
            '.',
        ]
        paths = [
            'source/idea/idea-cluster-manager/webapp/src/components/list-view/list-view.tsx',
            'source/idea/idea-sdk/src/ideasdk/config/cluster_config_db.py',
        ]
        for path in paths:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('\n' * 17 + (ROOT / path).read_text())
        self.scan(command, 0)
        for path, line in [
            (paths[0], 305),
            (paths[0], 311),
            (paths[0], 386),
            (paths[0], 392),
            (paths[1], 374),
        ]:
            with self.subTest(path=path, line=line):
                target = self.root / path
                original = target.read_text()
                self.secret(path, line)
                self.scan(command, 1)
                target.write_text(original)
        self.secret('unreviewed.txt')
        self.git('add', '.')
        hook = (ROOT / 'hk.pkl').read_text().split('["gitleaks"]', 1)[1]
        argv = re.search(r'argv = List\(([^\n]+)\)', hook)[1]
        self.scan(json.loads(f'[{argv}]'), 1)

    def test_no_line_only_fingerprints_or_ineffective_rule_allowlist(self):
        self.assertFalse((ROOT / '.gitleaksignore').exists())
        self.assertNotIn(
            'square-access-token', (ROOT / '.github/.gitleaks.toml').read_text()
        )


if __name__ == '__main__':
    unittest.main()
