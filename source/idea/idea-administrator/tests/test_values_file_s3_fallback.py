import botocore.exceptions
import pytest

from ideaadministrator import app_main

CLUSTER_NAME = 'c'
AWS_REGION = 'us-east-2'
BUCKET = f'{CLUSTER_NAME}-cluster-{AWS_REGION}-123456789012'
S3_URI = f's3://{BUCKET}/values/values.yml'
VALUES_YAML = 'cluster_name: c\nbase_os: amazonlinux2\n'


class FakeS3:
    def __init__(self, body=None, upload_error=None):
        self.body = body
        self.upload_error = upload_error
        self.uploaded = []

    def get_object(self, Bucket, Key):
        if self.body is None:
            raise botocore.exceptions.ClientError(
                {'Error': {'Code': 'NoSuchKey', 'Message': 'not found'}}, 'GetObject'
            )
        return {'Body': self.body}

    def upload_file(self, Bucket, Filename, Key):
        if self.upload_error is not None:
            raise self.upload_error
        self.uploaded.append((Bucket, Filename, Key))


class FakeContext:
    def __init__(self, s3):
        self.messages = []
        self._s3 = s3

    def info(self, message):
        self.messages.append(message)

    warning = info
    error = info
    success = info

    def aws(self):
        return self

    def s3(self):
        return self._s3


def stub_db(monkeypatch):
    class FakeDb:
        def __init__(self, **_kwargs):
            pass

        def get_config_entry(self, key):
            return {'key': key, 'value': BUCKET}

    monkeypatch.setattr(app_main, 'ClusterConfigDB', FakeDb)


def values_path(tmp_path):
    return tmp_path / '.idea' / 'clusters' / CLUSTER_NAME / AWS_REGION / 'values.yml'


def write_local(tmp_path, text):
    path = values_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def setup(monkeypatch, tmp_path, s3):
    # AdministratorProps resolves the values file under ~/.idea
    monkeypatch.setenv('HOME', str(tmp_path))
    stub_db(monkeypatch)
    return FakeContext(s3)


def update_base_os(context, base_os='rhel9'):
    return app_main._update_values_base_os(
        context=context,
        cluster_name=CLUSTER_NAME,
        aws_region=AWS_REGION,
        aws_profile=None,
        base_os=base_os,
    )


def save_values(context, aws_profile=None):
    app_main._save_values_file_to_bucket(
        context=context,
        cluster_name=CLUSTER_NAME,
        aws_region=AWS_REGION,
        aws_profile=aws_profile,
    )


def test_a_missing_values_file_is_restored_from_the_bucket_and_updated(
    monkeypatch, tmp_path
):
    context = setup(monkeypatch, tmp_path, FakeS3(body=VALUES_YAML))

    update_base_os(context)

    written = values_path(tmp_path).read_text()
    assert 'base_os: rhel9' in written
    # the rest of the restored file is kept, not just the base_os line
    assert 'cluster_name: c' in written
    assert any(S3_URI in message for message in context.messages)


def test_a_local_values_file_is_not_overwritten_from_the_bucket(monkeypatch, tmp_path):
    context = setup(monkeypatch, tmp_path, FakeS3(body=VALUES_YAML))
    path = write_local(tmp_path, 'cluster_name: local\nbase_os: amazonlinux2\n')

    update_base_os(context)

    assert path.read_text() == 'cluster_name: local\nbase_os: rhel9\n'


def test_a_local_copy_that_differs_from_the_bucket_is_reported(monkeypatch, tmp_path):
    # the local file wins, so the admin has to be told the bucket holds something else
    context = setup(monkeypatch, tmp_path, FakeS3(body=VALUES_YAML))
    write_local(tmp_path, 'cluster_name: local\nbase_os: amazonlinux2\n')

    update_base_os(context)

    assert any(
        'differ' in message
        and 'cluster_name' in message
        and S3_URI in message
        and 'local copy' in message
        for message in context.messages
    )


def test_a_values_file_in_neither_place_names_both_locations(monkeypatch, tmp_path):
    context = setup(monkeypatch, tmp_path, FakeS3())

    with pytest.raises(SystemExit):
        update_base_os(context)

    errors = [m for m in context.messages if 'Values file not found' in m]
    assert len(errors) == 1
    assert str(values_path(tmp_path)) in errors[0]
    assert S3_URI in errors[0]
    assert 'config save-values' in errors[0]
    assert not values_path(tmp_path).exists()


def test_a_values_file_without_a_base_os_key_fails_instead_of_claiming_success(
    monkeypatch, tmp_path
):
    # a values.yml old enough to predate the key would otherwise be reported as updated
    context = setup(monkeypatch, tmp_path, FakeS3())
    path = write_local(tmp_path, 'cluster_name: c\n')

    with pytest.raises(SystemExit):
        update_base_os(context)

    assert path.read_text() == 'cluster_name: c\n'
    assert any(
        'no base_os key' in message and 'rhel9' in message
        for message in context.messages
    )
    assert not any('Successfully updated' in message for message in context.messages)


def test_values_file_is_saved_to_the_bucket_after_a_successful_upgrade(
    monkeypatch, tmp_path
):
    s3 = FakeS3()
    context = setup(monkeypatch, tmp_path, s3)
    path = write_local(tmp_path, VALUES_YAML)

    save_values(context)

    assert s3.uploaded == [(BUCKET, str(path), 'values/values.yml')]


def test_a_failed_save_to_the_bucket_warns_with_the_command_to_run(
    monkeypatch, tmp_path
):
    # credentials that expire during a long upgrade break this upload first
    upload_error = botocore.exceptions.ClientError(
        {'Error': {'Code': 'ExpiredToken', 'Message': 'expired'}}, 'PutObject'
    )
    context = setup(monkeypatch, tmp_path, FakeS3(upload_error=upload_error))

    save_values(context, aws_profile='my-profile')

    assert any(
        f'config save-values --cluster-name {CLUSTER_NAME} --aws-region {AWS_REGION} '
        f'--aws-profile my-profile' in message
        for message in context.messages
    )
    assert any('upgrade itself' in message for message in context.messages)
