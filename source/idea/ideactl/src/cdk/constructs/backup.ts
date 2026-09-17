/**
 * A plain holder around the `backup.BackupPlan` / `backup.BackupSelection` L2s. Construct ids are
 * `<backupPlanName>` and `<backupPlanName>-selection`, the plan's id is the *plan name*
 * (`<cluster>-<moduleId>`).
 *
 * `disableDefaultBackupPolicy: true` keeps the L2 from attaching
 * `AWSBackupServiceRolePolicyForBackup` to the role it is handed; the cluster stack builds that
 * role with the copied policies itself.
 */

import { Duration } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import * as backup from 'aws-cdk-lib/aws-backup';
import * as events from 'aws-cdk-lib/aws-events';
import type * as iam from 'aws-cdk-lib/aws-iam';

import { convertCustomTags } from '../base-stack.ts';
import { valueAsDict, valueAsInt, valueAsList, valueAsString, valueAsBool } from './storage.ts';

export interface BackupPlanProps {
  backupPlanName: string;
  /** The `<module>.backups.backup_plan` subtree. */
  backupPlanConfig: Record<string, unknown> | undefined;
  backupVault: backup.IBackupVault;
  backupRole: iam.IRole;
}

export class BackupPlan {
  readonly backupPlan: backup.BackupPlan;
  readonly backupSelection: backup.BackupSelection;

  constructor(scope: Construct, props: BackupPlanProps) {
    const config = props.backupPlanConfig;

    const rules: backup.BackupPlanRule[] = [];
    const ruleConfigs = valueAsDict('rules', config) ?? {};
    for (const [ruleName, rawRule] of Object.entries(ruleConfigs)) {
      const rule = rawRule as Record<string, unknown> | undefined;
      const deleteAfterDays = valueAsInt('delete_after_days', rule);
      const startWindowMinutes = valueAsInt('start_window_minutes', rule);
      const completionWindowMinutes = valueAsInt('completion_window_minutes', rule);
      const scheduleExpression = valueAsString('schedule_expression', rule);
      const moveToColdStorageAfterDays = valueAsInt('move_to_cold_storage_after_days', rule);

      // All four rule properties are required.
      if (deleteAfterDays === undefined) throw new Error(`backup rule ${ruleName}: delete_after_days is required`);
      if (startWindowMinutes === undefined) {
        throw new Error(`backup rule ${ruleName}: start_window_minutes is required`);
      }
      if (completionWindowMinutes === undefined) {
        throw new Error(`backup rule ${ruleName}: completion_window_minutes is required`);
      }
      if (scheduleExpression === undefined) {
        throw new Error(`backup rule ${ruleName}: schedule_expression is required`);
      }

      rules.push(
        new backup.BackupPlanRule({
          ruleName,
          backupVault: props.backupVault,
          startWindow: Duration.minutes(startWindowMinutes as number),
          completionWindow: Duration.minutes(completionWindowMinutes as number),
          deleteAfter: Duration.days(deleteAfterDays as number),
          moveToColdStorageAfter:
            moveToColdStorageAfterDays === undefined
              ? undefined
              : Duration.days(moveToColdStorageAfterDays as number),
          scheduleExpression: events.Schedule.expression(scheduleExpression),
        }),
      );
    }

    this.backupPlan = new backup.BackupPlan(scope, props.backupPlanName, {
      backupPlanName: props.backupPlanName,
      backupPlanRules: rules,
      backupVault: props.backupVault,
      windowsVss: valueAsBool('enable_windows_vss', config, false),
    });

    const selection = valueAsDict('selection', config) ?? {};
    const selectionTags = convertCustomTags((valueAsList('tags', selection) ?? []) as string[]);
    const resources = Object.entries(selectionTags).map(([key, value]) =>
      backup.BackupResource.fromTag(key, value, backup.TagOperation.STRING_EQUALS),
    );

    this.backupSelection = new backup.BackupSelection(scope, `${props.backupPlanName}-selection`, {
      backupPlan: this.backupPlan,
      resources,
      backupSelectionName: `${props.backupPlanName}-selection`,
      role: props.backupRole,
      disableDefaultBackupPolicy: true,
    });
  }

  getBackupPlanArn(): string {
    return this.backupPlan.backupPlanArn;
  }
}
