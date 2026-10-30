"""Keep image smoke tests and cached environments aligned with runtime inputs."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
IMPORTS = (
    'asyncssh',
    'ideaclustermanager.app.cluster_manager_app',
    'ideascheduler.app.scheduler_app',
    'ideascheduler.app.sync_users',
    'ideavirtualdesktopcontroller.app.virtual_desktop_controller_app',
)


class ImageCiTests(unittest.TestCase):
    def test_both_smokes_import_services_in_the_image(self):
        for path in ('scripts/ci-smoke-images.sh', '.github/workflows/build_push.yaml'):
            lines = (ROOT / path).read_text().splitlines()
            commands = [line for line in lines if '--entrypoint python3.13' in line]
            self.assertEqual(len(commands), 1, path)
            for option in (
                '--network none',
                '--env IDEA_CLUSTER_NAME=smoke',
                '--env AWS_DEFAULT_REGION=us-east-1',
                '--env AWS_EC2_METADATA_DISABLED=true',
            ):
                self.assertIn(option, commands[0])
            for module in IMPORTS:
                self.assertIn(module, commands[0])

    def test_venv_cache_only_restores_exact_lock(self):
        action = yaml.safe_load(
            (ROOT / '.github/actions/setup_dev_environment/action.yml').read_text()
        )
        cache = next(
            step for step in action['runs']['steps'] if step.get('id') == 'cache-venv'
        )
        self.assertNotIn('restore-keys', cache['with'])
        self.assertIn("hashFiles('requirements/dev.txt')", cache['with']['key'])

    def test_unused_signer_is_absent_from_runtime_and_dev(self):
        for name in ('runtime.in', 'runtime.txt', 'dev.txt'):
            self.assertNotIn(
                'requests-aws4auth', (ROOT / 'requirements' / name).read_text()
            )

    def test_every_sdist_pins_the_build_backend(self):
        import tomllib

        for module in (
            'data-model',
            'sdk',
            'scheduler',
            'cluster-manager',
            'virtual-desktop-controller',
        ):
            path = ROOT / f'source/idea/idea-{module}/src/pyproject.toml'
            self.assertTrue(path.exists(), str(path))
            build = tomllib.loads(path.read_text())['build-system']
            self.assertEqual(build['requires'], ['setuptools==84.2.1'])
            self.assertEqual(build['build-backend'], 'setuptools.build_meta:__legacy__')


if __name__ == '__main__':
    unittest.main()
