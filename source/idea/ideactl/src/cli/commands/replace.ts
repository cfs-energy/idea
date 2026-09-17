/**
 * `replace`: the deliberate path to a new stateful component.
 *
 * Replacing something that holds state is an intentional act with a consequence an operator has to
 * accept in advance. It is never something an upgrade or a migration does on the way past, so it
 * has its own verb rather than a flag on theirs. `upgrade` and `deploy` keep refusing every
 * replacement through the change-set guard, and the `UpdateReplacePolicy: Retain` on the resources
 * an upgrade has never replaced is the backstop under that, for a deploy that reaches
 * CloudFormation by some other route.
 *
 * Two components here, the jump host and the scheduler host, are replaced by an ordinary upgrade
 * already and carry no retain policy. They are listed because an operator may want to replace one
 * on purpose, and the consequence is worth reading either way.
 *
 * This command does not invent a replacement. CloudFormation replaces a resource when a property
 * that cannot be changed in place changes, and `forcedBy` below names those properties for each
 * component. The operator changes the setting, then runs this, and this permits the replacement the
 * new configuration already implies, for that one component, once.
 */

import type { Command } from 'commander';

import { DeploymentHelper } from '../deployment-helper.ts';
import { ExitWithCode, type Deps } from '../cdk-invoker.ts';

export interface ReplaceableComponent {
  /** What an operator calls it. */
  component: string;
  /** The module whose stack builds it. */
  moduleName: string;
  /** The CloudFormation type, which is how the allowance is scoped. */
  resourceType: string;
  /** What is lost. Written for the consequence, not for the mechanism. */
  consequence: string;
  /** The properties whose change makes CloudFormation replace it, and where they come from. */
  forcedBy: string;
  /** Set when there is no version of this that ends well. The command refuses and explains. */
  refuse?: string;
}

export const REPLACEABLE_COMPONENTS: readonly ReplaceableComponent[] = [
  {
    component: 'jump-host',
    moduleName: 'bastion-host',
    resourceType: 'AWS::EC2::Instance',
    consequence:
      'The jump host is rebuilt with a new host identity, so every operator who has connected to it ' +
      'before finds their stored host key no longer matches and is asked to accept a new one, and ' +
      'automation that pins the old key stops working until it is updated. An ordinary upgrade ' +
      'already replaces this host, about twenty times over the life of a cluster, so the new key ' +
      'is a familiar interruption rather than a rare one. Keeping the identity across a ' +
      'replacement is being designed separately; until that lands, expect the prompt.',
    forcedBy: 'ImageId, KeyName, LaunchTemplate or NetworkInterfaces, from bastion-host.instance_ami and cluster.network.ssh_key_pair',
  },
  {
    component: 'search-domain',
    moduleName: 'analytics',
    resourceType: 'AWS::OpenSearchService::Domain',
    consequence:
      'The indexed history is destroyed. Every job record, desktop session record and application ' +
      'document already indexed is gone, and none of it is rebuilt from the cluster settings: the ' +
      'new domain starts empty. Unless the intent is to start the index over from nothing, this is ' +
      'almost certainly the wrong action.',
    forcedBy: 'DomainName or EngineMode, from analytics.opensearch.domain_name',
  },
  {
    component: 'directory',
    moduleName: 'directoryservice',
    resourceType: 'AWS::DirectoryService::MicrosoftAD',
    consequence:
      'Every machine joined to the directory is severed from it. Compute nodes, virtual desktops ' +
      'and the control-plane hosts lose their domain membership and have to be rejoined, and the ' +
      'user and group objects that live in the directory rather than in the cluster settings are ' +
      'gone with it. Nobody authenticates against the cluster until the rejoin is finished.',
    forcedBy: 'Name, ShortName, Edition, Password, CreateAlias or VpcSettings, from directoryservice.name, .ad_short_name, .ad_edition and the cluster network settings',
  },
  {
    component: 'user-pool',
    moduleName: 'identity-provider',
    resourceType: 'AWS::Cognito::UserPool',
    consequence:
      'Every account in the pool is lost, along with every multi-factor enrolment. Each person has ' +
      'to be created again and enrol again before they can sign in, and the sign-in integrations ' +
      'that name the old pool stop working until they are pointed at the new one.',
    forcedBy:
      'no property of a user pool is marked replacing in the CloudFormation resource specification, ' +
      'so a replacement here comes from the resource moving or being rebuilt rather than from a setting',
  },
  {
    component: 'scheduler-host',
    moduleName: 'scheduler',
    resourceType: 'AWS::EC2::Instance',
    consequence:
      'The machine the batch server runs on is rebuilt, and whatever the server holds locally goes ' +
      'with it. Execution nodes configured with the current server name stop finding the server, ' +
      'so work in flight is lost rather than requeued. An ordinary upgrade already replaces this ' +
      'host, which is why an upgrade is drained and announced; running it on its own needs the ' +
      'same drain and the same announcement.',
    forcedBy: 'ImageId, KeyName, LaunchTemplate or NetworkInterfaces, from scheduler.instance_ami and cluster.network.ssh_key_pair',
  },
  {
    component: 'shared-file-system',
    moduleName: 'shared-storage',
    resourceType: 'AWS::EFS::FileSystem',
    consequence:
      'The applications and data file systems hold the only copy of what the cluster stores. A ' +
      'replacement is an empty file system beside a full one.',
    forcedBy: 'Encrypted, PerformanceMode, KmsKeyId or AvailabilityZoneName, from shared-storage.apps.efs.* and shared-storage.data.efs.*',
    refuse:
      'Moving to a new file system is a migration, not a replacement: the data has to be copied ' +
      'while both exist, and no ordering of a single deploy does that. Create the new file system, ' +
      'copy the data across, then point the cluster setting at it.',
  },
  {
    component: 'backup-vault',
    moduleName: 'cluster',
    resourceType: 'AWS::Backup::BackupVault',
    consequence: 'The vault holds every recovery point taken from this cluster.',
    forcedBy: 'EncryptionKeyArn, from cluster.backups.backup_vault.kms_key_id; the vault name is derived and not settable',
    refuse:
      'A replacement vault is empty and the recovery points in the old one cannot be moved into ' +
      'it, so there is no version of this that ends with the backups intact. Copy what is needed ' +
      'to a vault you create separately first.',
  },
];

export function componentByName(name: string): ReplaceableComponent | undefined {
  return REPLACEABLE_COMPONENTS.find((entry) => entry.component === name);
}

/** The block an operator reads before they can proceed. */
export function warningFor(component: ReplaceableComponent): string {
  return [
    '',
    `REPLACING ${component.component.toUpperCase()}`,
    '',
    component.consequence,
    '',
    `Forced by a change to: ${component.forcedBy}`,
    `Stack: ${component.moduleName}. Resource type: ${component.resourceType}.`,
    '',
  ].join('\n');
}

export interface ReplaceCommandOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  moduleSet: string;
  deploymentId?: string;
  /** The component name, typed again. Anything else does not proceed. */
  confirm?: string;
}

export async function runReplace(deps: Deps, componentName: string, options: ReplaceCommandOptions): Promise<void> {
  const component = componentByName(componentName);
  if (component === undefined) {
    deps.err(`unknown component: ${componentName}`);
    deps.err(`components: ${REPLACEABLE_COMPONENTS.map((entry) => entry.component).join(', ')}`);
    throw new ExitWithCode(1);
  }

  deps.out(warningFor(component));

  if (component.refuse !== undefined) {
    deps.err(`${component.component} cannot be replaced by this command.`);
    deps.err(component.refuse);
    throw new ExitWithCode(1);
  }

  if (options.confirm !== component.component) {
    deps.err('Nothing was changed.');
    deps.err(`Read the consequence above. To proceed, run the same command again with --confirm ${component.component}`);
    throw new ExitWithCode(1);
  }

  const helper = await DeploymentHelper.open({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    moduleSet: options.moduleSet,
    awsProfile: options.awsProfile,
    deploymentId: options.deploymentId,
    upgrade: true,
    allModules: false,
    moduleIds: [component.moduleName],
    // Scoped to this one type in this one stack, and only to the replacement class. A removal is a
    // different intent and is still refused.
    allowReplacementOfType: new Map([[component.resourceType, component.component]]),
    deps,
  });
  await helper.invoke();

  deps.out(
    `If the change set held no replacement of ${component.resourceType}, nothing was replaced. ` +
      `CloudFormation only replaces when one of these changes: ${component.forcedBy}.`,
  );
}

export function registerReplaceCommands(program: Command, deps: Deps): void {
  program
    .command('replace')
    .description('replace one stateful component, deliberately. Upgrade and migrate never do this.')
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
    .option('--deployment-id <deployment-id>', 'A UUID to identify the deployment.')
    .option('--confirm <component>', 'The component name again. Required, and it is the only thing that lets the run proceed.')
    .argument('<component>', `one of: ${REPLACEABLE_COMPONENTS.map((entry) => entry.component).join(', ')}`)
    .action(async (component: string, options: ReplaceCommandOptions) => {
      await runReplace(deps, component, options);
    });
}
