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

test('the pbs stage base is one multi-arch tag, resolved per platform by buildx', () => {
  const from = text.match(/^FROM (\S+) AS pbs$/m);
  assert.ok(from, 'no `FROM ... AS pbs` stage');
  assert.equal(from![1], '${PBS_IMAGE}');
  // PBS_IMAGE defaults to one multi-arch tag and stays overridable.
  const arg = text.match(/^ARG PBS_IMAGE=(.*)$/m);
  assert.ok(arg, 'PBS_IMAGE has no default');
  assert.doesNotMatch(arg![1], /TARGETARCH/);
  assert.match(text, /^ARG PBS_IMAGE_REPO=idea-scheduler-pbs$/m);
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
