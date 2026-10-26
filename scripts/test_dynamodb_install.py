"""Exercise the shared installer without service dependencies or network access."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'source/idea/idea-test-utils/src/ideatestutils/dynamodb/install.py'


class DynamoDBInstallTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('ddb_install', INSTALLER)
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('DynamoDBLocal.jar', 'test jar')
        self.archive = archive.getvalue()
        checksum = hashlib.sha256(self.archive).hexdigest()
        patcher = patch.object(
            self.installer, 'DYNAMODB_LOCAL_PACKAGE_SHA256_CHECKSUM', checksum
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self, content):
        return patch.object(self.installer, 'urlopen', return_value=io.BytesIO(content))

    def test_versioned_install_and_cache_hit(self):
        self.assertNotIn('latest', self.installer.DYNAMODB_LOCAL_PACKAGE_URL)
        with self.download(self.archive) as download:
            self.installer.install(self.home)
            self.installer.install(self.home)
        download.assert_called_once()
        self.assertEqual(
            (self.home / 'lib/dynamodb-local/DynamoDBLocal.jar').read_text(), 'test jar'
        )

    def test_corrupt_download_is_never_reused(self):
        for _ in range(2):
            with self.download(b'corrupt') as download:
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    self.installer.install(self.home)
                download.assert_called_once()
        self.assertFalse((self.home / 'lib/dynamodb-local/DynamoDBLocal.jar').exists())
        with self.download(self.archive):
            self.installer.install(self.home)

    def test_old_install_is_replaced(self):
        library = self.home / 'lib/dynamodb-local'
        library.mkdir(parents=True)
        (library / 'DynamoDBLocal.jar').write_text('old jar')
        with self.download(self.archive):
            self.installer.install(self.home)
        self.assertEqual((library / 'DynamoDBLocal.jar').read_text(), 'test jar')

    def test_ci_paths_share_the_installer_and_cache(self):
        action = (ROOT / '.github/actions/setup_dynamodb_local/action.yml').read_text()
        self.assertIn('actions/cache@', action)
        self.assertIn(
            "hashFiles('source/idea/idea-test-utils/src/ideatestutils/dynamodb/install.py')",
            action,
        )
        self.assertIn(str(INSTALLER.relative_to(ROOT)), action)
        setup = (ROOT / '.github/actions/setup_dev_environment/action.yml').read_text()
        checks = (ROOT / '.github/workflows/ideactl_checks.yaml').read_text()
        self.assertIn('uses: ./.github/actions/setup_dynamodb_local', setup)
        self.assertEqual(
            checks.count('uses: ./.github/actions/setup_dynamodb_local'), 2
        )
        self.assertNotIn('dynamodb_local_latest', checks)


if __name__ == '__main__':
    unittest.main()
