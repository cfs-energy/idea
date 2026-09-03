"""
App code must never import cli modules. prettytable is a scheduler runtime dependency (the
OpenPBS invocation context uses it), so only the module boundary is asserted here, plus an
import smoke test of the image service.
"""

import pathlib
import re
import subprocess
import sys

import ideascheduler

APP_DIR = pathlib.Path(ideascheduler.__file__).parent / 'app'
CLI_IMPORT = re.compile(r'^\s*(from|import)\s+ideascheduler\.cli\b', re.M)


def test_no_app_module_imports_cli_code():
    offenders = []
    for path in sorted(APP_DIR.rglob('*.py')):
        if CLI_IMPORT.search(path.read_text()):
            offenders.append(str(path.relative_to(APP_DIR)))
    assert offenders == [], f'app modules importing cli code: {offenders}'


def test_admin_api_and_image_service_import_without_click():
    script = (
        'import sys\n'
        'sys.modules["click"] = None\n'
        'import ideascheduler.app.images.compute_images\n'
        'import ideascheduler.app.images.compute_node_ami_builder\n'
        'print("ok")\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
