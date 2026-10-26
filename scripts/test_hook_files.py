"""Check byte-level formatting, classification, and index-sensitive hook behavior."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('hook-files.py')
spec = importlib.util.spec_from_file_location('hook_files', SCRIPT)
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)


class HookTests(unittest.TestCase):
    def test_text_formatting_preserves_line_endings_and_empty_files(self):
        cases = [
            ('trailing-whitespace', b'a  \r\nb\t\n', b'a\r\nb\n'),
            ('trailing-whitespace', b'a\v\fb  ', b'a\v\fb'),
            ('end-of-file-fixer', b'', b''),
            ('end-of-file-fixer', b'\n\r\n', b''),
            ('end-of-file-fixer', b'a', b'a\n'),
            ('end-of-file-fixer', b'a\r\n\n', b'a\r\n'),
            ('end-of-file-fixer', b'a\r', b'a\r'),
            (
                'pretty-format-json',
                b'{\r\n  "a": 1\r\n}\r\n',
                b'{\r\n  "a": 1\r\n}\r\n',
            ),
            ('remove-tabs', b'a\tb\t\n', b'a   b   \n'),
            ('remove-crlf', b'a\r\nb\r', b'a\nb\r'),
            ('fix-byte-order-marker', b'\xef\xbb\xbfa\n', b'a\n'),
            ('pretty-format-json', b'{"z":1,"a":2}', b'{\n  "z": 1,\n  "a": 2\n}\n'),
        ]
        for kind, original, expected in cases:
            with self.subTest(kind=kind, original=original):
                self.assertEqual(hooks.format_bytes(kind, original), expected)
                self.assertEqual(hooks.format_bytes(kind, expected), expected)

    def test_json_rejects_duplicate_keys(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate key'):
            hooks.unique_keys([('a', 1), ('a', 2)])

    def test_type_selection_skips_unrelated_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = root / 'invalid.txt'
            data = root / 'invalid.json'
            link = root / 'link.json'
            text.write_text('not json\n')
            data.write_text('not json\n')
            link.symlink_to(data)
            command = [
                sys.executable,
                str(SCRIPT),
                '--type',
                'json',
                '--check',
                'check-json',
            ]
            result = subprocess.run(
                [*command, str(text), str(link)], capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([*command, str(data)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'invalid.json', result.stdout)

    def test_check_does_not_write_but_fix_does(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.txt'
            path.write_bytes(b'hello  \n')
            self.assertEqual(hooks.check_files('trailing-whitespace', [str(path)]), 1)
            self.assertEqual(path.read_bytes(), b'hello  \n')
            self.assertEqual(
                hooks.check_files('trailing-whitespace', [str(path)], fix=True), 1
            )
            self.assertEqual(path.read_bytes(), b'hello\n')

    def test_large_files_only_checks_new_index_entries_and_skips_lfs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                subprocess.run(
                    ['git', *args], cwd=root, check=True, capture_output=True
                )

            git('init', '-q')
            large = root / 'large.bin'
            large.write_bytes(b'\0' * (500 * 1024 + 1))
            command = [
                sys.executable,
                str(SCRIPT.resolve()),
                '--check',
                'check-added-large-files',
                'large.bin',
            ]
            self.assertEqual(
                subprocess.run(command, cwd=root, capture_output=True).returncode, 0
            )
            git('add', 'large.bin')
            self.assertEqual(
                subprocess.run(command, cwd=root, capture_output=True).returncode, 1
            )
            (root / '.gitattributes').write_text('*.bin filter=lfs\n')
            self.assertEqual(
                subprocess.run(command, cwd=root, capture_output=True).returncode, 0
            )


if __name__ == '__main__':
    unittest.main()
