/**
 * `constructs/backup.ts` against templates the Python administrator generated.
 *
 * The dev27 capture has `cluster.backups.enabled: false`, so no live template contains an
 * `AWS::Backup::*` resource and `backup.test.ts` can only assert the shapes the spec fixes. The
 * Python-generated `cdk.out` trees under `~/.idea/clusters` (gitignored, present only where the
 * Python administrator has been run) are a two-sided oracle for the same construct: they are the
 * output of the code this file ports, driven by a config this test reads from the same tree.
 *
 * Every cluster found there is exercised; the whole suite requires the directory.
 * Nothing about a specific cluster is hard coded: names, plan names and the role ARN all come out
 * of the discovered files at run time.
 *
 * Construct ids mirror the two call sites, so the logical IDs match Python's and the emitted
 * `Properties` can be compared whole:
 *   `cluster_stack.py:275-330`   backup-vault + cluster-backup-role, vault created in-stack
 *   `virtual_desktop_controller_stack.py:1371-1394`  both imported, so the vault renders as a name
 */

import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { after, describe, test } from 'node:test';

import { RemovalPolicy } from 'aws-cdk-lib';
import * as backup from 'aws-cdk-lib/aws-backup';
import * as iam from 'aws-cdk-lib/aws-iam';
import yaml from 'js-yaml';

import { BackupPlan } from '../../src/cdk/constructs/backup.ts';
import { buildServicePrincipal } from '../../src/cdk/constructs/base.ts';
import { cleanup, harness } from './harness.ts';
import type { Json } from './harness.ts';
import { requireFixtures } from '../support/fixtures.ts';

after(cleanup);

const CLUSTERS_DIR = join(homedir(), '.idea', 'clusters');
requireFixtures(
  [CLUSTERS_DIR],
  "Run the Python administrator to synthesize a cluster with backup configuration",
);

interface OracleCase {
  /** `cluster` or `vdc`: which of the two call sites this template came from. */
  module: string;
  /** The Python-generated `AWS::Backup::BackupPlan` resource, by logical ID. */
  plan: [string, Json];
  /** The Python-generated `AWS::Backup::BackupSelection` resource, by logical ID. */
  selection: [string, Json];
  /** The `backup_plan` subtree Python was handed, read from the same cluster's config. */
  planConfig: Record<string, unknown>;
}

function readYaml(path: string): Record<string, unknown> {
  return (yaml.load(readFileSync(path, 'utf8'), { schema: yaml.CORE_SCHEMA }) ?? {}) as Record<string, unknown>;
}

function subtree(value: unknown, path: string[]): Record<string, unknown> | undefined {
  let current: unknown = value;
  for (const key of path) {
    if (current === null || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current === null || typeof current !== 'object' ? undefined : (current as Record<string, unknown>);
}

/** One case per (cluster, module) with both a generated template and the config behind it. */
function discoverCases(): OracleCase[] {
  const sources: Array<{ module: string; settings: string[]; configPath: string[] }> = [
    { module: 'cluster', settings: ['cluster', 'settings.yml'], configPath: ['backups', 'backup_plan'] },
    {
      module: 'vdc',
      settings: ['vdc', 'settings.yml'],
      configPath: ['vdi_host_backup', 'backup_plan'],
    },
  ];

  const cases: OracleCase[] = [];
  for (const cluster of readdirSync(CLUSTERS_DIR)) {
    const clusterDir = join(CLUSTERS_DIR, cluster);
    for (const region of readdirSync(clusterDir).filter((entry) => existsSync(join(clusterDir, entry, '_cdk')))) {
      const root = join(clusterDir, region);
      for (const source of sources) {
        const cdkOut = join(root, '_cdk', `cdk.out.${source.module}`);
        const settingsFile = join(root, 'config', ...source.settings);
        if (!existsSync(cdkOut) || !existsSync(settingsFile)) continue;
        const templateFile = readdirSync(cdkOut).find((entry) => entry.endsWith('.template.json'));
        if (templateFile === undefined) continue;

        const resources = (JSON.parse(readFileSync(join(cdkOut, templateFile), 'utf8')) as Json).Resources as Json;
        const byType = (type: string): [string, Json] | undefined =>
          Object.entries(resources).find(([, resource]) => (resource as Json).Type === type) as
            | [string, Json]
            | undefined;
        const plan = byType('AWS::Backup::BackupPlan');
        const selection = byType('AWS::Backup::BackupSelection');
        const planConfig = subtree(readYaml(settingsFile), source.configPath);
        if (plan === undefined || selection === undefined || planConfig === undefined) continue;

        cases.push({ module: source.module, plan, selection, planConfig });
      }
    }
  }
  return cases;
}

const cases = discoverCases();

/** Rebuilds one discovered case with this port, in a throwaway stack. */
function build(oracle: OracleCase): Json {
  const planName = ((oracle.plan[1].Properties as Json).BackupPlan as Json).BackupPlanName as string;
  const roleArn = ((oracle.selection[1].Properties as Json).BackupSelection as Json).IamRoleArn as
    | string
    | Json;
  const h = harness({ moduleId: oracle.module, moduleName: oracle.module });

  let vault: backup.IBackupVault;
  let role: iam.IRole;
  if (oracle.module === 'cluster') {
    // the cluster stack creates both, and the selection points at them by GetAtt
    vault = new backup.BackupVault(h.base.stack, 'backup-vault', {
      backupVaultName: `${planName}-backup-vault`,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    role = new iam.Role(h.base.stack, 'cluster-backup-role', {
      roleName: `${planName}-backup-role-${h.ctx.awsRegion}`,
      assumedBy: buildServicePrincipal('backup'),
    }).withoutPolicyUpdates();
  } else {
    // the vdc stack imports both; the vault name comes back out of its ARN
    const vaultName = ((oracle.plan[1].Properties as Json).BackupPlan as Json).BackupPlanRule as Json[];
    vault = backup.BackupVault.fromBackupVaultName(
      h.base.stack,
      'cluster-backup-vault',
      (vaultName[0] as Json).TargetBackupVault as string,
    );
    role = iam.Role.fromRoleArn(h.base.stack, 'backup-role', roleArn as string);
  }

  new BackupPlan(h.base.stack, {
    backupPlanName: planName,
    backupPlanConfig: oracle.planConfig,
    backupVault: vault,
    backupRole: role,
  });
  return h.template().Resources as Json;
}

describe('BackupPlan against the Python-generated templates', () => {
  test('a case was discovered for both call sites', () => {
    assert.ok(cases.length >= 2, `expected at least one cluster and one vdc case, found ${cases.length}`);
    assert.deepEqual(
      [...new Set(cases.map((entry) => entry.module))].sort(),
      ['cluster', 'vdc'],
      'both BackupPlan call sites must be covered',
    );
  });

  for (const oracle of cases) {
    const [planId, pythonPlan] = oracle.plan;
    const [selectionId, pythonSelection] = oracle.selection;

    test(`${oracle.module}/${planId}: the plan matches Python's, logical ID included`, () => {
      const resources = build(oracle);
      assert.ok(resources[planId] !== undefined, `missing logical id: ${planId}`);
      assert.deepEqual(resources[planId].Properties, pythonPlan.Properties);
    });

    test(`${oracle.module}/${selectionId}: the selection matches Python's, logical ID included`, () => {
      const resources = build(oracle);
      assert.ok(resources[selectionId] !== undefined, `missing logical id: ${selectionId}`);
      assert.deepEqual(resources[selectionId].Properties, pythonSelection.Properties);
    });
  }
});
