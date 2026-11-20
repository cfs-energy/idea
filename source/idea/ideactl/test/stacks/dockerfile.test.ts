// Assert image properties that require CI build inputs:
//   1. nothing arch-dependent falls back to a hardcoded architecture, and
//   2. release archives are temporary build inputs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const DOCKERFILE = new URL(
  '../../../../../deployment/ecr/idea-control-plane/Dockerfile',
  import.meta.url,
);
const text = readFileSync(DOCKERFILE, 'utf8');
test('OpenPBS is compiled from pinned source inside the control-plane build', () => {
  const stage = text.match(/^FROM public\.ecr\.aws\/amazonlinux\/amazonlinux:2023@sha256:[a-f0-9]{64} AS pbs\n([\s\S]*?)(?=^FROM )/m)?.[1];
  assert.ok(stage, 'no local OpenPBS build stage');
  assert.match(stage, /^ARG OPENPBS_VERSION=23\.06\.06$/m);
  assert.match(stage, /^ARG OPENPBS_URL=https:\/\/github\.com\/openpbs\/openpbs\/archive\/v23\.06\.06\.tar\.gz$/m);
  assert.match(stage, /^ARG OPENPBS_SHA384=8a4d7f9c326fd1de5c103e700422bc4d49edc9d50f142c033e9e7de8d10d52f5c4f92e902e107b8e89d3e12c147ebef4$/m);
  assert.ok(stage.includes('echo "${OPENPBS_SHA384}  openpbs.tar.gz" | sha384sum -c -'));
  assert.ok(stage.indexOf('sha384sum -c -') < stage.indexOf('tar xzf openpbs.tar.gz'));
  assert.match(stage, /python3-devel/);
  assert.match(stage, /make install/);
  assert.match(stage, /pbs_postinstall/);
  assert.match(stage, /chmod 4755 \/opt\/pbs\/sbin\/pbs_iff;/);
  assert.match(stage, /chmod 0755 \/opt\/pbs\/sbin\/pbs_rcp/);
  assert.doesNotMatch(stage, /IDEA_VERSION|^COPY |^ADD /m, 'release inputs must not invalidate the OpenPBS cache');
  assert.match(text, /^COPY --from=pbs \/opt\/pbs \/opt\/pbs$/m);
});

test('no arch-dependent step defaults to an architecture when TARGETARCH is unset', () => {
  for (const m of text.matchAll(/\$\{TARGETARCH:-([^}]*)\}/g)) {
    assert.equal(m[1], '', `TARGETARCH falls back to "${m[1]}"`);
  }
  // every case block over TARGETARCH ends in a failing default arm
  const arms = [...text.matchAll(/case "\$\{TARGETARCH:?-?\}?" in([\s\S]*?)esac/g)];
  assert.ok(arms.length > 0, 'no TARGETARCH case block found');
  for (const [, body] of arms) assert.match(body, /\*\)[^\n]*exit 1/);
});

test('the release bundle is mounted and removed in the install layer', () => {
  assert.doesNotMatch(text, /\/root\/\.idea\/downloads/);
  assert.match(text, /RUN --mount=type=bind,source=deployment\/ecr\/idea-control-plane\/all-\$\{IDEA_VERSION\}\.tar\.gz,target=\/tmp\/release\.tar\.gz/);
  const install = text.split('target=/tmp/release.tar.gz')[1]?.split('\n\n')[0] ?? '';
  assert.match(install, /tar -xzf \/tmp\/release\.tar\.gz -C \/tmp\/release/);
  assert.match(install, /rm -rf \/tmp\/release \/tmp\/idea/);
});

test('module archives resolve nothing outside the lock and the build checks what is installed', () => {
  const install = text.split('target=/tmp/release.tar.gz')[1]?.split('\n\n')[0] ?? '';
  const pips = install.match(/python3\.13 -m pip install [^;]+/g) ?? [];
  assert.equal(pips.length, 3);
  // the lock's own setuptools, hashed, before anything is built
  assert.match(pips[0]!, /--require-hashes --no-deps -r \/tmp\/setuptools\.txt$/);
  // source-only pins and the module archives build with it, never in a fetched build environment
  assert.match(pips[1]!, /--require-hashes --no-deps --no-build-isolation --check-build-dependencies -r "\$\{PKG\}\/requirements\.txt"$/);
  assert.match(pips[2]!, /--no-deps --no-build-isolation --check-build-dependencies "\$\{PKG\}"\/\*-lib\.tar\.gz$/);
  assert.ok(install.indexOf("grep -q -- '--hash=sha256:' /tmp/setuptools.txt") < install.indexOf(pips[0]!));
  assert.ok(install.indexOf('python3.13 -m pip check') > install.lastIndexOf('pip install'));
});

test('the setuptools pin is taken from the lock with every hash line', () => {
  const command = text.match(/awk '(\/\^setuptools==\/[^']+)'/)?.[1];
  assert.ok(command, 'the extraction step');
  const lock = 'aiofiles==25.1.0 \\\n    --hash=sha256:aa\n    # via sanic\nsetuptools==84.2.1 \\\n    --hash=sha256:bb \\\n    --hash=sha256:cc\n    # via -r runtime.in\nsix==1.17.0 \\\n    --hash=sha256:dd\n';
  const out = execFileSync('awk', [command], { input: lock, encoding: 'utf8' });
  assert.equal(out, 'setuptools==84.2.1 \\\n    --hash=sha256:bb \\\n    --hash=sha256:cc\n');
});

test('only pbs_iff and sudo keep a set-id bit, cleared after the last package change', () => {
  const layers = text.split('\nRUN ');
  const layer = [...layers].reverse().find((each) => /\bdnf (install|upgrade)\b/.test(each))!;
  const strip = layer.indexOf('/opt/idea/roles/setid_strip.sh --build /;');
  const upgrade = layer.lastIndexOf('dnf upgrade');
  assert.ok(strip >= 0 && upgrade >= 0);
  assert.ok(strip > upgrade, 'the strip must follow the last upgrade');
  const roles = text.indexOf('COPY deployment/ecr/idea-control-plane/roles /opt/idea/roles');
  assert.ok(roles >= 0 && roles < text.indexOf(layer), 'the script is in the image before the layer runs it');
  assert.ok(layer.includes('test -u /usr/bin/sudo') && layer.includes('test -u /opt/pbs/sbin/pbs_iff'));
  assert.match(text, /^\s+glibc-langpack-en [^\n]*\bfindutils\b/m);
});

test('the image has the service account the scheduler runs license checks as', () => {
  assert.match(text, /useradd --system --no-create-home --shell \/sbin\/nologin ideaserviceaccount/);
});

test('base images and both Node archives are integrity pinned', () => {
  const bases = text.match(/^FROM public\.ecr\.aws\/amazonlinux\/amazonlinux:[^\n]+/gm) ?? [];
  assert.equal(bases.length, 2);
  for (const base of bases) assert.match(base, /:2023@sha256:[a-f0-9]{64} AS /);
  const node = text.split('ARG NODE_VERSION=')[1]!.split('\nENV PATH=')[0]!;
  for (const arch of ['amd64', 'arm64']) assert.match(node, new RegExp(`${arch}\\) N=\\w+; SUM=[a-f0-9]{64}`));
  assert.ok(node.includes('sha256sum -c /tmp/node.sha256'));
  assert.ok(node.indexOf('sha256sum -c') < node.indexOf('tar -xzf'));
});


test('T-11/T-30: Node patches are visible but require approval and manual checksum updates', () => {
  const version = text.match(/^ARG NODE_VERSION=(\S+)$/m)?.[1];
  assert.equal(text.match(/^# Node archive checksums for (\S+)$/m)?.[1], version);
  const renovate = JSON.parse(readFileSync(new URL('../../../../../.github/renovate.json', import.meta.url), 'utf8'));
  for (const update of ['major', 'minor', 'patch']) {
    const effective = Object.assign({}, ...renovate.packageRules.filter((rule: any) =>
      rule.matchDepNames?.includes('node') && (!rule.matchUpdateTypes || rule.matchUpdateTypes.includes(update))));
    if (update === 'patch') {
      assert.equal(effective.enabled, true);
      assert.equal(effective.dependencyDashboardApproval, true);
      assert.equal(effective.automerge, false);
      assert.equal(effective.groupName, null);
      assert.match(effective.description, /checksums/);
    } else assert.equal(effective.enabled, false);
  }
  assert.match(renovate.customManagers.find((manager: any) => manager.depNameTemplate === 'node').description,
    /Dependency Dashboard/);
});

test('S-39: the image uv pin follows the toolchain through Renovate', () => {
  const renovate = JSON.parse(readFileSync(new URL('../../../../../.github/renovate.json', import.meta.url), 'utf8'));
  const manager = renovate.customManagers.find((entry: any) => entry.depNameTemplate === 'uv');
  assert.equal(manager?.datasourceTemplate, 'pypi');
  const match = text.match(new RegExp(manager.matchStrings[0]));
  const mise = readFileSync(new URL('../../../../../mise.toml', import.meta.url), 'utf8');
  assert.equal(match?.groups?.currentValue, mise.match(/^uv = "([^"]+)"$/m)?.[1]);
});


test('the image, the host installer and the toolchain pin the same Node, each seen by Renovate', () => {
  const renovate = JSON.parse(readFileSync(new URL('../../../../../.github/renovate.json', import.meta.url), 'utf8'));
  const manager = renovate.customManagers.find((entry: any) => entry.depNameTemplate === 'node');
  const versions = readFileSync(new URL('../../../../../software_versions.yml', import.meta.url), 'utf8');
  const mise = readFileSync(new URL('../../../../../mise.toml', import.meta.url), 'utf8');
  const found = (source: string) => manager.matchStrings
    .map((pattern: string) => source.match(new RegExp(pattern))?.groups?.currentValue)
    .find((value: string | undefined) => value != null);
  const image = found(text);
  assert.match(image, /^\d+\.\d+\.\d+$/);
  assert.equal(found(versions), image);
  assert.equal(mise.match(/^node = "([^"]+)"$/m)?.[1], image);
});


test('U-27: mise Java and java-jdk platform major and minor updates stay manual', () => {
  const renovate = JSON.parse(readFileSync(new URL('../../../../../.github/renovate.json', import.meta.url), 'utf8'));
  for (const depName of ['java', 'java-jdk']) {
    for (const update of ['major', 'minor']) {
      const effective = Object.assign({}, ...renovate.packageRules.filter((rule: any) =>
        rule.matchDepNames?.includes(depName) && (!rule.matchUpdateTypes || rule.matchUpdateTypes.includes(update))));
      assert.equal(effective.enabled, false, `${depName} ${update}`);
    }
  }
});
