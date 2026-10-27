"""Install the same checksum-pinned DynamoDB Local for CI and service tests."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen
import zipfile

DYNAMODB_LOCAL_PACKAGE_URL = (
    'https://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_2025-04-14.zip'
)
DYNAMODB_LOCAL_PACKAGE_SHA256_CHECKSUM = (
    '06e7bdd5d03262d8373696282f79867f9f6beb94b76e230b49da135be080558c'
)


def install(idea_home: Path) -> None:
    library = idea_home / 'lib/dynamodb-local'
    library.parent.mkdir(parents=True, exist_ok=True)
    # A failed download never becomes a cached installation.
    with tempfile.TemporaryDirectory(dir=library.parent) as scratch:
        archive = library / 'dynamodb.zip'
        if not archive.is_file():
            archive = Path(scratch) / 'dynamodb.zip'
            print(f'downloading ddb local: {DYNAMODB_LOCAL_PACKAGE_URL} ...')
            with urlopen(DYNAMODB_LOCAL_PACKAGE_URL, timeout=120) as response:
                with archive.open('wb') as output:
                    shutil.copyfileobj(response, output)
        with archive.open('rb') as source:
            checksum = hashlib.file_digest(source, 'sha256').hexdigest()
        if checksum != DYNAMODB_LOCAL_PACKAGE_SHA256_CHECKSUM:
            raise ValueError('DynamoDB Local checksum mismatch')
        extracted = Path(scratch) / 'extracted'
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        if not (extracted / 'DynamoDBLocal.jar').is_file():
            raise ValueError('DynamoDB Local archive has no jar')
        # Restore the jar and all dependencies from verified bytes, even on a cache hit.
        shutil.copyfile(archive, extracted / 'dynamodb.zip')
        if library.exists():
            shutil.rmtree(library)
        extracted.rename(library)


if __name__ == '__main__':
    install(Path(os.environ.get('IDEA_USER_HOME', Path.home() / '.idea')))
