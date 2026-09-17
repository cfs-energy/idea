/**
 * `constructs/backup.ts`.
 *
 * dev27 has `cluster.backups.enabled: false`, so no live template covers these resources; the
 * assertions below are against the shapes the reference implementation fixes, driven by the
 * dev27 `cluster.backups.backup_plan` config, plus the branches that config can take.
 */

import assert from 'node:assert/strict';
import { after, describe, test } from 'node:test';

import * as backup from 'aws-cdk-lib/aws-backup';
import * as iam from 'aws-cdk-lib/aws-iam';

import { BackupPlan } from '../../src/cdk/constructs/backup.ts';
import { buildServicePrincipal } from '../../src/cdk/constructs/base.ts';
import { CLUSTER, cleanup, harness } from '../support/construct-harness.ts';
import type { Json } from '../support/construct-harness.ts';

after(cleanup);

const PLAN_NAME = `${CLUSTER}-cluster`;
const PLAN_ID = 'ideadev27cluster19E9865D';
const SELECTION_ID = 'ideadev27clusterselection7EAA3C5C';

interface BuildOptions {
  /** Defaults to the dev27 `cluster.backups.backup_plan` subtree. */
  backupPlanConfig?: Record<string, unknown>;
  /** Mirrors the VDC stack, which imports the role the cluster stack created. */
  importRole?: boolean;
}

function buildPlan(options: BuildOptions = {}): Json {
  const h = harness({ moduleId: 'cluster', moduleName: 'cluster' });
  const vault = new backup.BackupVault(h.base.stack, 'backup-vault', {
    backupVaultName: `${CLUSTER}-cluster-backup-vault`,
  });
  const role = options.importRole
    ? iam.Role.fromRoleArn(
        h.base.stack,
        'backup-role',
        `arn:aws:iam::111111111111:role/${CLUSTER}-cluster-backup-role-us-east-2`,
      )
    : new iam.Role(h.base.stack, 'cluster-backup-role', {
        roleName: `${CLUSTER}-cluster-backup-role-us-east-2`,
        assumedBy: buildServicePrincipal('backup'),
      });

  new BackupPlan(h.base.stack, {
    backupPlanName: PLAN_NAME,
    backupPlanConfig: options.backupPlanConfig ?? h.ctx.config.getConfig('cluster.backups.backup_plan'),
    backupVault: vault,
    backupRole: role,
  });
  return h.template().Resources as Json;
}

describe('BackupPlan', () => {
  test('the plan and its selection carry the ids and shapes the cluster stack expects', () => {
    const resources = buildPlan();
    const plan = resources[PLAN_ID];
    assert.equal(plan.Type, 'AWS::Backup::BackupPlan');
    assert.equal(plan.Metadata['aws:cdk:path'], `idea-dev27-cluster/${PLAN_NAME}/Resource`);
    assert.deepEqual(plan.Properties.BackupPlan, {
      BackupPlanName: PLAN_NAME,
      BackupPlanRule: [
        {
          CompletionWindowMinutes: 480,
          Lifecycle: { DeleteAfterDays: 7 },
          RuleName: 'default',
          ScheduleExpression: 'cron(0 5 * * ? *)',
          StartWindowMinutes: 60,
          TargetBackupVault: { 'Fn::GetAtt': ['backupvault2340AC35', 'BackupVaultName'] },
        },
      ],
    });

    const selection = resources[SELECTION_ID];
    assert.equal(selection.Type, 'AWS::Backup::BackupSelection');
    assert.equal(selection.Metadata['aws:cdk:path'], `idea-dev27-cluster/${PLAN_NAME}-selection/Resource`);
    assert.deepEqual(selection.Properties.BackupPlanId, { 'Fn::GetAtt': [PLAN_ID, 'BackupPlanId'] });
    assert.equal(selection.Properties.BackupSelection.SelectionName, `${PLAN_NAME}-selection`);
    assert.deepEqual(selection.Properties.BackupSelection.ListOfTags, [
      { ConditionKey: 'idea:BackupPlan', ConditionType: 'STRINGEQUALS', ConditionValue: PLAN_NAME },
    ]);
    assert.deepEqual(selection.Properties.BackupSelection.IamRoleArn, {
      'Fn::GetAtt': ['clusterbackuprole32DD592F', 'Arn'],
    });
    assert.equal(plan.Properties.BackupPlan.AdvancedBackupSettings, undefined);
  });

  test('disable_default_backup_policy keeps the L2 off the role', () => {
    const role = buildPlan().clusterbackuprole32DD592F;
    assert.equal(role.Properties.ManagedPolicyArns, undefined);
  });

  test('an imported role is written as a literal ARN', () => {
    const selection = buildPlan({ importRole: true })[SELECTION_ID];
    assert.equal(
      selection.Properties.BackupSelection.IamRoleArn,
      `arn:aws:iam::111111111111:role/${CLUSTER}-cluster-backup-role-us-east-2`,
    );
  });

  test('move_to_cold_storage_after_days and enable_windows_vss are optional extras', () => {
    const plan = buildPlan({
      backupPlanConfig: {
        enable_windows_vss: true,
        rules: {
          default: {
            delete_after_days: 30,
            start_window_minutes: 60,
            completion_window_minutes: 480,
            move_to_cold_storage_after_days: 8,
            schedule_expression: 'cron(0 5 * * ? *)',
          },
        },
        selection: { tags: ['Key=idea:BackupPlan,Value=idea-dev27-cluster'] },
      },
    })[PLAN_ID];
    assert.deepEqual(plan.Properties.BackupPlan.BackupPlanRule[0].Lifecycle, {
      DeleteAfterDays: 30,
      MoveToColdStorageAfterDays: 8,
    });
    assert.deepEqual(plan.Properties.BackupPlan.AdvancedBackupSettings, [
      { BackupOptions: { WindowsVSS: 'enabled' }, ResourceType: 'EC2' },
    ]);
  });

  test('rules keep their config order and each becomes one BackupPlanRule', () => {
    const rule = (name: string, hour: number): Record<string, unknown> => ({
      delete_after_days: 7,
      start_window_minutes: 60,
      completion_window_minutes: 480,
      schedule_expression: `cron(0 ${hour} * * ? *)`,
      rule_label: name,
    });
    const plan = buildPlan({
      backupPlanConfig: {
        rules: { nightly: rule('nightly', 5), weekly: rule('weekly', 6) },
        selection: { tags: ['Key=idea:BackupPlan,Value=idea-dev27-cluster'] },
      },
    })[PLAN_ID];
    assert.deepEqual(
      (plan.Properties.BackupPlan.BackupPlanRule as Json[]).map((entry) => entry.RuleName),
      ['nightly', 'weekly'],
    );
  });

  test('a rule missing a required field fails at synth', () => {
    assert.throws(
      () =>
        buildPlan({
          backupPlanConfig: {
            rules: { default: { delete_after_days: 7, start_window_minutes: 60 } },
            selection: { tags: [] },
          },
        }),
      /completion_window_minutes is required/,
    );
  });

  test('no selection tags means no resources are selected', () => {
    const selection = buildPlan({
      backupPlanConfig: {
        rules: {
          default: {
            delete_after_days: 7,
            start_window_minutes: 60,
            completion_window_minutes: 480,
            schedule_expression: 'cron(0 5 * * ? *)',
          },
        },
      },
    })[SELECTION_ID];
    assert.equal(selection.Properties.BackupSelection.ListOfTags, undefined);
  });
});
