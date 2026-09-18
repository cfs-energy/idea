// Assert image properties that require CI build inputs:
//   1. nothing arch-dependent falls back to a hardcoded architecture, and
//   2. the release bundle stays in ~/.idea/downloads, where upload-packages/patch read it.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const DOCKERFILE = new URL(
  '../../../../../deployment/ecr/idea-control-plane/Dockerfile',
  import.meta.url,
);
const text = readFileSync(DOCKERFILE, 'utf8');
const downloadsDirectory = "/root/.idea/downloads";

/** Return whether a removal target is the downloads directory, an ancestor, or a descendant. */
function removalTouchesDownloads(rawTarget: string): boolean {
  const target = rawTarget
    .replace(/^(['"])(.*)\1$/, "$2")
    .replace(/^(?:~|\$HOME|\$\{HOME\})(?=\/|$)/, "/root")
    .replace(/\/+$/, "");
  return (
    target === downloadsDirectory ||
    downloadsDirectory.startsWith(`${target}/`) ||
    target.startsWith(`${downloadsDirectory}/`)
  );
}

test('OpenPBS is compiled from pinned source inside the control-plane build', () => {
  const stage = text.match(/^FROM public\.ecr\.aws\/amazonlinux\/amazonlinux:2023 AS pbs\n([\s\S]*?)(?=^FROM )/m)?.[1];
  assert.ok(stage, 'no local OpenPBS build stage');
  assert.match(stage, /^ARG OPENPBS_VERSION=23\.06\.06$/m);
  assert.match(stage, /^ARG OPENPBS_URL=https:\/\/github\.com\/openpbs\/openpbs\/archive\/v23\.06\.06\.tar\.gz$/m);
  assert.match(stage, /^ARG OPENPBS_SHA384=8a4d7f9c326fd1de5c103e700422bc4d49edc9d50f142c033e9e7de8d10d52f5c4f92e902e107b8e89d3e12c147ebef4$/m);
  assert.ok(stage.includes('echo "${OPENPBS_SHA384}  openpbs.tar.gz" | sha384sum -c -'));
  assert.ok(stage.indexOf('sha384sum -c -') < stage.indexOf('tar xzf openpbs.tar.gz'));
  assert.match(stage, /python3-devel/);
  assert.match(stage, /make install/);
  assert.match(stage, /pbs_postinstall/);
  assert.match(stage, /chmod 4755 \/opt\/pbs\/sbin\/pbs_iff \/opt\/pbs\/sbin\/pbs_rcp/);
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

test('the release bundle lands in ~/.idea/downloads and is never deleted', () => {
  assert.match(
    text,
    /^ADD \S*all-\$\{IDEA_VERSION\}\.tar\.gz \/root\/\.idea\/downloads\/$/m,
    'the release bundle is not ADDed to /root/.idea/downloads/',
  );
  for (const m of text.matchAll(/rm -rf ([^;\n]*)/g)) {
    const targets = m[1].match(/"[^"]*"|'[^']*'|\S+/g) ?? [];
    for (const target of targets) {
      assert.ok(
        !removalTouchesDownloads(target),
        `an rm -rf touches the downloads tree: rm -rf ${m[1]}`,
      );
    }
  }
});
