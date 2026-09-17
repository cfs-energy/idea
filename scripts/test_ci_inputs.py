"""Keep build gates fail-closed and exercise the paths that dependency updates touch."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('ci-inputs.sh').resolve()


class InputTests(unittest.TestCase):
    def run_gate(self, paths, event='pull_request', diff_status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = root / 'git'
            git.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$TEST_PATHS"\nexit "$TEST_STATUS"\n'
            )
            git.chmod(0o755)
            output = root / 'outputs'
            result = subprocess.run(
                ['bash', str(SCRIPT)],
                env={
                    **os.environ,
                    'PATH': directory + os.pathsep + os.environ['PATH'],
                    'RUNNER_TEMP': directory,
                    'GITHUB_OUTPUT': str(output),
                    'GITHUB_EVENT_NAME': event,
                    'PR_BASE_SHA': 'base',
                    'TEST_PATHS': paths,
                    'TEST_STATUS': str(diff_status),
                },
                capture_output=True,
                text=True,
            )
            values = (
                dict(line.split('=') for line in output.read_text().splitlines())
                if output.exists()
                else {}
            )
            return result.returncode, values

    def test_changed_inputs_and_docs(self):
        for path, images, release, runtimes in [
            ('requirements/dev.txt', True, False, False),
            ('deployment/ecr/idea-scheduler-pbs/Dockerfile', True, False, False),
            ('source/idea/ideactl/scripts/ideactl-linux.Dockerfile', True, True, False),
            ('source/idea/idea-bootstrap/bootstrap.py', True, True, False),
            ('source/idea/idea-cluster-manager/webapp/yarn.lock', True, False, False),
            ('software_versions.yml', True, True, True),
            ('.github/workflows/build_push.yaml', True, True, True),
            ('docs/README.md', False, False, False),
        ]:
            with self.subTest(path=path):
                code, values = self.run_gate(path)
                self.assertEqual(code, 0)
                self.assertEqual(
                    values,
                    {
                        key: str(value).lower()
                        for key, value in [
                            ('images', images),
                            ('release', release),
                            ('runtimes', runtimes),
                        ]
                    },
                )

    def test_real_renames_out_of_gated_trees(self):
        for source, release in [
            ('deployment/ecr/idea-control-plane/Dockerfile', 'false'),
            ('source/idea/ideactl/resources/input.txt', 'true'),
        ]:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)

                def git(*args):
                    return subprocess.check_output(
                        ['git', *args], cwd=root, text=True
                    ).strip()

                git('init', '-q')
                git('config', 'commit.gpgsign', 'false')
                git('config', 'user.name', 'fixture')
                git('config', 'user.email', 'fixture@example.invalid')
                path = root / source
                path.parent.mkdir(parents=True)
                path.write_text('FROM scratch\n')
                git('add', '.')
                git('commit', '-qm', 'input')
                base = git('rev-parse', 'HEAD')
                (root / 'docs').mkdir()
                git('mv', source, 'docs/example.txt')
                git('commit', '-qm', 'move')
                self.assertTrue(
                    git('diff', '--name-status', '-M', base).startswith('R100')
                )
                output = root / 'outputs'
                subprocess.run(
                    ['bash', str(SCRIPT)],
                    cwd=root,
                    env={
                        **os.environ,
                        'RUNNER_TEMP': directory,
                        'GITHUB_OUTPUT': str(output),
                        'GITHUB_EVENT_NAME': 'pull_request',
                        'PR_BASE_SHA': base,
                    },
                    check=True,
                )
                self.assertEqual(
                    dict(line.split('=') for line in output.read_text().splitlines()),
                    {'images': 'true', 'release': release, 'runtimes': 'false'},
                )

    def test_push_dispatch_and_diff_failure(self):
        for event in ['push', 'workflow_dispatch', 'workflow_call']:
            code, values = self.run_gate('', event)
            self.assertEqual(code, 0)
            self.assertEqual(set(values.values()), {'true'})
        code, values = self.run_gate('', diff_status=1)
        self.assertNotEqual(code, 0)
        self.assertEqual(values, {})


if __name__ == '__main__':
    unittest.main()
