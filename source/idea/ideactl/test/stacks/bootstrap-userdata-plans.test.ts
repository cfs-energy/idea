import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { BootstrapPackageBuilder, bootstrapPackagePlans } from '../../src/cli/bootstrap-package.ts';
import { BOOTSTRAP_SOURCE, templateContext } from './bootstrap-oracle-context.ts';
import { readTarArchive } from './tar.ts';

for (const [moduleName, script] of [['ecs', 'ecs-host/directory_join.sh'], ['directoryservice', 'openldap-server/setup.sh']]) {
  test(`${moduleName} bootstrap plan packages its real entry script`, (t) => {
    const directory = mkdtempSync(join(tmpdir(), 'bootstrap-plan-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const plans = bootstrapPackagePlans(moduleName!, moduleName!, 'offline', 'openldap');
    assert.equal(plans.length, 1);
    const plan = plans[0]!;
    const archive = new BootstrapPackageBuilder({
      sourceDirectory: BOOTSTRAP_SOURCE, targetPackageBasename: plan.basename,
      components: plan.components, context: templateContext(), tmpDir: directory,
    }).build();
    assert.ok(readTarArchive(archive).some((entry) => entry.name === `./${script}` && entry.content.length > 0));
  });
}
