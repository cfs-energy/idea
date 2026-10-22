// Use a stub node executable to inspect ideactl arguments.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

// this file's directory -> <repo>
const repoRoot = join(fileURLToPath(import.meta.url), '../../../../../..');
const entrypoint = join(repoRoot, 'deployment/ecr/idea-control-plane/entrypoint.sh');
const stubDir = mkdtempSync(join(tmpdir(), 'ideactl-entrypoint-'));
writeFileSync(join(stubDir, 'node'), '#!/bin/bash\necho "node $*"\n');
chmodSync(join(stubDir, 'node'), 0o755);

function run(args: string[], role?: string): { status: number; out: string } {
  try {
    const out = execFileSync('bash', [entrypoint, ...args], {
      env: { PATH: `${stubDir}:/usr/bin:/bin`, ...(role ? { IDEA_CONTAINER_ROLE: role } : {}) },
      encoding: 'utf8',
    });
    return { status: 0, out };
  } catch (e) {
    const err = e as { status: number; stdout: string; stderr: string };
    return { status: err.status, out: `${err.stdout}${err.stderr}` };
  }
}

test('first argument selects the ideactl role', () => {
  const { status, out } = run(['ideactl', 'about']);
  assert.equal(status, 0);
  assert.match(out, /^node \/opt\/idea\/ideactl\/dist\/src\/cli\/main\.js about$/m);
});

test('IDEA_CONTAINER_ROLE still works with no arguments', () => {
  const { status, out } = run([], 'ideactl');
  assert.equal(status, 0);
  assert.match(out, /^node \/opt\/idea\/ideactl\/dist\/src\/cli\/main\.js$/m);
});

test('a first argument wins over IDEA_CONTAINER_ROLE', () => {
  const { out } = run(['ideactl', 'config', 'show'], 'cluster-manager');
  assert.match(out, /main\.js config show$/m);
});

test('neither argument nor env is an error', () => {
  const { status, out } = run(['--help']);
  assert.notEqual(status, 0);
  assert.match(out, /IDEA_CONTAINER_ROLE is required/);
});

test('each installed application exposes a distinct CLI independent of install order', () => {
  for (const [module, command, target] of [
    ['idea-cluster-manager', 'idea-cluster-manager-cli', 'ideaclustermanager'],
    ['idea-scheduler', 'idea-scheduler-cli', 'ideascheduler'],
    ['idea-virtual-desktop-controller', 'idea-vdc-cli', 'ideavirtualdesktopcontroller'],
  ]) {
    // Execute setup.py with setuptools stubbed: inspect the actual registration pip installs.
    const registration = execFileSync('python3', ['-c', `
import runpy, sys, types
from pathlib import Path
setup = Path(sys.argv[1])
sys.path.insert(0, str(setup.parent))
sys.modules['setuptools'] = types.SimpleNamespace(find_packages=lambda: [], setup=lambda **kw: print(kw['entry_points']))
runpy.run_path(str(setup))
`, join(repoRoot, 'source/idea', module!, 'src/setup.py')], { encoding: 'utf8' });
    assert.ok(registration.includes(`${command}=${target}.cli.cli_main:main`), registration);
  }
});
