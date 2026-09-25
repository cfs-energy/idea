"""
App code must not import cli modules. The controller cli imports prettytable, so its
entry point is also smoke-tested with the controller dependencies.
"""

import pathlib
import re
import subprocess
import sys

import ideavirtualdesktopcontroller

APP_DIR = pathlib.Path(ideavirtualdesktopcontroller.__file__).parent / 'app'
CLI_IMPORT = re.compile(
    r'^\s*(from|import)\s+ideavirtualdesktopcontroller\.cli\b', re.M
)
CLI_ONLY_PACKAGE = re.compile(r'^\s*(from|import)\s+prettytable\b', re.M)


def test_no_app_module_imports_cli_code_or_cli_only_packages():
    offenders = []
    for path in sorted(APP_DIR.rglob('*.py')):
        text = path.read_text()
        if CLI_IMPORT.search(text) or CLI_ONLY_PACKAGE.search(text):
            offenders.append(str(path.relative_to(APP_DIR)))
    assert offenders == [], f'app modules importing cli code: {offenders}'


def test_admin_api_and_image_services_import_without_prettytable():
    script = (
        'import sys\n'
        'sys.modules["prettytable"] = None\n'
        'import ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api\n'
        'import ideavirtualdesktopcontroller.app.software_stacks.desktop_images\n'
        'import ideavirtualdesktopcontroller.app.software_stacks.dcv_host_image_builder\n'
        'import ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_utils\n'
        'print("ok")\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


def test_cli_main_imports_with_controller_dependencies():
    requirements = pathlib.Path(__file__).resolve().parents[4] / 'requirements'
    assert (
        'prettytable'
        in (requirements / 'idea-virtual-desktop-controller.in')
        .read_text()
        .splitlines()
    )
    assert re.search(
        r'^prettytable==\d+\.\d+\.\d+$',
        (requirements / 'idea-virtual-desktop-controller.txt').read_text(),
        re.M,
    )
    script = 'import ideavirtualdesktopcontroller.cli.cli_main\nprint("ok")\n'
    result = subprocess.run(
        [sys.executable, '-c', script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
