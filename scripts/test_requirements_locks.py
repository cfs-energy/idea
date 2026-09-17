"""Exercise drift detection with a real resolver and a tiny offline wheel index."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location(
    'locks', Path(__file__).with_name('requirements-locks.py')
)
locks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(locks)


class LockTests(unittest.TestCase):
    def test_relaunch_uses_configured_python_and_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'requirements').mkdir()
            (root / 'requirements/dev.txt').write_text(
                'pip-tools==7.6.1\nclick==8.4.2\n'
            )
            for version, system, force in (
                ('3.13.15', 'Darwin', False),
                ('3.14.0', 'Darwin', False),
                ('3.14.0', 'Linux', True),
            ):
                (root / 'software_versions.yml').write_text(
                    f'python_version: {version}\n'
                )
                with (
                    self.subTest(version=version),
                    patch.object(locks, 'ROOT', root),
                    patch.object(locks.platform, 'system', return_value=system),
                    patch.dict(locks.os.environ, {}, clear=True),
                    patch.object(
                        locks.sys, 'argv', ['requirements-locks.py', '--check']
                    ),
                    patch.object(locks.subprocess, 'run') as run,
                ):
                    run.return_value.returncode = 0
                    with self.assertRaises(SystemExit) as result:
                        locks.relaunch_on_linux(force=force)
                    self.assertEqual(result.exception.code, 0)
                    command = run.call_args.args[0]
                    self.assertIn(f'python:{version}-slim', command)
                    self.assertIn(
                        'pip install -q pip-tools==7.6.1 click==8.4.2', command[-3]
                    )
                    self.assertEqual(command[-1], '--check')

    def test_recompile_detects_input_and_output_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / 'requirements'
            wheels = requirements / 'wheels'
            wheels.mkdir(parents=True)
            for version in ('1.0', '2.0'):
                info = f'lock_probe-{version}.dist-info'
                with zipfile.ZipFile(
                    wheels / f'lock_probe-{version}-py3-none-any.whl', 'w'
                ) as wheel:
                    wheel.writestr(
                        f'{info}/METADATA',
                        f'Metadata-Version: 2.1\nName: lock-probe\nVersion: {version}\n',
                    )
                    wheel.writestr(
                        f'{info}/WHEEL',
                        'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
                    )
                    wheel.writestr(f'{info}/RECORD', '')
            source = requirements / 'probe.in'
            source.write_text(f'--no-index\n--find-links {wheels}\nlock-probe==1.0\n')
            locks.compile_locks(root)
            locks.check_locks(root)
            output = source.with_suffix('.txt')
            canonical = output.read_text()
            # Hosted compiler flag formatting must be replaced before the CI byte check.
            output.write_text(canonical.replace('--strip-extras', '--no-strip-extras'))
            with self.assertRaisesRegex(SystemExit, 'Requirements locks differ'):
                locks.check_locks(root)
            locks.compile_locks(root)
            self.assertEqual(output.read_text(), canonical)
            source.write_text(source.read_text().replace('==1.0', '==2.0'))
            with self.assertRaisesRegex(SystemExit, 'Requirements locks differ'):
                locks.check_locks(root)
            locks.compile_locks(root)
            output = source.with_suffix('.txt')
            output.write_text(output.read_text().replace('==2.0', '==1.0'))
            with self.assertRaisesRegex(SystemExit, 'Requirements locks differ'):
                locks.check_locks(root)


if __name__ == '__main__':
    unittest.main()
