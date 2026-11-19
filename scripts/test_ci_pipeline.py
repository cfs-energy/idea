"""Keep CI credentials, installs and release inputs inside the boundaries the workflows promise."""

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _steps(job):
    return job.get('steps') or []


class CiPipelineTests(unittest.TestCase):
    def test_python_test_oracles_install_from_the_hashed_lock(self):
        jobs = _workflow('ideactl_checks.yaml')['jobs']
        installs = [
            step['run']
            for name in ('checks', 'private-parity-build')
            for step in _steps(jobs[name])
            if 'pip install' in step.get('run', '')
        ]
        self.assertEqual(len(installs), 2)
        for run in installs:
            self.assertNotRegex(run, r"pip install '[^']*==")
            self.assertIn('--require-hashes', run)
            with tempfile.TemporaryDirectory() as temp:
                extract = next(
                    line for line in run.splitlines() if line.startswith('awk ')
                )
                subprocess.run(
                    ['bash', '-ec', extract],
                    cwd=ROOT / 'source/idea/ideactl',
                    env={**os.environ, 'RUNNER_TEMP': temp},
                    check=True,
                )
                oracles = Path(temp, 'oracles.txt').read_text()
            names = re.findall(r'^([a-z0-9-]+)==', oracles, re.M)
            self.assertEqual(names, ['jinja2', 'markupsafe', 'pyyaml'])
            self.assertGreaterEqual(oracles.count('--hash=sha256:'), 3)

    def test_renovate_locks_generate_without_credentials_and_commit_without_tooling(
        self,
    ):
        workflow = _workflow('renovate_locks.yaml')
        text = (WORKFLOWS / 'renovate_locks.yaml').read_text()
        self.assertEqual(
            workflow['concurrency']['group'],
            'renovate-locks-${{ github.event.pull_request.number }}',
        )
        generate, commit = workflow['jobs']['generate'], workflow['jobs']['commit']
        self.assertIn(
            'github.event.pull_request.head.repo.full_name == github.repository',
            generate['if'],
        )
        self.assertEqual(generate['permissions'], {'contents': 'read'})
        self.assertEqual(commit['permissions'], {'contents': 'read'})
        self.assertNotIn('secrets.', yaml.safe_dump(generate))
        for job in (generate, commit):
            checkout = next(
                s
                for s in _steps(job)
                if s.get('uses', '').startswith('actions/checkout@')
            )
            self.assertIs(checkout['with']['persist-credentials'], False)
        # the writer runs git and the accept script, never dependency tooling
        commands = '\n'.join(step.get('run', '') for step in _steps(commit)).replace(
            'yarn.lock', ''
        )
        self.assertNotRegex(commands, r'\b(mise|npm|npx|yarn|pip3?|node|python3?|uv)\b')
        self.assertTrue(
            all(not s.get('uses', '').startswith('jdx/') for s in _steps(commit))
        )
        self.assertEqual(text.count('secrets.RENOVATE_LOCKS_'), 2)

    def test_renovate_lock_artifact_accepts_only_lock_files(self):
        script = ROOT / '.github/renovate-locks-accept.sh'
        allowed = (
            'source/idea/ideactl/package-lock.json',
            'source/idea/idea-cluster-manager/webapp/yarn.lock',
            'requirements/runtime.txt',
        )

        def accept(files, link=None):
            with tempfile.TemporaryDirectory() as temp:
                artifact, checkout = Path(temp, 'artifact'), Path(temp, 'checkout')
                (checkout / 'requirements').mkdir(parents=True)
                (checkout / 'requirements/runtime.in').write_text('marker\n')
                for path in files:
                    (artifact / path).parent.mkdir(parents=True, exist_ok=True)
                    (artifact / path).write_text('new\n')
                    (checkout / path).parent.mkdir(parents=True, exist_ok=True)
                if link:
                    (artifact / link).parent.mkdir(parents=True, exist_ok=True)
                    (artifact / link).symlink_to('/etc/hosts')
                artifact.mkdir(exist_ok=True)
                result = subprocess.run(
                    ['bash', str(script), str(checkout)],
                    cwd=artifact,
                    capture_output=True,
                    text=True,
                )
                written = sorted(
                    str(path.relative_to(checkout))
                    for path in checkout.rglob('*')
                    if path.is_file() and path.read_text() == 'new\n'
                )
                return result.returncode, written

        self.assertEqual(accept(allowed), (0, sorted(allowed)))
        for extra in (
            '.github/workflows/x.yaml',
            'requirements/other.txt',
            'requirements/a/b.txt',
            'package-lock.json',
        ):
            self.assertEqual(accept((*allowed, extra)), (1, []), extra)
        self.assertEqual(accept(allowed[:2], link='requirements/runtime.txt'), (1, []))
        self.assertEqual(accept(()), (1, []))

    DOCKER_STUB = r"""#!{python}
import json, os, sys
state_path = os.environ['DOCKER_STATE']
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write('docker ' + ' '.join(args) + '\n')

def find(ref):
    for name, digests in state.items():
        if ref == name or ref in digests:
            return name
    return None

if args[:2] == ['image', 'inspect']:
    name = find(args[-1])
    if name is None:
        sys.exit(1)
    if '--format' in args and 'RepoDigests' in args[args.index('--format') + 1]:
        print('\n'.join(state[name]))
    sys.exit(0)
if args[0] == 'run' and '--entrypoint' in args and args[args.index('--entrypoint') + 1] == 'cat':
    # answers only at the path the image really keeps (derived from the Dockerfile by the test)
    if args[-1] != os.environ['IMAGE_VERSION_FILE']:
        print(f'cat: {args[-1]}: No such file or directory', file=sys.stderr)
        sys.exit(1)
    versions = json.loads(os.environ.get('IMAGE_VERSIONS', '{}'))
    print(versions.get(args[-2], os.environ['IMAGE_VERSION']))
    sys.exit(0)
if args[0] == 'pull':
    if os.environ.get('PULL_FAILS'):
        sys.exit(1)
    state[args[1]] = [args[1]]
elif args[0] == 'tag':
    source = find(args[1])
    if source is None:
        sys.exit(1)
    state[args[2]] = list(state[source])
json.dump(state, open(state_path, 'w'))
sys.exit(0)
"""

    @staticmethod
    def _image_version_file():
        """Where the built image keeps its release, read from the Dockerfile and ideactl's build."""
        dockerfile = (ROOT / 'deployment/ecr/idea-control-plane/Dockerfile').read_text()
        final = dockerfile[dockerfile.rindex('\nFROM ') :]
        moved = re.search(r'mv /idea-build/source/idea/ideactl (\S+);', final)
        assert moved, 'the final stage moves the ideactl build into place'
        copier = (ROOT / 'source/idea/ideactl/scripts/copy-resources.mjs').read_text()
        return CiPipelineTests._version_file_from(final, moved[1], copier)

    @staticmethod
    def _version_file_from(final, ideactl_dir, copier):
        """The image path of the IDEA_VERSION.txt copy-resources.mjs writes, from its code."""
        # outputResources defaults to join(packageRoot, <segments>); an environment override,
        # whatever operator falls back from it, must not be set by the image build
        init = re.search(
            r'const\s+outputResources\s*=\s*resolve\(([\s\S]*?)\);', copier
        )
        assert init, 'copy-resources.mjs defines outputResources'
        default = re.search(
            r'join\(\s*packageRoot\s*,((?:\s*["\'][^"\']+["\']\s*,?)+)\s*\)', init[1]
        )
        assert default, 'outputResources has a default under the package'
        segments = re.findall(r'["\']([^"\']+)["\']', default[1])
        for name in re.findall(r'process\.env\.(\w+)', init[1]):
            assert name not in final, f'the image build sets {name}'
        # it writes IDEA_VERSION.txt next to outputResources, that is in its parent directory
        assert re.search(
            r'join\(\s*dirname\(\s*outputResources\s*\)\s*,\s*["\']IDEA_VERSION\.txt["\']\s*\)',
            copier,
        ), 'copy-resources.mjs writes IDEA_VERSION.txt beside its output resources'
        path = '/'.join([ideactl_dir, *segments[:-1], 'IDEA_VERSION.txt'])
        for removed in re.findall(r'rm -rf ([^;\\\n]+)', final):
            for target in removed.split():
                assert not path.startswith(target.rstrip('/') + '/'), (
                    f'{path} is removed by {target}'
                )
        return path

    def test_idea_admin_reads_the_release_where_the_image_keeps_it(self):
        script = (ROOT / 'idea-admin.sh').read_text()
        found = re.search(r'^IDEA_IMAGE_VERSION_FILE="([^"]+)"$', script, re.M)
        self.assertEqual(found[1], self._image_version_file())

    def _admin(
        self,
        env=None,
        local=None,
        record='default',
        stamp=None,
        pull_fails=False,
        status=200,
        versions=None,
    ):
        """Runs the real idea-admin.sh 'about' with stubbed docker, curl and dig."""
        repository = 'public.ecr.aws/s5o2b4m0/idea-control-plane'
        digest = 'sha256:' + 'ab' * 32
        if record == 'default':
            record = f'{{\n  "version": "1.0",\n  "image": "{repository}@{digest}",\n  "run_id": 1\n}}\n'
        with tempfile.TemporaryDirectory() as temp:
            bin_dir, log, state = (
                Path(temp, 'bin'),
                Path(temp, 'calls'),
                Path(temp, 'state.json'),
            )
            bin_dir.mkdir()
            state.write_text(json.dumps(local or {}))
            (bin_dir / 'docker').write_text(
                self.DOCKER_STUB.replace('{python}', sys.executable)
            )
            logged = f'#!/bin/bash\necho "$(basename "$0") $*" >> {log}\n'
            if record is None:
                curl = 'exit 7\n'
            else:
                curl = (
                    'while [[ $# -gt 0 ]]; do [[ "$1" == --output ]] && out=$2; shift; done\n'
                    f'cat > "$out" <<\'EOF\'\n{record}EOF\nprintf {status}\n'
                )
            (bin_dir / 'curl').write_text(logged + curl)
            (bin_dir / 'dig').write_text(logged)
            for name in ('docker', 'curl', 'dig'):
                (bin_dir / name).chmod(0o755)
            script = ROOT / 'idea-admin.sh'
            if stamp is not None:
                text = script.read_text()
                self.assertEqual(text.count('\nIDEA_IMAGE_DIGEST_DEFAULT=""\n'), 1)
                script = Path(temp, 'idea-admin.sh')
                script.write_text(
                    text.replace(
                        '\nIDEA_IMAGE_DIGEST_DEFAULT=""\n',
                        f'\nIDEA_IMAGE_DIGEST_DEFAULT="{stamp}"\n',
                    )
                )
            clean = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(('AWS_', 'IDEA_'))
            }
            revision = (env or {}).get('IDEA_REVISION') or re.search(
                r'^IDEA_REVISION=\$\{IDEA_REVISION:-"([^"]+)"\}',
                script.read_text(),
                re.M,
            )[1]
            result = subprocess.run(
                ['bash', str(script), 'about'],
                env={
                    **clean,
                    'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                    'HOME': temp,
                    'DOCKER_STATE': str(state),
                    'CALL_LOG': str(log),
                    'IMAGE_VERSION': revision[1:],
                    'IMAGE_VERSIONS': json.dumps(versions or {}),
                    'IMAGE_VERSION_FILE': self._image_version_file(),
                    **({'PULL_FAILS': '1'} if pull_fails else {}),
                    **(env or {}),
                },
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            calls = log.read_text().splitlines() if log.exists() else []
            final = json.loads(state.read_text())
        runs = [
            call
            for call in calls
            if call.startswith('docker run ') and '--entrypoint cat' not in call
        ]
        network = [
            call
            for call in calls
            if call.startswith(('curl ', 'dig ', 'docker pull', 'docker logout'))
        ]
        return result.returncode, runs, network, final, result.stdout + result.stderr

    def test_idea_admin_chooses_the_release_image_by_digest_and_needs_no_network_for_a_local_image(
        self,
    ):
        revision = re.search(
            r'^IDEA_REVISION=\$\{IDEA_REVISION:-"([^"]+)"\}',
            (ROOT / 'idea-admin.sh').read_text(),
            re.M,
        )[1]
        repository = 'public.ecr.aws/s5o2b4m0/idea-control-plane'
        digest, other = 'sha256:' + 'ab' * 32, 'sha256:' + 'cd' * 32
        tag = f'{repository}:{revision}'

        # nothing local: the record is fetched, the image pulled by digest and tagged for next time
        status, runs, network, final, _ = self._admin()
        self.assertEqual(status, 0)
        self.assertIn(f' {repository}@{digest} ideactl about', runs[0])
        self.assertTrue(
            any(
                f'/{revision}/candidate.json' in call
                for call in network
                if call.startswith('curl ')
            )
        )
        self.assertIn(f'docker pull {repository}@{digest}', network)
        self.assertEqual(final[tag], [f'{repository}@{digest}'])

        # image present locally (pulled earlier) and named by the record: no pull, no ECR reset
        status, runs, network, _, _ = self._admin(
            local={tag: [f'{repository}@{digest}']}
        )
        self.assertEqual(status, 0)
        self.assertEqual([c for c in network if not c.startswith('curl ')], [])
        self.assertIn(f' {repository}@{digest} ideactl about', runs[0])

        # C1: a host that ran an earlier candidate, through its release-attached script or the
        # digest variable, then runs an unstamped script: the release image runs, not the candidate
        candidate = 'sha256:' + 'ee' * 32
        for label, kwargs in (
            ('stamped candidate', {'stamp': candidate}),
            ('override variable', {'env': {'IDEA_IMAGE_DIGEST': candidate}}),
        ):
            status, runs, _, after, _ = self._admin(**kwargs)
            self.assertEqual(status, 0, label)
            self.assertIn(f' {repository}@{candidate} ideactl about', runs[0], label)
            self.assertNotIn(tag, after, f'{label}: no release tag is written for it')
            status, runs, network, _, _ = self._admin(local=after)
            self.assertEqual(status, 0, label)
            self.assertIn(f' {repository}@{digest} ideactl about', runs[0], label)
        # a release tag left on another image (an older script tagged by pull, or by hand) is
        # refused against the record, with the command that clears it
        status, runs, _, _, output = self._admin(
            local={tag: [f'{repository}@{candidate}']}
        )
        self.assertEqual((status, runs), (1, []))
        self.assertIn(f'docker image rm {tag}', output)
        self.assertIn('earlier candidate or another build', output)
        # no route to the record: the local copy runs, with a warning that it is unchecked
        status, runs, network, _, output = self._admin(
            local={tag: [f'{repository}@{candidate}']}, record=None
        )
        self.assertEqual(status, 0)
        self.assertIn(f' {repository}@{candidate} ideactl about', runs[0])
        self.assertIn('is used unchecked', output)
        self.assertFalse(any(c.startswith('docker pull') for c in network))

        # a same-named image built or loaded locally carries no registry digest and is not used
        status, runs, network, _, _ = self._admin(local={tag: []})
        self.assertEqual(status, 0)
        self.assertIn(f' {repository}@{digest} ideactl about', runs[0])
        self.assertTrue(any(call.startswith('curl ') for call in network))

        # digest given: no lookup; a local copy needs nothing else, a missing one is pulled by digest
        status, runs, network, _, _ = self._admin(
            {'IDEA_IMAGE_DIGEST': other},
            local={f'{repository}@{other}': [f'{repository}@{other}']},
        )
        self.assertEqual((status, network), (0, []))
        self.assertIn(f' {repository}@{other} ideactl about', runs[0])
        status, runs, network, _, _ = self._admin(
            {'IDEA_IMAGE_DIGEST': other}, local={tag: [f'{repository}@{digest}']}
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            [c for c in network if not c.startswith(('dig ', 'docker logout'))],
            [f'docker pull {repository}@{other}'],
        )

        # the idea-admin.sh attached to a release carries the digest: no lookup
        status, runs, network, _, _ = self._admin(stamp=other, record=None)
        self.assertEqual(status, 0)
        self.assertFalse(any(call.startswith('curl ') for call in network))
        self.assertIn(f' {repository}@{other} ideactl about', runs[0])
        # ...but only for its own revision
        status, runs, network, _, _ = self._admin(
            {'IDEA_REVISION': 'v26.11.1'}, stamp=other
        )
        self.assertEqual(status, 0)
        self.assertTrue(any('/v26.11.1/candidate.json' in call for call in network))

        # fetch fails and nothing local: stop, saying exactly what to set and where to find it
        for label, record in (
            ('unreachable', None),
            ('another repository', f'  "image": "registry.invalid/other@{digest}",\n'),
            ('no digest', f'  "image": "{repository}:{revision}",\n'),
        ):
            status, runs, _, _, output = self._admin(record=record)
            self.assertEqual((status, runs), (1, []), label)
            for needed in (
                'IDEA_IMAGE_DIGEST=sha256:',
                f'releases/tag/v{revision[1:]}',
                '"image" field of candidate.json',
                'IDEA_DOCKER_REPO',
            ):
                self.assertIn(needed, output, label)
        status, runs, _, _, _ = self._admin({'IDEA_IMAGE_DIGEST': 'sha256:short'})
        self.assertEqual((status, runs), (1, []))

        # mirror: the digest applies to the mirror; given, it needs no lookup
        mirror = 'mirror.invalid/idea'
        status, runs, network, final, _ = self._admin(
            {'IDEA_DOCKER_REPO': mirror, 'IDEA_IMAGE_DIGEST': other}
        )
        self.assertEqual(status, 0)
        self.assertIn(f' {mirror}@{other} ideactl about', runs[0])
        self.assertFalse(any(call.startswith('curl ') for call in network))
        self.assertNotIn(
            f'{mirror}:{revision}', final, 'a digest given is not tagged as the release'
        )
        status, runs, network, _, _ = self._admin({'IDEA_DOCKER_REPO': mirror})
        self.assertEqual(status, 0)
        self.assertIn(f' {mirror}@{digest} ideactl about', runs[0])
        status, runs, _, _, output = self._admin(
            {'IDEA_DOCKER_REPO': mirror, 'IDEA_IMAGE_DIGEST': other}, pull_fails=True
        )
        self.assertEqual((status, runs), (1, []))
        self.assertIn('same digest', output)

        # the record is JSON however it is laid out
        status, runs, _, _, _ = self._admin(
            record=f'{{"version":"x","image":"{repository}@{digest}","run_id":1}}'
            + '\n'
        )
        self.assertEqual(status, 0)
        self.assertIn(f' {repository}@{digest} ideactl about', runs[0])

        # the releases an operator may need back, the previous one and the upgrade floor, carry a
        # record (checked on the published v26.11.1 and v26.10.9); a release older than 26.10.4
        # published none and runs by the tag it always used
        for old in (
            'v26.10.3',
            'v26.09.5',
            'v26.08.0',
            'v25.09.1',
        ):  # 08, 09: not octal
            status, runs, network, final, output = self._admin(
                {'IDEA_REVISION': old}, record='Not Found\n', status=404
            )
            self.assertEqual(status, 0, f'{old}: {output}')
            self.assertIn(f'docker pull {repository}:{old}', network)
            self.assertIn(f' {repository}:{old} ideactl about', runs[0])
            self.assertIn('predates the release image record', output)
            self.assertNotIn('value too great', output)
        # ...but a release from 26.10.4 on without a record, or an unreachable record, stops
        for label, extra in (('missing record', {'IDEA_REVISION': 'v26.11.1'}),):
            status, runs, _, _, output = self._admin(
                extra, record='Not Found\n', status=404
            )
            self.assertEqual((status, runs), (1, []), label)
        status, runs, _, _, _ = self._admin({'IDEA_REVISION': 'v26.10.3'}, record=None)
        self.assertEqual(
            (status, runs), (1, []), 'an outage is not read as an old release'
        )

        # the image must be the release asked for: a digest left set from another release stops (U14)
        status, runs, _, _, output = self._admin(
            {'IDEA_REVISION': 'v26.11.1', 'IDEA_IMAGE_DIGEST': other},
            versions={f'{repository}@{other}': '26.11.2'},
        )
        self.assertEqual((status, runs), (1, []))
        self.assertIn("is release '26.11.2', not v26.11.1", output)
        self.assertIn('env -u IDEA_IMAGE_DIGEST', output)
        # a candidate revision runs its candidate image, which holds the release it would become
        status, runs, _, _, output = self._admin(
            {'IDEA_REVISION': 'v26.11.2-rc.3'},
            versions={f'{repository}@{digest}': '26.11.2'},
        )
        self.assertEqual(status, 0, output)
        self.assertIn(f' {repository}@{digest} ideactl about', runs[0])

        # a refused image is not tagged, so the next run without the variable chooses afresh
        wrong = {f'{repository}@{other}': '26.11.1'}
        status, runs, _, final, _ = self._admin(
            {'IDEA_IMAGE_DIGEST': other}, versions=wrong
        )
        self.assertEqual((status, runs), (1, []))
        self.assertNotIn(tag, final)
        status, runs, _, _, _ = self._admin(local=final, versions=wrong)
        self.assertEqual(status, 0)
        self.assertIn(f' {repository}@{digest} ideactl about', runs[0])
        # an older release's image left under this release's tag, used offline, is refused by
        # its version with the command that clears it
        status, runs, _, _, output = self._admin(
            local={tag: [f'{repository}@{other}']}, versions=wrong, record=None
        )
        self.assertEqual((status, runs), (1, []))
        self.assertIn(f'docker image rm {tag}', output)

        # a local build runs only when asked for
        status, runs, network, _, _ = self._admin(
            {'IDEA_LOCAL_IMAGE': 'true'}, local={f'idea-control-plane:{revision}': []}
        )
        self.assertEqual((status, network), (0, []))
        self.assertIn(f' idea-control-plane:{revision} ideactl about', runs[0])
        status, runs, _, _, _ = self._admin({'IDEA_LOCAL_IMAGE': 'true'})
        self.assertEqual((status, runs), (1, []))

    def test_private_parity_runs_test_code_without_network(self):
        script = ROOT / 'source/idea/ideactl/scripts/private-parity.sh'
        with tempfile.TemporaryDirectory() as temp:
            bin_dir, log = Path(temp, 'bin'), Path(temp, 'calls')
            bin_dir.mkdir()
            stubs = {
                'aws': 'exit 0',
                'node': 'exit 0',
                # the unpacker is stubbed; the summary filter runs on the real interpreter
                'python3': f'[[ "$1" == *unpack-private-parity.py ]] && exit 0\nexec {sys.executable} "$@"',
                'sudo': 'printf "# tests 1\\n# pass 1\\n"',
            }
            for name, body in stubs.items():
                (bin_dir / name).write_text(
                    f'#!/bin/bash\necho "$(basename "$0") $*" >> {log}\n{body}\n'
                )
                (bin_dir / name).chmod(0o755)
            result = subprocess.run(
                ['bash', str(script)],
                cwd=temp,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                    'RUNNER_TEMP': temp,
                    'IDEACTL_PARITY_SHA256': 'a' * 64,
                    'IDEACTL_PARITY_BUCKET': 'marker-bucket',
                    'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'oidc-request-marker',
                    'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://oidc.invalid/marker',
                },
            )
            calls = log.read_text().splitlines()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('# pass 1', result.stdout)
        self.assertFalse(
            [call for call in calls if call.startswith('node ')],
            'no test code runs with network access',
        )
        sudo = [call for call in calls if call.startswith('sudo ')]
        self.assertEqual(len(sudo), 1)
        self.assertTrue(
            sudo[0].startswith('sudo -n unshare --net -- bash -ec'), sudo[0]
        )
        self.assertIn('setpriv --reuid=', sudo[0])
        self.assertIn('env -i "${@:3}" node --test', sudo[0])
        # only the named variables cross into the namespace; the job's OIDC request token does not
        self.assertNotIn('marker', sudo[0].replace('marker-bucket', ''))
        self.assertIn('IDEACTL_REQUIRE_PRIVATE_CAPTURES=1', sudo[0])

    def test_no_dependency_code_runs_in_a_job_that_can_request_an_oidc_token(self):
        jobs = _workflow('ideactl_checks.yaml')['jobs']
        build, parity = jobs['private-parity-build'], jobs['private-parity']
        self.assertEqual(build['permissions'], {'contents': 'read'})
        self.assertEqual(parity['permissions'].get('id-token'), 'write')
        self.assertEqual(parity['needs'], 'private-parity-build')
        self.assertEqual(build['if'], parity['if'])
        for name, job in jobs.items():
            if (job.get('permissions') or {}).get('id-token') != 'write':
                continue
            for step in _steps(job):
                body = f'{step.get("uses", "")}\n{step.get("run", "")}'
                self.assertNotRegex(
                    body,
                    r'\b(npm|npx|yarn|pip3?|uv)\b|node (?!--test)|\brun build\b',
                    f'{name}: {step.get("name")}',
                )
                # a checkout runs no repository code; it must be this commit, without credentials
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertEqual(
                        step['with'],
                        {'ref': '${{ github.sha }}', 'persist-credentials': False},
                    )

    def test_the_parity_job_takes_only_build_outputs_and_runs_its_own_checkout(self):
        jobs = _workflow('ideactl_checks.yaml')['jobs']
        archive = next(
            s['run']
            for s in _steps(jobs['private-parity-build'])
            if 'tar -czf' in s.get('run', '')
        )
        outputs = [
            'source/idea/ideactl/node_modules',
            'source/idea/ideactl/dist',
            'source/idea/ideactl/.parity-oracles',
        ]
        self.assertEqual(re.findall(r'source/idea/ideactl/\S+', archive), outputs)
        self.assertNotRegex(
            archive, r'"\$GITHUB_WORKSPACE" \.\s*$', 'not the whole workspace'
        )
        steps = _steps(jobs['private-parity'])
        uses = [s.get('uses', '') for s in steps]
        checkout = next(
            i for i, u in enumerate(uses) if u.startswith('actions/checkout@')
        )
        unpack = next(
            i
            for i, s in enumerate(steps)
            if 'unpack-parity-build.sh' in s.get('run', '')
        )
        self.assertLess(checkout, unpack)
        script = ROOT / 'source/idea/ideactl/scripts/unpack-parity-build.sh'
        with tempfile.TemporaryDirectory() as temp:
            source, workspace = Path(temp, 'source'), Path(temp, 'workspace')
            for path in outputs:
                Path(source, path).mkdir(parents=True)
                Path(source, path, 'built.js').write_text('built\n')
            Path(workspace, 'source/idea/ideactl/scripts').mkdir(parents=True)
            reviewed = Path(workspace, 'source/idea/ideactl/scripts/private-parity.sh')
            reviewed.write_text('reviewed\n')
            good = Path(temp, 'good.tar.gz')
            subprocess.run(
                ['tar', '-czf', str(good), '-C', str(source), *outputs], check=True
            )
            result = subprocess.run(
                ['bash', str(script), str(good), str(workspace)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(
                Path(workspace, 'source/idea/ideactl/dist/built.js').exists()
            )
            # an archive that also carries a script the parity job runs is refused, nothing unpacked
            Path(source, 'source/idea/ideactl/scripts').mkdir(parents=True)
            Path(source, 'source/idea/ideactl/scripts/private-parity.sh').write_text(
                'replaced\n'
            )
            bad = Path(temp, 'bad.tar.gz')
            subprocess.run(
                [
                    'tar',
                    '-czf',
                    str(bad),
                    '-C',
                    str(source),
                    *outputs,
                    'source/idea/ideactl/scripts',
                ],
                check=True,
            )
            result = subprocess.run(
                ['bash', str(script), str(bad), str(workspace)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('outside its three output directories', result.stdout)
            self.assertEqual(reviewed.read_text(), 'reviewed\n')

    def test_workflows_default_to_read_only_and_pin_actions_by_commit(self):
        writers = {
            ('build_push.yaml', 'publish_release_candidate'),
            ('build_push.yaml', 'promote_release'),
            ('sync_docs_branch.yaml', 'sync'),
        }
        for path in sorted(WORKFLOWS.glob('*.yaml')):
            workflow = yaml.safe_load(path.read_text())
            self.assertEqual(workflow['permissions'], {'contents': 'read'}, path.name)
            for name, job in workflow['jobs'].items():
                granted = job.get('permissions') or {}
                if any(
                    value == 'write' and scope != 'id-token'
                    for scope, value in granted.items()
                ):
                    self.assertIn((path.name, name), writers)
        for path in [
            *WORKFLOWS.glob('*.yaml'),
            *ROOT.glob('.github/actions/*/action.yml'),
        ]:
            for action in re.findall(r'uses: ([^\s]+)', path.read_text()):
                if not action.startswith('./'):
                    self.assertRegex(
                        action, r'@[0-9a-f]{40}$', f'{path.name}: {action}'
                    )

    def test_runtime_check_runs_only_the_reviewed_nvm_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            bin_dir = Path(temp, 'bin')
            bin_dir.mkdir()
            marker = Path(temp, 'ran')
            (bin_dir / 'curl').write_text(
                f'#!/bin/bash\nwhile [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out=$2; shift; done\n'
                f'echo "touch {marker}" > "$out"\n'
            )
            (bin_dir / 'curl').chmod(0o755)
            result = subprocess.run(
                ['bash', str(ROOT / 'scripts/ci-runtime.sh')],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(
                marker.exists(), 'an installer that is not the reviewed file never runs'
            )

    IMAGE_ACTION = './.github/actions/build_control_plane_image'
    PUBLISH = re.compile(
        r'\b(skopeo|aws|gh release|gh api|docker push|docker login|imagetools create|git push)\b'
        r'|--push\b|type=registry|upload-artifact|configure-aws-credentials|login-action'
    )

    def _image_step(self, job):
        return [s for s in _steps(job) if s.get('uses') == self.IMAGE_ACTION]

    def test_pull_request_image_check_holds_no_credential_and_publishes_nothing(self):
        text = (WORKFLOWS / 'image_checks.yaml').read_text()
        workflow = yaml.safe_load(text)
        triggers = workflow.get(True, workflow.get('on'))
        self.assertIn('pull_request', triggers)
        self.assertNotIn(
            'pull_request_target',
            triggers,
            'pull_request_target would hand forks secrets',
        )
        self.assertNotIn('workflow_run', triggers)
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        for needle in (
            'secrets.',
            'id-token',
            'github.token',
            'GITHUB_TOKEN',
            'environment:',
        ):
            self.assertNotIn(needle, text, needle)
        for name, job in workflow['jobs'].items():
            self.assertIn(job.get('permissions'), (None, {'contents': 'read'}), name)
            self.assertNotIn('environment', job, name)
            for step in _steps(job):
                body = f'{step.get("uses", "")}\n{step.get("run", "")}'
                self.assertNotRegex(body, self.PUBLISH, f'{name}: {step.get("name")}')
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertIs(step['with']['persist-credentials'], False, name)
        # the shared action is part of the pull request job, so it is held to the same rule
        action = (
            ROOT / '.github/actions/build_control_plane_image/action.yml'
        ).read_text()
        for needle in ('secrets.', 'id-token', 'github.token', 'GH_TOKEN'):
            self.assertNotIn(needle, action, needle)
        for step in yaml.safe_load(action)['runs']['steps']:
            body = f'{step.get("uses", "")}\n{step.get("run", "")}'
            self.assertNotRegex(body, self.PUBLISH, step.get('name'))
        self.assertNotRegex(
            (ROOT / 'scripts/ci-scan-image.sh').read_text(), self.PUBLISH
        )

    def test_release_and_pull_request_build_and_scan_through_one_action(self):
        release = _workflow('build_push.yaml')['jobs']['build_image']
        check = _workflow('image_checks.yaml')['jobs']['control_plane_image']
        self.assertEqual(release['runs-on'], check['runs-on'])
        (release_image,), (check_image,) = (
            self._image_step(release),
            self._image_step(check),
        )
        self.assertEqual(release_image['with'], {'cache-write': 'true'})
        self.assertEqual(
            check_image['with'], {'cache-write': "${{ github.event_name == 'push' }}"}
        )

        def before_image(job):
            steps = _steps(job)
            prefix = steps[: steps.index(self._image_step(job)[0])]
            return [s['uses'] for s in prefix if s.get('uses', '').startswith('./')]

        self.assertEqual(before_image(release), before_image(check))
        # nothing else builds, smokes or scans the image, so the two cannot drift apart
        sources = [
            *WORKFLOWS.glob('*.yaml'),
            *ROOT.glob('.github/actions/*/action.yml'),
            *ROOT.glob('.github/*.hcl'),
        ]
        for path in sources:
            if path.parent.name == 'build_control_plane_image':
                continue
            body = path.read_text()
            for needle in (
                'trivy',
                'ci-scan-image',
                'buildx build',
                'bake-action',
                'idea-control-plane/Dockerfile',
                'ci-stage-images',
            ):
                self.assertNotIn(needle, body, f'{path.name}: {needle}')
        self.assertEqual(
            sorted(
                p.name for p in ROOT.glob('scripts/*.sh') if 'trivy' in p.read_text()
            ),
            ['ci-scan-image.sh'],
        )

    def test_pull_request_image_check_skips_unchanged_images_and_cancels_superseded_runs(
        self,
    ):
        workflow = _workflow('image_checks.yaml')
        self.assertEqual(
            workflow['concurrency'],
            {
                'group': 'image-checks-${{ github.event.pull_request.number || github.ref }}',
                'cancel-in-progress': "${{ github.event_name == 'pull_request' }}",
            },
        )
        job = workflow['jobs']['control_plane_image']
        self.assertEqual(job['needs'], 'changes')
        self.assertEqual(job['if'], "needs.changes.outputs.images == 'true'")
        detect = workflow['jobs']['changes']['steps'][-1]
        self.assertEqual(detect['run'], 'bash scripts/ci-inputs.sh')
        for changed, expected in (
            (['docs/a.md', 'docs/b/c.png'], 'false'),
            (['docs/a.md', 'requirements/runtime.txt'], 'true'),
        ):
            with tempfile.TemporaryDirectory() as temp:
                subprocess.run(['git', 'init', '-q', temp], check=True)
                git = [
                    'git',
                    '-C',
                    temp,
                    '-c',
                    'user.name=t',
                    '-c',
                    'user.email=t@t',
                    '-c',
                    'commit.gpgsign=false',
                ]
                Path(temp, 'base').write_text('x')
                subprocess.run([*git, 'add', '-A'], check=True)
                subprocess.run([*git, 'commit', '-qm', 'base'], check=True)
                base = subprocess.run(
                    [*git, 'rev-parse', 'HEAD'],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                for path in changed:
                    Path(temp, path).parent.mkdir(parents=True, exist_ok=True)
                    Path(temp, path).write_text('y')
                subprocess.run([*git, 'add', '-A'], check=True)
                subprocess.run([*git, 'commit', '-qm', 'change'], check=True)
                out = Path(temp, 'out')
                subprocess.run(
                    ['bash', str(ROOT / 'scripts/ci-inputs.sh')],
                    cwd=temp,
                    check=True,
                    env={
                        **os.environ,
                        'GITHUB_EVENT_NAME': 'pull_request',
                        'PR_BASE_SHA': base,
                        'RUNNER_TEMP': temp,
                        'GITHUB_OUTPUT': str(out),
                    },
                )
                self.assertIn(f'images={expected}\n', out.read_text(), changed)

    def test_scan_tells_an_unavailable_scanner_apart_from_findings(self):
        script = ROOT / 'scripts/ci-scan-image.sh'

        def scan(trivy_db_failures=0, scan_status=0, curl_fails=False, wrong_sum=False):
            with tempfile.TemporaryDirectory() as temp:
                bin_dir, log, package = (
                    Path(temp, 'bin'),
                    Path(temp, 'calls'),
                    Path(temp, 'pkg'),
                )
                bin_dir.mkdir()
                package.mkdir()
                counter = Path(temp, 'db-failures')
                counter.write_text(str(trivy_db_failures))
                (package / 'trivy').write_text(
                    '#!/bin/bash\n'
                    f'echo "trivy $*" >> {log}\n'
                    'if [[ "$*" == *--download-db-only* ]]; then\n'
                    f'  left=$(cat {counter}); if [ "$left" -gt 0 ]; then echo $((left - 1)) > {counter}; exit 1; fi; exit 0\n'
                    'fi\n'
                    '[[ "$*" == *--download-java-db-only* ]] && exit 0\n'
                    f'exit {scan_status}\n'
                )
                (package / 'trivy').chmod(0o755)
                tarball = Path(temp, 'trivy.tar.gz')
                subprocess.run(
                    ['tar', '-czf', str(tarball), '-C', str(package), 'trivy'],
                    check=True,
                )
                digest = subprocess.run(
                    ['shasum', '-a', '256', str(tarball)],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.split()[0]
                (bin_dir / 'curl').write_text(
                    '#!/bin/bash\n'
                    + (
                        'exit 22\n'
                        if curl_fails
                        else 'while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out=$2; shift; done\n'
                        f'cp {tarball} "$out"\n'
                    )
                )
                (bin_dir / 'curl').chmod(0o755)
                result = subprocess.run(
                    ['bash', str(script), 'image:test'],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                        'RUNNER_TEMP': temp,
                        'SCANNER_VERSION': '0.0.0',
                        'SCANNER_SHA256': '0' * 64 if wrong_sum else digest,
                        'SCANNER_RETRY_DELAY': '0',
                    },
                )
                calls = log.read_text().splitlines() if log.exists() else []
            scans = [c for c in calls if 'download' not in c]
            downloads = [c for c in calls if '--download-db-only' in c]
            return result.returncode, result.stdout + result.stderr, downloads, scans

        status, output, downloads, scans = scan()
        self.assertEqual(status, 0, output)
        self.assertEqual(len(scans), 1)
        for flag in (
            '--exit-code 10',
            '--skip-db-update',
            '--skip-java-db-update',
            '--ignorefile .github/image-scan-allowlist',
            '--severity HIGH,CRITICAL',
            '--ignore-unfixed',
            'image:test',
        ):
            self.assertIn(flag, scans[0])
        status, output, downloads, scans = scan(trivy_db_failures=2)
        self.assertEqual(
            (status, len(downloads), len(scans)),
            (0, 3, 1),
            'two failed downloads are retried',
        )
        status, output, downloads, scans = scan(trivy_db_failures=3)
        self.assertEqual(
            (status, len(downloads), scans),
            (2, 3, []),
            'retries are bounded and nothing is scanned',
        )
        self.assertIn('Scanner unavailable, image not scanned', output)
        self.assertIn('not a vulnerability finding', output)
        self.assertNotIn('Vulnerabilities found', output)
        status, output, _, scans = scan(scan_status=10)
        self.assertEqual(status, 1)
        self.assertIn('Vulnerabilities found', output)
        self.assertNotIn('not scanned', output)
        status, output, _, _ = scan(scan_status=1)
        self.assertEqual(status, 2)
        self.assertIn('Scanner failed, image not scanned', output)
        status, output, _, scans = scan(curl_fails=True)
        self.assertEqual((status, scans), (2, []))
        self.assertIn('Scanner unavailable', output)
        status, output, downloads, scans = scan(wrong_sum=True)
        self.assertEqual((status, downloads, scans), (2, [], []))
        self.assertIn('Scanner checksum mismatch', output)

    @staticmethod
    def _allowlist_problems(lines, today):
        """Every accepted line: an identifier, an expiry at most 90 days out, and a reason above it."""
        import datetime

        problems = []
        for index, line in enumerate(lines):
            if not line.strip() or line.startswith('#'):
                continue
            match = re.fullmatch(
                r'(CVE-\d{4}-\d+|GHSA-[\w-]+|[A-Z]+-[\w.-]+) exp:(\d{4}-\d{2}-\d{2})',
                line,
            )
            if not match:
                problems.append(f'line {index + 1}: not "<id> exp:YYYY-MM-DD"')
                continue
            if not (index > 0 and re.match(r'# \S.*: \S', lines[index - 1])):
                problems.append(f'line {index + 1}: no reason above it')
            if (datetime.date.fromisoformat(match[2]) - today).days > 90:
                problems.append(f'line {index + 1}: expiry more than 90 days out')
        return problems

    def test_every_accepted_finding_has_a_reason_and_a_near_expiry(self):
        import datetime

        today = datetime.date(2026, 11, 18)
        lines = (ROOT / '.github/image-scan-allowlist').read_text().splitlines()
        self.assertEqual(self._allowlist_problems(lines, datetime.date.today()), [])
        # the rule itself, on lines the empty list cannot show
        good = [
            '# openssl 3.2.2: no AL2023 fix yet; removed when the base image takes it',
            'CVE-2026-12345 exp:2027-02-01',
        ]
        self.assertEqual(self._allowlist_problems(good, today), [])
        for label, lines, problem in (
            ('no reason', ['CVE-2026-12345 exp:2027-02-01'], 'no reason above it'),
            (
                'no expiry',
                ['# why: reason', 'CVE-2026-12345'],
                'not "<id> exp:YYYY-MM-DD"',
            ),
            (
                'far expiry',
                ['# why: reason', 'CVE-2026-12345 exp:2027-06-01'],
                'more than 90 days out',
            ),
        ):
            found = self._allowlist_problems(lines, today)
            self.assertEqual(len(found), 1, label)
            self.assertIn(problem, found[0], label)

    def test_the_shared_action_scans_and_checks_the_image_in_blocking_steps(self):
        steps = yaml.safe_load(
            (ROOT / '.github/actions/build_control_plane_image/action.yml').read_text()
        )['runs']['steps']
        names = [step.get('name') for step in steps]
        build, scan = (
            names.index('Build and test the image'),
            names.index('Scan the image for known vulnerabilities'),
        )
        self.assertLess(build, scan)
        scans = [step for step in steps if 'ci-scan-image.sh' in step.get('run', '')]
        self.assertEqual(scans, [steps[scan]])
        self.assertEqual(
            steps[scan]['run'].splitlines()[0], 'bash scripts/ci-scan-image.sh "$IMAGE"'
        )
        for step in steps:
            self.assertNotIn('continue-on-error', step, step.get('name'))
            self.assertNotRegex(
                step.get('run', ''),
                r'ci-scan-image\.sh[^\n]*\|\||image_check\.sh[^\n]*\|\|',
            )
        # the containers branch's in-image check, as root with no network, before the scan; the
        # step names the script by the path it has once both branches are merged
        body = steps[build]['run']
        self.assertIn('test -f deployment/ecr/idea-control-plane/image_check.sh', body)
        check = re.search(
            r'docker run --rm --network none --user 0 --entrypoint /bin/bash \\\n\s+-v "\$PWD/deployment/ecr/idea-control-plane/image_check\.sh:/tmp/image_check\.sh:ro" \\\n\s+"\$IMAGE" /tmp/image_check\.sh',
            body,
        )
        self.assertTrue(check, 'the image check runs as its header documents')
        self.assertLess(
            body.index('RootFS.Layers'), check.start(), 'it checks the archived image'
        )

    TRIVY_REFERENCE = (
        'https://raw.githubusercontent.com/aquasecurity/trivy/3f6334d223b52188324af3b9923b4249cd206652/'
        'docs/guide/references/configuration/cli/trivy_image.md'
    )  # tag v0.76.2; sha256 5b69ac231bc6d25e6a7385d2d851e46fad8673ae5e84cc8c037a8193f6ba0d67 before tabs and the final newline were normalised

    def test_every_scanner_flag_is_documented_for_the_pinned_version(self):
        action = (
            ROOT / '.github/actions/build_control_plane_image/action.yml'
        ).read_text()
        version = re.search(r'SCANNER_VERSION: (\S+)', action)[1]
        reference = (
            ROOT / f'scripts/fixtures/trivy-{version}-image-cli.md'
        ).read_text()
        documented = set(re.findall(r'^\s+(?:-\w, )?(--[a-z0-9-]+)', reference, re.M))
        self.assertGreater(
            len(documented),
            50,
            'the reference lists the image command and global flags',
        )
        script = (ROOT / 'scripts/ci-scan-image.sh').read_text()
        invocations = re.findall(
            r'"\$SCANNER/trivy" image((?:[^\n]*\\\n)*[^\n]*)', script
        )
        self.assertEqual(len(invocations), 3, 'two database downloads and the scan')
        used = {
            flag for call in invocations for flag in re.findall(r'(--[a-z0-9-]+)', call)
        }
        self.assertGreaterEqual(
            used,
            {
                '--download-db-only',
                '--download-java-db-only',
                '--skip-db-update',
                '--timeout',
                '--exit-code',
            },
        )
        self.assertEqual(
            sorted(used - documented), [], f'flags not in trivy {version} image --help'
        )


if __name__ == '__main__':
    unittest.main()
