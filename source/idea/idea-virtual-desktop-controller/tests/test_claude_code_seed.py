"""
Exercises the seed script that actually ships, extracted from the rendered template. Home directories
roam between desktops, so the interesting cases are the ones where the file already exists.
"""

import json
import os
import re
import subprocess
import tempfile

import jinja2
import pytest

TEMPLATE = 'source/idea/idea-bootstrap/_templates/linux/claude_code_settings.jinja2'
SETTINGS = (
    'source/idea/idea-administrator/resources/config/templates/'
    'virtual-desktop-controller/settings.yml'
)


def render(root, permission_mode='auto'):
    class Ctx:
        class vars:
            bedrock_env = {'CLAUDE_CODE_USE_BEDROCK': '1'}
            auto_mode_environment = ['$defaults', 'Organization: the test cluster']
            claude_code_output_style = 'Concise'
            claude_code_permission_mode = permission_mode

    return (
        jinja2.Environment()
        .from_string((root / TEMPLATE).read_text())
        .render(context=Ctx)
    )


def seeded_defaults(rendered):
    return json.loads(
        re.search(
            r"<<'IDEA_CLAUDE_DEFAULTS'\n(.*?)\nIDEA_CLAUDE_DEFAULTS", rendered, re.S
        ).group(1)
    )


@pytest.fixture(scope='module')
def template_root(pytestconfig):
    root = pytestconfig.rootpath
    for _ in range(6):
        if (root / TEMPLATE).exists():
            break
        root = root.parent
    return root


@pytest.fixture(scope='module')
def seeder(template_root):
    rendered = render(template_root)
    defaults = seeded_defaults(rendered)
    seed_src = re.search(
        r"<<'IDEA_CLAUDE_SEED'\n(.*?)\nIDEA_CLAUDE_SEED", rendered, re.S
    ).group(1)

    work = tempfile.mkdtemp()
    defaults_path = os.path.join(work, 'defaults.json')
    with open(defaults_path, 'w') as handle:
        json.dump(defaults, handle)
    seed_path = os.path.join(work, 'seed.py')
    with open(seed_path, 'w') as handle:
        handle.write(
            seed_src.replace(
                "'/etc/idea/claude-code-defaults.json'", repr(defaults_path)
            )
        )

    def run(home, check=True):
        subprocess.run(
            ['python3', seed_path], env={**os.environ, 'HOME': home}, check=check
        )
        path = os.path.join(home, '.claude', 'settings.json')
        return json.load(open(path)) if os.path.exists(path) else None

    def home(settings=None):
        created = tempfile.mkdtemp(dir=work)
        if settings is not None:
            os.makedirs(os.path.join(created, '.claude'), exist_ok=True)
            with open(os.path.join(created, '.claude', 'settings.json'), 'w') as handle:
                json.dump(settings, handle)
        return created

    return defaults, run, home


def test_auto_mode_is_the_shipped_default(seeder):
    defaults, _run, _home = seeder
    assert defaults['permissions']['defaultMode'] == 'auto'
    assert defaults['outputStyle'] == 'Concise'


def test_an_empty_home_gets_every_default(seeder):
    _defaults, run, home = seeder
    got = run(home())
    assert got['outputStyle'] == 'Concise'
    assert got['permissions']['defaultMode'] == 'auto'
    assert got['autoMode']['environment'][0] == '$defaults'


def test_a_roaming_home_that_already_has_settings_still_gets_them(seeder):
    """the case that was broken: a shared home means the file already exists on a new desktop"""
    _defaults, run, home = seeder
    got = run(home({'theme': 'dark'}))
    assert got['theme'] == 'dark'
    assert got['permissions']['defaultMode'] == 'auto'


def test_an_existing_permissions_block_still_gains_the_mode(seeder):
    """merging whole top-level keys would skip defaultMode for anyone with their own allow rules"""
    _defaults, run, home = seeder
    got = run(home({'permissions': {'allow': ['Bash(ls *)']}}))
    assert got['permissions']['allow'] == ['Bash(ls *)']
    assert got['permissions']['defaultMode'] == 'auto'


def test_a_user_choice_is_never_overwritten(seeder):
    _defaults, run, home = seeder
    got = run(home({'outputStyle': 'Explanatory'}))
    assert got['outputStyle'] == 'Explanatory'


def test_a_deleted_key_stays_deleted(seeder):
    _defaults, run, home = seeder
    created = home()
    run(created)
    path = os.path.join(created, '.claude', 'settings.json')
    settings = json.load(open(path))
    del settings['outputStyle']
    with open(path, 'w') as handle:
        json.dump(settings, handle)

    got = run(created)

    assert 'outputStyle' not in got
    assert got['permissions']['defaultMode'] == 'auto'


def test_seeding_is_idempotent(seeder):
    _defaults, run, home = seeder
    created = home()
    first = run(created)
    assert run(created) == first


def test_the_cluster_default_permission_mode_is_auto(template_root):
    """what a desktop gets when nobody changes the setting"""
    settings = (template_root / SETTINGS).read_text()
    assert re.search(r'^\s+permission_mode: auto$', settings, re.M)


def test_the_seeded_permission_mode_is_the_one_the_cluster_configured(template_root):
    defaults = seeded_defaults(render(template_root, 'plan'))
    assert defaults['permissions']['defaultMode'] == 'plan'


def test_an_empty_permission_mode_seeds_no_permissions_block(template_root):
    """a cluster that offers no mode leaves the client on the one it ships with"""
    defaults = seeded_defaults(render(template_root, ''))
    assert 'permissions' not in defaults
    assert defaults['outputStyle'] == 'Concise'


def test_a_settings_write_that_fails_changes_nothing(seeder):
    """
    the marker records what has been offered: written ahead of the settings it describes,
    it would suppress the keys the user never got.
    """
    _defaults, run, home = seeder
    created = home({'theme': 'dark'})
    # the temp file the seeder writes through cannot be created over a directory
    os.mkdir(os.path.join(created, '.claude', 'settings.json.tmp'))

    run(created, check=False)

    settings_path = os.path.join(created, '.claude', 'settings.json')
    assert json.load(open(settings_path)) == {'theme': 'dark'}
    assert not os.path.exists(os.path.join(created, '.claude', '.idea-seeded'))
