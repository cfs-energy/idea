"""Exercise drift detection with a real resolver and a tiny offline wheel index."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location(
    'locks', Path(__file__).with_name('requirements-locks.py')
)
locks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(locks)


class LockTests(unittest.TestCase):
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
