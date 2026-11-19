"""
Sign-off findings on the Linux bootstrap, run through the rendered templates: the stored OpenPBS
reference against a real repository, and job values queued before an upgrade as shell data.
"""

import os
import shutil
import subprocess

import pytest

from test_bootstrap_downloads_executed import FAILURE
from test_bootstrap_secrets_and_downloads import LOG_STUBS, bash
from test_bootstrap_shell_syntax import BASH, Config, job
from test_image_bake_bootstrap import render

pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which('git') is None, reason='needs bash and git'
)


def git(cwd, *args):
    return subprocess.run(
        [
            'git',
            '-c',
            'user.name=test',
            '-c',
            'user.email=test@example.invalid',
            '-c',
            'commit.gpgsign=false',
            '-c',
            'tag.gpgsign=false',
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture()
def openpbs_repo(tmp_path):
    """A local repository with a default branch, a second branch and a tag on separate commits."""
    repo = tmp_path / 'upstream'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'master')
    (repo / 'README').write_text('one\n')
    git(repo, 'add', 'README')
    git(repo, 'commit', '-q', '-m', 'one')
    tagged = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'tag', 'v23.06.06')
    git(repo, 'checkout', '-q', '-b', 'el10-fixes')
    (repo / 'README').write_text('two\n')
    git(repo, 'commit', '-q', '-am', 'two')
    branch = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '-q', 'master')
    return repo, {'tag': tagged, 'branch': branch}


def openpbs_build(tmp_path, repo, stored):
    config = Config()
    config.put('global-settings.package_config.openpbs.commit', stored)
    config.put('global-settings.package_config.openpbs.repo_url', str(repo))
    text = render('_templates/linux/openpbs.jinja2', 'rocky9', config=config)
    start = text.index('function install_openpbs_git() {')
    body = text[start : text.index('\n}\n', start) + 3].replace(
        '/root/bootstrap', str(tmp_path / 'work')
    )
    stubs = """
check_openpbs_git_installed() { return 1; }
install_openpbs_dependencies() { :; }
compile_and_install_openpbs() { echo "BUILT $(git -C "$2" rev-parse HEAD)"; }
mkdir() { command mkdir "$@"; }
"""
    return bash(
        LOG_STUBS
        + FAILURE
        + stubs
        + body.replace('/opt/pbs/.idea_openpbs_commit', str(tmp_path / 'marker'))
        + 'install_openpbs_git\n'
    )


@pytest.mark.parametrize('form', ['full', 'short', 'upper', 'branch', 'tag', 'unknown'])
def test_openpbs_builds_each_stored_reference_form(tmp_path, openpbs_repo, form):
    repo, commits = openpbs_repo
    stored, expected, pinned = {
        'full': (commits['tag'], commits['tag'], True),
        'short': (commits['tag'][:10], commits['tag'], True),
        'upper': (commits['tag'].upper(), commits['tag'], True),
        # a branch other than the repository default exists after the clone only as origin/<name>
        'branch': ('el10-fixes', commits['branch'], False),
        'tag': ('v23.06.06', commits['tag'], False),
        'unknown': ('no-such-ref', None, None),
    }[form]
    result = openpbs_build(tmp_path, repo, stored)
    if expected is None:
        assert result.returncode == 1 and 'BUILT' not in result.stdout
        assert (
            'FAILED download-unverified: OpenPBS checkout of no-such-ref (commit not found) failed'
            in result.stdout
        )
        return
    assert f'BUILT {expected}' in result.stdout, result.stdout + result.stderr
    # a name that can move builds, and says which setting pins it
    warned = (
        'global-settings.package_config.openpbs.commit is' in result.stdout
        and 'a name that can move' in result.stdout
    )
    assert warned == (not pinned)
    if not pinned:
        assert f'building commit {expected}' in result.stdout


# U03: values from a job queued before the upgrade reach the node's root shell text only as data.
ODD = 'grp \'q\' "d" $(touch MARKER) `touch MARKER` $HOME\nnext'


def odd_job():
    return job().copy(update={'job_uid': ODD, 'job_group': ODD, 'owner': 'sample-user'})


def test_job_directory_reaches_the_environment_as_data(tmp_path):
    text = render(
        'compute-node/setup.sh.jinja2',
        'rhel9',
        variables={'job': odd_job(), 'job_directory': f'/apps/jobs/1-{ODD}'},
    )
    start = text.index("printf 'IDEA_JOB_UID=%q")
    end = text.index(
        '>> /etc/environment',
        text.index("printf 'IDEA_COMPUTE_NODE_LOGS_DIR=%q", start),
    )
    block = text[start : end + len('>> /etc/environment')]
    environment = tmp_path / 'environment'
    result = bash(
        'set -x\nhostname() { echo node-1; }\n'
        + block.replace('/etc/environment', str(environment))
        + f'\nset -a; source {environment}; set +a\nprintf "%s|%s|%s" "$IDEA_JOB_UID" "$IDEA_JOB_GROUP" "$IDEA_COMPUTE_NODE_LOGS_DIR"',
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'{ODD}|{ODD}|/apps/jobs/1-{ODD}/logs/node-1'
    assert not (tmp_path / 'MARKER').exists()
    # nothing else in the double-quoted environment block names the job directory
    assert (
        '{{' not in text and f'1-{ODD}' not in text.split("printf 'IDEA_JOB_UID=%q")[0]
    )


@pytest.mark.parametrize(
    'template',
    [
        'compute-node/compute_node.sh.jinja2',
        'compute-node/compute_node_post_reboot.sh.jinja2',
    ],
)
def test_reboot_entry_carries_no_job_value(template):
    text = render(
        template,
        'rhel9',
        variables={
            'job': odd_job(),
            'job_directory': f'/apps/jobs/1-{ODD}',
            'queue_profile': 'compute',
            'project': 'p',
            'motd_messages': [],
        },
    )
    lines = [row for row in text.splitlines() if '@reboot' in row]
    assert lines and all(
        'IDEA_COMPUTE_NODE_LOGS_DIR' not in row and ODD not in row for row in lines
    )


def test_post_reboot_started_by_cron_logs_to_the_job_directory(tmp_path):
    text = render(
        'compute-node/compute_node_post_reboot.sh.jinja2',
        'rhel9',
        variables={'job': job(), 'job_directory': '/apps/jobs/1'},
    )
    start = text.index('# started by cron after a reboot')
    block = text[start : text.index('\nfi\n', start) + 4]
    logs = tmp_path / f'logs {ODD.splitlines()[0]}'
    logs.mkdir()
    result = bash(
        'set -- crontab\n' + block + 'echo LOGGED\n',
        cwd=tmp_path,
        env={**os.environ, 'IDEA_COMPUTE_NODE_LOGS_DIR': str(logs)},
    )
    assert result.returncode == 0, result.stderr
    assert (logs / 'compute_node_bootstrap.log').read_text() == 'LOGGED\n'
    assert not (tmp_path / 'MARKER').exists()


def test_motd_writes_its_messages_as_data(tmp_path):
    from jinja2 import Environment, FileSystemLoader
    from ideasdk.context import BootstrapContext
    from test_bootstrap_shell_syntax import IDEA_BOOTSTRAP_DIR

    context = BootstrapContext(
        config=Config(),
        module_name='scheduler',
        module_id='scheduler',
        module_set='default',
        base_os='rhel9',
        instance_type='m7i.large',
    )
    # the shared-capacity compute node puts the job group in its message
    block = (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template('_templates/linux/motd.jinja2')
        .render(
            context=context,
            messages=[f'Compute Node, Queue: normal, JobGroup: {ODD}, Cluster: idea'],
        )
    )
    motd = tmp_path / 'motd'
    result = bash(block.replace('/etc/motd', str(motd)), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'MARKER').exists()
    assert f'JobGroup: {ODD}, Cluster' in motd.read_text()


@pytest.mark.parametrize('substitution', [True, False])
def test_user_data_passes_the_package_location_as_data(tmp_path, substitution):
    from ideasdk.bootstrap import BootstrapUserDataBuilder

    uri = f's3://bucket/idea/scheduler/bootstrap/1-{ODD}.tar.gz'
    text = (
        BootstrapUserDataBuilder(
            base_os='rhel9',
            aws_region='us-east-1',
            bootstrap_package_uri=uri,
            install_commands=['true'],
            substitution_support=substitution,
        )
        .build()
        .replace('${!', '${')
    )
    line = next(
        row
        for row in text.split('\nbash /root/bootstrap/download_bootstrap.sh ', 1)[1:]
    ).split(' || fail_userdata')[0]
    result = bash(f'printf "%s" {line}', cwd=tmp_path)
    assert result.stdout == uri
    assert not (tmp_path / 'MARKER').exists()
