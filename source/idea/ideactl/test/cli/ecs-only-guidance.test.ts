import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { test } from 'node:test';

const pkg = resolve(import.meta.dirname, '../..');
const root = resolve(pkg, '../../..');
const read = (file: string) => readFileSync(join(pkg, file), 'utf8');

test('P-11 P-52 P-55 P-56: ECS-only guidance names the recovery release and existing guide', () => {
  assert.doesNotMatch(read('resources/config/values.yml'), /^enable_ecs:/m);
  for (const file of ['src/config/values.ts', 'src/cli/commands/config.ts', 'src/cli/commands/deploy.ts', 'src/cli/commands/upgrade.ts']) {
    assert.match(read(file), /HOST_CONTROL_PLANE_REFUSAL/);
  }
  for (const file of readdirSync(join(root, 'docs'), { recursive: true }).filter((file) => String(file).endsWith('.md'))) {
    assert.doesNotMatch(readFileSync(join(root, 'docs', String(file)), 'utf8'), /26\.10\.0 administrator|move-to-containers/, String(file));
  }
  const commands = read('docs/COMMANDS.md');
  assert.doesNotMatch(commands, /existing host clusters upgrade with|move-to-containers/);
  assert.match(commands, /ecs-control-plane\.md#spend-and-storage/);
});

test('P-65: ECS generation and parity tooling no longer advertise a host switch', () => {
  assert.doesNotMatch(read('src/config/generator.ts'), /if \(containers/);
  assert.doesNotMatch(read('src/config/values.ts'), /g\.enableEcs\(\) &&|and enable_ecs = true/);
  assert.doesNotMatch(read('tools/parity/upgrade-dry-run.ts'), /enable-ecs/);
});


test('R3-A-7: the retired host scheduler oracle cannot run in private parity', () => {
  assert.equal(existsSync(join(pkg, 'test/stacks/scheduler-oracle.test.ts')), false);
  assert.match(read('test/stacks/ecs-sunset-parity.test.ts'), /all eleven ECS module templates match/);
  assert.match(read('scripts/private-parity.sh'), /test\/\*\*\/\*\.test\.ts/);
});

test('S-26 S-27: completion guidance and replace targets match ECS-only behavior', () => {
  assert.doesNotMatch(read('src/cli/commands/upgrade.ts'), /The bastion keeps its DNS record and module stack during cutover/);
  const targets = read('src/cli/commands/replace.ts').split('REPLACEABLE_COMPONENTS')[1].split('];')[0];
  assert.doesNotMatch(targets, /\n\s*\n/);
});
