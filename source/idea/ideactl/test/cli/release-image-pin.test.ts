import assert from 'node:assert/strict';
import { test } from 'node:test';
import { MockAgent, fetch } from 'undici';
import { compareUpgradeDrift } from '../../src/config/upgrade-drift.ts';
import { planEcsImageFollowsRelease, verifyReleaseImage, publicRegistryImageDigest } from '../../src/cli/commands/upgrade.ts';

const repository = 'public.ecr.aws/example/control';
const digest = `sha256:${'a'.repeat(64)}`;
const target = `${repository}:26.11.1`;
const settings = [
  { key: 'cluster.aws.partition', value: 'aws-us-gov' },
  { key: 'ecs.image_repositories.aws', value: repository },
  { key: 'ecs.image', value: `${repository}:26.10.9` },
];

test('S-22: GovCloud public fallback pins the previewed manifest and keeps following releases', async () => {
  for (const skip of [false, true]) {
    const entries = await verifyReleaseImage({
      out: () => {},
      releaseImageExists: async () => true,
      releaseImageDigest: async (image: string) => { assert.equal(image, target); return digest; },
    }, planEcsImageFollowsRelease(settings, '26.11.1'), settings, skip);
    assert.deepEqual(entries, [
      { key: 'ecs.release_image', value: `${target}@${digest}` },
      { key: 'ecs.image', value: `${target}@${digest}` },
    ]);
    const next = [...settings.filter(row => row.key !== 'ecs.image'), ...entries];
    assert.deepEqual(planEcsImageFollowsRelease(next, '26.12.1'), [
      { key: 'ecs.image', value: `${repository}:26.12.1` },
    ]);
    // A digest the operator sets separately remains theirs.
    next.find(row => row.key === 'ecs.image')!.value = `${target}@sha256:${'b'.repeat(64)}`;
    assert.deepEqual(planEcsImageFollowsRelease(next, '26.12.1'), []);
  }
});

test('S-22: missing or invalid digests block execution and remove the image move from preview', async () => {
  for (const value of [undefined, '', 'sha256:bad']) {
    const deps = { out: () => {}, releaseImageExists: async () => true, releaseImageDigest: async () => value };
    const planned = planEcsImageFollowsRelease(settings, '26.11.1');
    await assert.rejects(verifyReleaseImage(deps, planned, settings), /digest|does not exist/);
    const findings: string[] = [];
    assert.deepEqual(await verifyReleaseImage(deps, planned, settings, false, message => findings.push(message)), []);
    assert.equal(findings.length, 1);
  }
});


test('S-22: real registry responses yield the index digest, with fail-closed header validation', async () => {
  for (const header of [digest, '', 'sha256:bad']) {
    const agent = new MockAgent();
    agent.disableNetConnect();
    const pool = agent.get('https://public.ecr.aws');
    pool.intercept({ path: '/token/?scope=repository:example/control:pull', method: 'GET' })
      .reply(200, { token: 'test-token' });
    pool.intercept({ path: '/v2/example/control/manifests/26.11.1', method: 'HEAD' })
      .reply(200, '', { headers: { 'docker-content-digest': header } });
    const result = publicRegistryImageDigest(target, fetch, () => agent);
    if (header === digest) assert.equal(await result, digest);
    else await assert.rejects(result, /no valid manifest digest/);
    agent.assertNoPendingInterceptors();
  }
});

test('S-22: the managed digest marker remains a routine release update', () => {
  const report = compareUpgradeDrift({
    current: [{ key: 'ecs.release_image', value: 'old' }], generated: [],
    phase3: [{ key: 'ecs.release_image', value: `${target}@${digest}` },
      { key: 'ecs.previous_release_image', value: 'old' }],
  });
  for (const key of ['ecs.release_image', 'ecs.previous_release_image']) {
    assert.equal(report.findings.find(row => row.key === key)?.routineUpdate, 'release image');
  }
});

test('T-6/T-10: interrupting the first pin write preserves release following', async () => {
  const entries = await verifyReleaseImage({ out: () => {}, releaseImageDigest: async () => digest },
    planEcsImageFollowsRelease(settings, '26.11.1'), settings);
  const interrupted = new Map(settings.map(row => [row.key, row.value]));
  interrupted.set(entries[0]!.key, String(entries[0]!.value));
  const current = [...interrupted].map(([key, value]) => ({ key, value }));
  for (const release of ['26.11.1', '26.12.1']) {
    assert.deepEqual(planEcsImageFollowsRelease(current, release), [
      { key: 'ecs.image', value: `${repository}:${release}` },
    ]);
  }
});


test('T-6/T-10: every interrupted write also recovers a previously managed digest', async () => {
  const previous = `${repository}:26.10.9@sha256:${'c'.repeat(64)}`;
  const current = [...settings.filter(row => row.key !== 'ecs.image'),
    { key: 'ecs.image', value: previous }, { key: 'ecs.release_image', value: previous }];
  const entries = await verifyReleaseImage({ out: () => {}, releaseImageDigest: async () => digest },
    planEcsImageFollowsRelease(current, '26.11.1'), current);
  const interrupted = new Map(current.map(row => [row.key, row.value]));
  for (const entry of entries) {
    interrupted.set(entry.key, String(entry.value));
    assert.deepEqual(planEcsImageFollowsRelease([...interrupted].map(([key, value]) => ({ key, value })), '26.12.1'), [
      { key: 'ecs.image', value: `${repository}:26.12.1` },
    ]);
  }
});


test('T-10: an operator digest pin is visible', async () => {
  const output: string[] = [];
  const pinned = [...settings.filter(row => row.key !== 'ecs.image'),
    { key: 'ecs.image', value: `${target}@${digest}` }];
  assert.deepEqual(await verifyReleaseImage({ out: line => output.push(line) }, [], pinned), []);
  assert.match(output.join('\n'), /operator pin and does not follow releases/);
});
