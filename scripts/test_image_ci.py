"""Keep image smoke tests and cached environments aligned with runtime inputs."""

import base64
import hashlib
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


def _primary_fingerprint(armored):
    """V4 fingerprint of the first public-key packet in an armored key block."""
    lines = armored.strip().splitlines()
    body = lines[lines.index('') + 1 : -1]
    data = base64.b64decode(''.join(line for line in body if not line.startswith('=')))
    tag = data[0]
    if tag & 0x40:
        length, start = data[1], 2
        if length >= 192:
            length, start = ((length - 192) << 8) + data[2] + 192, 3
    else:
        width = (1, 2, 4)[tag & 0x03]
        length, start = int.from_bytes(data[1 : 1 + width], 'big'), 1 + width
    packet = data[start : start + length]
    return (
        hashlib.sha1(b'\x99' + len(packet).to_bytes(2, 'big') + packet)
        .hexdigest()
        .upper()
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

    def test_awscli_installer_is_signature_verified_before_unzip(self):
        action = ROOT / '.github/actions/setup_dev_environment'
        text = (action / 'action.yml').read_text()
        key = (action / 'awscli-public-key.asc').read_text()
        self.assertIn('-----BEGIN PGP PUBLIC KEY BLOCK-----', key)
        self.assertIn('FB5DB77FD5C118B80511ADA8A6310ACC4672475C', text)
        self.assertIn('VALIDSIG', text)
        self.assertLess(
            text.index('gpg --batch --status-fd 1 --verify'), text.index('unzip -q')
        )
        self.assertLess(
            text.index('gpg --batch --status-fd 1 --verify'), text.index('aws/install')
        )

    def test_dcv_rpms_are_verified_against_the_committed_nice_key(self):
        image = ROOT / 'deployment/ecr/idea-control-plane'
        self.assertEqual(
            _primary_fingerprint((image / 'NICE-GPG-KEY').read_text()),
            '5B9EEBC864449701F6CE566A11B5C70A170C6114',
        )
        for path in ('scripts/ci-stage-images.sh', '.github/workflows/build_push.yaml'):
            self.assertNotIn('/NICE-GPG-KEY"', (ROOT / path).read_text(), path)
        text = (image / 'Dockerfile').read_text()
        self.assertIn(
            'COPY deployment/ecr/idea-control-plane/NICE-GPG-KEY /tmp/NICE-GPG-KEY',
            text,
        )
        self.assertNotIn('/tmp/dcv-packages/NICE-GPG-KEY', text)
        check = text.index('--checksig "$R"')
        self.assertIn('*" signatures OK") ;;', text[check:])
        loop = text[text.rindex('for R in', 0, check) : check]
        for rpm in (
            'nice-dcv-session-manager-broker-amzn2023.noarch.rpm',
            'nice-dcv-connection-gateway-amzn2023.${A}.rpm',
            '/tmp/dcv/nice-dcv-web-viewer-*.rpm',
        ):
            self.assertIn(rpm, loop)
        installs = [
            line
            for line in text.splitlines()
            if 'dnf install -y' in line and 'dcv' in line
        ]
        self.assertEqual(len(installs), 3)
        for line in installs:
            self.assertLess(check, text.index(line))

    def test_release_tag_read_back_retries_stale_ecr_manifests(self):
        text = (ROOT / '.github/workflows/build_push.yaml').read_text()
        step = text.split('- name: Tag the candidate image as the release', 1)[1]
        step = step.split('- name: Publish the release', 1)[0]
        # the candidate check stays a single strict comparison
        self.assertIn(
            'test "$(digest "${REPOSITORY}:${CANDIDATE_TAG#v}")" = "${IMAGE#*@}"\n',
            step,
        )
        loop = step[step.index('for TAG in'):]
        self.assertIn('for TRY in 1 2 3 4 5 6; do', loop)
        self.assertIn('sleep 10', loop)
        self.assertIn('continue 2', loop)
        # exhausting the tries must fail the step
        self.assertLess(loop.index('done'), loop.index('exit 1'))

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
