#!/usr/bin/env node
/**
 * `ideactl`: the administrator CLI.
 *
 * Port of `app_main.py`'s command tree and of `main_wrapper`'s exit behaviour. The commands
 * themselves live under `commands/`; this file owns the program, the exit codes, `quick-setup` and
 * the live `Deps` (the only place in the CLI that constructs an AWS client).
 */

import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline/promises';
import { pathToFileURL } from 'node:url';

import { Command } from 'commander';

import { ClusterConfig, ConfigKeyNotFound, GeneralException, type ScanPage } from '../config/cluster-config.ts';
import { loadValuesFile, resourcePath } from '../config/values.ts';
import { ideaVersion } from '../version.ts';
import {
  ExitWithCode,
  liveSpawn,
  type ConfigWriter,
  type ConfigWriterOptions,
  type Deps,
  type PromptChoice,
} from './cdk-invoker.ts';
import { registerCostCollectorCommands } from './commands/cost-collector.ts';
import { registerCdkCommands } from './commands/cdk.ts';
import {
  configGenerate,
  configUpdate,
  pyStr,
  registerConfigCommands,
  renderTable,
  scanSettings,
} from './commands/config.ts';
import { registerDeleteClusterCommands } from './commands/delete-cluster.ts';
import { registerDeployCommands, runBootstrap, runDeploy } from './commands/deploy.ts';
import { registerReplaceCommands } from './commands/replace.ts';
import { registerMigrateCommands } from './commands/migrate.ts';
import { checkClusterStatus, connectionInfo, liveHttpStatus, modulesTable, registerStatusCommands } from './commands/status.ts';
import { createLiveUpgradeDeps, liveEcsAccountSettings, registerUpgradeCommands } from './commands/upgrade.ts';
import { registerRemainingOperatorCommands } from './commands/utils.ts';
import {
  AwsProfileCredentialsError,
  awsClientOptions,
  formatAwsIdentity,
} from "./aws-client-options.ts";
import { DeploymentHelper } from './deployment-helper.ts';
import { createLiveMigrateDeps } from "./live-migrate-adapters.ts";
import { createLiveDeleteClusterDeps, createLiveRemainingOperatorDeps } from "./live-operator-adapters.ts";

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
let actionProfile: string | undefined;
const initialAwsProfile = process.env.AWS_PROFILE;
const initialAwsDefaultRegion = process.env.AWS_DEFAULT_REGION;

function selectActionProfile(profile: string | undefined): void {
  actionProfile = profile === undefined || profile.trim() === "" ? undefined : profile;
  if (actionProfile === undefined) {
    if (initialAwsProfile === undefined) delete process.env.AWS_PROFILE;
    else process.env.AWS_PROFILE = initialAwsProfile;
  } else {
    process.env.AWS_PROFILE = actionProfile;
  }
}

/**
 * Bind the region the command selected, the same way the profile is bound.
 *
 * Every client the live dependencies build resolves its region from the
 * environment, so a `--aws-region` that is read only for the identity banner
 * leaves the table scan, the change-set calls and the values-file reads with no
 * region at all.
 */
function selectActionRegion(awsRegion: string | undefined): void {
  const selected = awsRegion === undefined || awsRegion.trim() === "" ? undefined : awsRegion;
  if (selected === undefined) {
    if (initialAwsDefaultRegion === undefined) delete process.env.AWS_DEFAULT_REGION;
    else process.env.AWS_DEFAULT_REGION = initialAwsDefaultRegion;
  } else {
    process.env.AWS_DEFAULT_REGION = selected;
  }
}

/** The region every live client uses, after the pre-action hook has bound it. */
function environmentRegion(): string {
  return process.env.AWS_DEFAULT_REGION ?? process.env.AWS_REGION ?? '';
}

/** Terminal red, as `click.secho(fg='red', bold=True)` prints it. */
function red(message: string): string {
  return process.stderr.isTTY === true ? `[1;31m${message}[0m` : message;
}

// ---------------------------------------------------------------------------------------------
// live deps
// ---------------------------------------------------------------------------------------------

/**
 * The real effects. Every AWS client is imported lazily so a command that needs no credentials
 * (`about`, `quick-setup-help`, `--help`) loads none of them.
 */
export function liveDeps(): Deps {
  const region = environmentRegion;
  const callerIdentity = async (options: {
    awsRegion: string;
    awsProfile?: string;
  }): Promise<{ account: string; arn: string }> => {
    const { STSClient, GetCallerIdentityCommand } = await import("@aws-sdk/client-sts");
    const result = await new STSClient(
      await awsClientOptions(options.awsRegion, options.awsProfile),
    ).send(new GetCallerIdentityCommand({}));
    if (result.Account === undefined || result.Arn === undefined) {
      const profile = options.awsProfile;
      const resolvedRegion = options.awsRegion;
      const where = [
        profile === undefined || profile.trim() === "" ? undefined : `profile ${profile}`,
        resolvedRegion.trim() === "" ? undefined : `region ${resolvedRegion}`,
      ]
        .filter((part): part is string => part !== undefined)
        .join(" in ");
      throw new GeneralException(
        where === ""
          ? "sts:GetCallerIdentity returned no account. The credentials did not resolve to an account. Pass --aws-profile and --aws-region, then retry."
          : `sts:GetCallerIdentity returned no account for ${where}. The credentials did not resolve to an account. Pass --aws-profile and --aws-region, then retry.`,
      );
    }
    return { account: result.Account, arn: result.Arn };
  };

  const scan = async (input: { TableName: string; ExclusiveStartKey?: Record<string, unknown> }): Promise<ScanPage> => {
    const { DynamoDBClient } = await import('@aws-sdk/client-dynamodb');
    const { DynamoDBDocumentClient, ScanCommand } = await import('@aws-sdk/lib-dynamodb');
    const doc = DynamoDBDocumentClient.from(new DynamoDBClient(await awsClientOptions(region())));
    return doc.send(new ScanCommand(input));
  };

  return {
    spawn: liveSpawn,
    scan,
    // `deploy` and `quick-setup` run the container pre-flight as soon as the deployment includes
    // the container module, so the reader it needs belongs in the live dependency set rather than
    // only in the upgrade command group's.
    ecsAccountSettings: liveEcsAccountSettings(),
    cfn: {
      async describeChangeSet(input) {
        const { CloudFormationClient, DescribeChangeSetCommand } = await import('@aws-sdk/client-cloudformation');
        const client = new CloudFormationClient(await awsClientOptions(region()));
        return client.send(new DescribeChangeSetCommand(input));
      },
      async executeChangeSet(input) {
        const { CloudFormationClient, ExecuteChangeSetCommand } = await import('@aws-sdk/client-cloudformation');
        const client = new CloudFormationClient(await awsClientOptions(region()));
        await client.send(new ExecuteChangeSetCommand(input));
      },
      async updateStack(input) {
        const { CloudFormationClient, UpdateStackCommand } = await import('@aws-sdk/client-cloudformation');
        const client = new CloudFormationClient(await awsClientOptions(region()));
        await client.send(
          new UpdateStackCommand({
            StackName: input.StackName,
            TemplateBody: input.TemplateBody,
            Parameters: input.ParameterKeys.map((key) => ({ ParameterKey: key, UsePreviousValue: true })),
            Capabilities: ['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND'],
          }),
        );
      },
      async getTemplate(stackName) {
        const { CloudFormationClient, GetTemplateCommand } = await import('@aws-sdk/client-cloudformation');
        const client = new CloudFormationClient(await awsClientOptions(region()));
        const result = await client.send(new GetTemplateCommand({ StackName: stackName, TemplateStage: 'Original' }));
        return result.TemplateBody;
      },
      async describeStack(stackName) {
        const { CloudFormationClient, DescribeStacksCommand } = await import('@aws-sdk/client-cloudformation');
        const client = new CloudFormationClient(await awsClientOptions(region()));
        const result = await client.send(new DescribeStacksCommand({ StackName: stackName }));
        const stack = result.Stacks?.[0];
        if (stack === undefined) throw new GeneralException(`stack not found: ${stackName}`);
        return stack;
      },
    },
    s3: {
      async putObject(input) {
        const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
        await new S3Client(await awsClientOptions(region())).send(new PutObjectCommand(input));
      },
      async getObject(input) {
        const { S3Client, GetObjectCommand } = await import('@aws-sdk/client-s3');
        const result = await new S3Client(await awsClientOptions(region())).send(new GetObjectCommand(input));
        return (await result.Body?.transformToString()) ?? '';
      },
    },
    async configWriter(options: ConfigWriterOptions): Promise<ConfigWriter> {
      const { DynamoDBClient } = await import('@aws-sdk/client-dynamodb');
      const { ClusterConfigDb } = await import('../config/cluster-config-db.ts');
      return ClusterConfigDb.open({
        clusterName: options.clusterName,
        awsRegion: options.awsRegion,
        client: new DynamoDBClient(await awsClientOptions(options.awsRegion, options.awsProfile)),
        dynamodbKmsKeyId: options.dynamodbKmsKeyId,
        createDatabase: options.createDatabase,
        logger: (message) => console.log(message),
      });
    },
    prefixList: {
      async getManagedPrefixListEntries(input) {
        const { EC2Client, GetManagedPrefixListEntriesCommand } = await import('@aws-sdk/client-ec2');
        const result = await new EC2Client(await awsClientOptions(region())).send(
          new GetManagedPrefixListEntriesCommand(input),
        );
        return {
          Entries: result.Entries?.map((entry) => ({ Cidr: entry.Cidr, Description: entry.Description })),
          NextToken: result.NextToken,
        };
      },
      async describeManagedPrefixLists(input) {
        const { DescribeManagedPrefixListsCommand, EC2Client } = await import('@aws-sdk/client-ec2');
        const result = await new EC2Client(await awsClientOptions(region())).send(
          new DescribeManagedPrefixListsCommand(input),
        );
        return { PrefixLists: result.PrefixLists?.map((list) => ({ Version: list.Version })) };
      },
      async modifyManagedPrefixList(input) {
        const { EC2Client, ModifyManagedPrefixListCommand } = await import('@aws-sdk/client-ec2');
        await new EC2Client(await awsClientOptions(region())).send(new ModifyManagedPrefixListCommand(input));
      },
    },
    certificates: {
      secrets: {
        async listSecretsByTagValue(tagKey, tagValues) {
          const { SecretsManagerClient, ListSecretsCommand } = await import('@aws-sdk/client-secrets-manager');
          const result = await new SecretsManagerClient(await awsClientOptions(region())).send(
            new ListSecretsCommand({
              Filters: [
                { Key: 'tag-key', Values: [tagKey] },
                { Key: 'tag-value', Values: tagValues },
              ],
            }),
          );
          return (result.SecretList ?? []).map((secret) => ({ Name: secret.Name, ARN: secret.ARN }));
        },
        async createSecret(input) {
          const { SecretsManagerClient, CreateSecretCommand } = await import('@aws-sdk/client-secrets-manager');
          const result = await new SecretsManagerClient(await awsClientOptions(region())).send(
            new CreateSecretCommand(input),
          );
          if (result.ARN === undefined) throw new GeneralException(`secretsmanager:CreateSecret returned no ARN for ${input.Name}`);
          return result.ARN;
        },
      },
      acm: {
        async listIssuedCertificates() {
          const { ACMClient, ListCertificatesCommand } = await import('@aws-sdk/client-acm');
          const client = new ACMClient(await awsClientOptions(region()));
          const summaries: Array<{ DomainName?: string; CertificateArn?: string }> = [];
          let nextToken: string | undefined;
          do {
            const page = await client.send(new ListCertificatesCommand({ CertificateStatuses: ['ISSUED'], NextToken: nextToken }));
            for (const summary of page.CertificateSummaryList ?? []) {
              summaries.push({ DomainName: summary.DomainName, CertificateArn: summary.CertificateArn });
            }
            nextToken = page.NextToken;
          } while (nextToken !== undefined && nextToken !== '');
          return summaries;
        },
        async importCertificate(input) {
          const { ACMClient, ImportCertificateCommand } = await import('@aws-sdk/client-acm');
          // ACM takes the two PEMs as blobs; everything else in this path handles them as text.
          const pem = (value: string): Uint8Array => new TextEncoder().encode(value);
          const result = await new ACMClient(await awsClientOptions(region())).send(
            new ImportCertificateCommand({
              Certificate: pem(input.Certificate),
              PrivateKey: pem(input.PrivateKey),
              Tags: input.Tags,
            }),
          );
          if (result.CertificateArn === undefined) throw new GeneralException('acm:ImportCertificate returned no ARN');
          return result.CertificateArn;
        },
      },
      openssl: (args, cwd) => {
        const result = spawnSync('openssl', [...args], { cwd, encoding: 'utf-8' });
        return {
          status: result.status,
          stderr: result.stderr ?? '',
          missing: (result.error as { code?: string } | undefined)?.code === 'ENOENT',
        };
      },
    },
    callerIdentity,
    async accountId() {
      return (await callerIdentity({ awsRegion: region(), awsProfile: actionProfile })).account;
    },
    instanceProtection: {
      async isProtected(input) {
        const { DescribeInstanceAttributeCommand, EC2Client } = await import('@aws-sdk/client-ec2');
        const result = await new EC2Client(await awsClientOptions(input.awsRegion)).send(
          new DescribeInstanceAttributeCommand({ InstanceId: input.instanceId, Attribute: 'disableApiTermination' }),
        );
        return result.DisableApiTermination?.Value === true;
      },
      async setProtected(input) {
        const { EC2Client, ModifyInstanceAttributeCommand } = await import('@aws-sdk/client-ec2');
        await new EC2Client(await awsClientOptions(input.awsRegion)).send(
          new ModifyInstanceAttributeCommand({ InstanceId: input.instanceId, DisableApiTermination: { Value: input.protected } }),
        );
      },
    },
    httpStatus: liveHttpStatus,
    sleep,
    now: () => Date.now(),
    uuid: () => randomUUID(),
    out: (line) => console.log(line),
    err: (line) => console.error(red(line)),
    async prompt(choice: PromptChoice) {
      const rl = createInterface({ input: process.stdin, output: process.stdout });
      try {
        const suffix = choice.choices === undefined ? ' [Y/n] ' : ` [${choice.choices.join('/')}] `;
        const answer = (await rl.question(`${choice.message}${suffix}`)).trim();
        if (choice.choices !== undefined) {
          const matched = choice.choices.find((option) => option.toLowerCase() === answer.toLowerCase());
          return matched ?? String(choice.default ?? choice.choices[0]);
        }
        if (answer === '') return choice.default !== false;
        return ['y', 'yes'].includes(answer.toLowerCase());
      } finally {
        rl.close();
      }
    },
  };
}

// ---------------------------------------------------------------------------------------------
// quick-setup
// ---------------------------------------------------------------------------------------------

export interface QuickSetupOptions {
  valuesFile?: string;
  existingResources?: boolean;
  terminationProtection?: string;
  deploymentId?: string;
  optimizeDeployment?: boolean;
  force?: boolean;
  skipConfig?: boolean;
  rollback?: boolean;
  moduleSet: string;
  allowReplacement?: string[];
}

/** `quick_setup` (`app_main.py:1644-1795`), step for step. */
export async function quickSetup(deps: Deps, options: QuickSetupOptions): Promise<void> {
  let values: Record<string, unknown>;
  if (options.skipConfig === true) {
    if (options.valuesFile === undefined) {
      deps.err('--values-file is required when --skip-config flag is provided.');
      throw new ExitWithCode(1);
    }
    values = loadValuesFile(options.valuesFile);
  } else {
    deps.out(`ideactl ${ideaVersion()}`);
    values = await configGenerate(deps, {
      valuesFile: options.valuesFile,
      existingResources: options.existingResources,
      force: options.force,
    });
  }

  const clusterName = String(values['cluster_name'] ?? '');
  const awsRegion = String(values['aws_region'] ?? '');
  const awsProfile = values['aws_profile'] === undefined ? undefined : String(values['aws_profile']);
  selectActionProfile(awsProfile);
  // quick-setup takes its region from the values file rather than from an option, so the hook has
  // nothing to bind and the clients below would have no region.
  selectActionRegion(awsRegion);
  if (deps.callerIdentity !== undefined) {
    const identity = await deps.callerIdentity({ awsRegion, awsProfile });
    deps.out(formatAwsIdentity(identity, awsProfile));
  }

  if (options.skipConfig !== true) {
    await configUpdate(deps, {
      clusterName,
      awsRegion,
      awsProfile,
      moduleSet: options.moduleSet,
      force: options.force,
    });
  }

  const settings = await scanSettings(deps, clusterName);
  deps.out(
    renderTable(['Key', 'Value', 'Version'], settings.map((row) => [row.key, pyStr(row.value), String(row.version ?? 0)])),
  );

  let config = await ClusterConfig.fromDynamoDb(clusterName, awsRegion, {
    moduleSet: options.moduleSet,
    scan: deps.scan,
  });
  deps.out(modulesTable(config.modules()));

  if (options.force !== true) {
    const confirm = await deps.prompt({
      message: 'Are you sure you want to deploy above IDEA modules with applicable configuration settings?',
      default: true,
    });
    if (confirm !== true && confirm !== 'Yes') {
      deps.out('Deployment aborted!');
      throw new ExitWithCode(0);
    }
  }

  await runBootstrap(deps, {
    clusterName,
    awsRegion,
    awsProfile,
    terminationProtection: options.terminationProtection,
    moduleSet: options.moduleSet,
  });

  const helper = await DeploymentHelper.open({
    clusterName,
    awsRegion,
    moduleSet: options.moduleSet,
    awsProfile,
    allModules: true,
    upgrade: false,
    forceBuildBootstrap: true,
    optimizeDeployment: options.optimizeDeployment === true,
    deploymentId: options.deploymentId,
    deps,
  });
  const moduleIds = helper.getDeploymentOrder();
  if (moduleIds.length === 0) {
    deps.out('all modules are already deployed. skipping deployment.');
  } else {
    const order = options.optimizeDeployment === true ? helper.getOptimizedDeploymentOrder() : moduleIds;
    deps.out(`deploying modules: ${JSON.stringify(order)}`);
    await runDeploy(deps, moduleIds, {
      clusterName,
      awsRegion,
      awsProfile,
      terminationProtection: options.terminationProtection,
      deploymentId: options.deploymentId,
      rollback: options.rollback,
      optimizeDeployment: options.optimizeDeployment,
      moduleSet: options.moduleSet,
      allowReplacement: options.allowReplacement,
    });
  }

  await checkClusterStatus(deps, {
    clusterName,
    awsRegion,
    awsProfile,
    wait: true,
    waitTimeout: 30 * 60,
    moduleSet: options.moduleSet,
  });

  config = await ClusterConfig.fromDynamoDb(clusterName, awsRegion, {
    moduleSet: options.moduleSet,
    scan: deps.scan,
  });
  deps.out(modulesTable(config.modules()));
  deps.out('--- Cluster Connection Info ---');
  for (const entry of connectionInfo(config, awsRegion)) deps.out(`${entry.key}: ${entry.value}`);
}

// ---------------------------------------------------------------------------------------------
// program
// ---------------------------------------------------------------------------------------------

export function buildProgram(deps: Deps): Command {
  // The shell wrapper this tool replaces defaults the security scan off for deploys, and the
// suppression metadata is emitted whether or not the aspect runs. Keep the same default so
// running from the image behaves like running from the wrapper.
process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN ??= 'false';

const program = new Command('ideactl')
    .description('IDEA cluster administration')
    .version(ideaVersion())
    .helpOption('-h, --help', 'display help for command')
    .showHelpAfterError()
    // Set before the subcommands are registered so every one of them inherits it: commander
    // copies settings at `.command()` time, and a subcommand that still calls `process.exit`
    // would take the process down before `run` could map the exit code.
    .exitOverride();

  program.hook("preAction", async (_command, actionCommand) => {
    const options = actionCommand.opts();
    const selected = typeof options.awsProfile === "string" ? options.awsProfile : undefined;
    selectActionProfile(selected);

    const optionRegion = options.awsRegion;
    const positionalRegion =
      actionCommand.name() === "check-aws-services" ? actionCommand.args[0] : undefined;
    const awsRegion =
      typeof optionRegion === "string"
        ? optionRegion
        : typeof positionalRegion === "string"
          ? positionalRegion
          : undefined;
    selectActionRegion(awsRegion);
    const offlineReplay = actionCommand.name() === "cdk-app" &&
      typeof options.configFile === "string" && typeof options.synthReads === "string";
    if (!offlineReplay && awsRegion !== undefined && deps.callerIdentity !== undefined) {
      const identity = await deps.callerIdentity({ awsRegion, awsProfile: selected });
      deps.out(formatAwsIdentity(identity, selected));
    }
  });

  program
    .command('about')
    .description('print the release version')
    .option('--no-banner', 'print the version without the banner')
    .action(() => {
      deps.out(`ideactl ${ideaVersion()}`);
    });

  program
    .command('quick-setup-help')
    .description('display quick-setup help')
    .action(() => {
      deps.out(readFileSync(resourcePath('config/values.yml'), 'utf-8'));
    });

  program
    .command('quick-setup')
    .description('Install a new cluster')
    .option('--values-file <values-file>', 'path to values.yml file')
    .option('--existing-resources', 'Install IDEA using existing resources')
    .option('--termination-protection <termination-protection>', 'enable/disable termination protection for all stacks', 'true')
    .option('--deployment-id <deployment-id>', 'Deployment Id')
    .option('--optimize-deployment', 'Deploy applicable stacks in parallel.')
    .option('--force', 'Skip all confirmation prompts')
    .option(
      '--skip-config',
      'Skip config generation and update steps. --values-file is required when this flag is provided.',
    )
    .option('--rollback', 'Rollback stack to stable state on failure. Default.', true)
    .option('--no-rollback', 'Do not roll back on failure, to iterate more rapidly.')
    .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
    .option(
      '--allow-replacement <logical-id>',
      'Accept a change-set entry the deploy guard would refuse, by logical ID. Repeatable.',
      (value: string, previous: string[] = []) => [...previous, value],
    )
    .action(async (options: QuickSetupOptions) => {
      await quickSetup(deps, options);
    });

  registerConfigCommands(program, deps);
  registerCdkCommands(program, deps);
  registerDeployCommands(program, deps);
  registerCostCollectorCommands(program, deps);
  registerReplaceCommands(program, deps);
  registerStatusCommands(program, deps);
  registerUpgradeCommands(program, createLiveUpgradeDeps(deps));
  registerMigrateCommands(program, createLiveMigrateDeps(deps, environmentRegion));
  registerDeleteClusterCommands(program, createLiveDeleteClusterDeps(deps));
  registerRemainingOperatorCommands(program, createLiveRemainingOperatorDeps(deps));

  return program;
}

/**
 * CLI exit behaviour: with no arguments print help and exit 0. Operator errors print one red
 * line and exit 1. A stack is printed only when IDEA_DEBUG=1.
 */
export async function run(argv: string[] = process.argv.slice(2), deps: Deps = liveDeps()): Promise<number> {
  const program = buildProgram(deps);
  try {
    await program.parseAsync(argv.length === 0 ? ['--help'] : argv, { from: 'user' });
    return 0;
  } catch (error) {
    if (error instanceof ExitWithCode) {
      if (error.message !== '') deps.err(error.message);
      return error.code;
    }
    if (isCommanderExit(error)) return (error as { exitCode: number }).exitCode;
    deps.err(operatorMessage(error, argv));
    if (process.env.IDEA_DEBUG === '1') console.error(error);
    return 1;
  }
}

function isCommanderExit(error: unknown): boolean {
  return (error as { code?: string })?.code?.startsWith('commander.') === true;
}

/** One operator-facing sentence for a caught failure. */
function operatorMessage(error: unknown, argv: string[]): string {
  if (isClusterConfigNotInitialized(error)) {
    return formatUninitialisedCluster(error as Error, argv);
  }
  if (isResourceNotFound(error)) {
    return formatMissingTables(argv);
  }
  if (error instanceof ConfigKeyNotFound) {
    return formatConfigKeyNotFound(error, argv);
  }
  if (error instanceof AwsProfileCredentialsError) {
    return error.message;
  }
  if (isCredentialsError(error)) {
    return formatCredentialsError(error as Error, argv);
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

function isClusterConfigNotInitialized(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === 'ClusterConfigDbError' ||
      error.message.startsWith('Configuration tables not found for cluster'))
  );
}

function isResourceNotFound(error: unknown): boolean {
  return (error as { name?: string })?.name === 'ResourceNotFoundException';
}

function isCredentialsError(error: unknown): boolean {
  const name = (error as { name?: string })?.name ?? '';
  const message = (error as Error)?.message ?? '';
  return (
    name === 'CredentialsProviderError' ||
    /Profile .* (could not be found|not found)/i.test(message) ||
    /Could not resolve credentials using profile:/i.test(message)
  );
}

function flagValue(argv: string[], flag: string): string | undefined {
  const index = argv.indexOf(flag);
  if (index === -1) return undefined;
  const value = argv[index + 1];
  if (value === undefined || value.startsWith('-')) return undefined;
  return value;
}

function formatUninitialisedCluster(error: Error, argv: string[]): string {
  if (error.message.includes('Create them with ideactl config update')) return error.message;
  const cluster =
    error.message.match(/cluster:\s*(\S+)/)?.[1] ??
    flagValue(argv, '--cluster-name') ??
    'the cluster';
  const region =
    flagValue(argv, '--aws-region') ?? process.env.AWS_DEFAULT_REGION ?? process.env.AWS_REGION ?? '';
  if (region === '') {
    return (
      `Configuration tables not found for cluster ${cluster}. Create them with ideactl config update ` +
      `--cluster-name ${cluster} --aws-region <region>, or confirm the cluster was installed in this account and region.`
    );
  }
  return (
    `Configuration tables not found for cluster ${cluster} in ${region}. Create them with ideactl config update ` +
    `--cluster-name ${cluster} --aws-region ${region}, or confirm the cluster was installed in this account and region.`
  );
}

function formatMissingTables(argv: string[]): string {
  const cluster = flagValue(argv, '--cluster-name') ?? '<cluster>';
  const region =
    flagValue(argv, '--aws-region') ?? process.env.AWS_DEFAULT_REGION ?? process.env.AWS_REGION ?? '<region>';
  return (
    `No configuration tables for cluster ${cluster} in ${region} (looked for ${cluster}.modules and ${cluster}.cluster-settings). ` +
    `Install with ideactl quick-setup, or run ideactl config update --cluster-name ${cluster} --aws-region ${region}. ` +
    'If the cluster already exists, check --aws-region and --aws-profile.'
  );
}

function formatConfigKeyNotFound(error: ConfigKeyNotFound, argv: string[]): string {
  const key = error.message.match(/, key:\s*(.+)$/)?.[1] ?? error.message;
  const cluster = flagValue(argv, '--cluster-name') ?? '<cluster>';
  const region = flagValue(argv, '--aws-region') ?? '<region>';
  return (
    `Configuration key ${key} is missing for this cluster. Show nearby keys with ideactl config show ` +
    `--cluster-name ${cluster} --aws-region ${region}, or set it with ideactl config set.`
  );
}

function formatCredentialsError(error: Error, argv: string[]): string {
  const profileFromSdk = error.message.match(/profile:\s*\[([^\]]+)\]/i)?.[1];
  const profile =
    profileFromSdk ?? actionProfile ?? flagValue(argv, '--aws-profile') ?? process.env.AWS_PROFILE;
  if (
    profile !== undefined &&
    profile !== '' &&
    (/Could not resolve credentials using profile:/i.test(error.message) ||
      /Profile .* (could not be found|not found)/i.test(error.message))
  ) {
    return (
      `AWS profile ${profile} was not found in the shared config/credentials files. ` +
      'Create the profile, or pass an existing name with --aws-profile. AWS_PROFILE is also read.'
    );
  }
  const region =
    flagValue(argv, '--aws-region') ?? process.env.AWS_DEFAULT_REGION ?? process.env.AWS_REGION ?? '';
  if (region === '') {
    return 'No AWS credentials were loaded. Export keys, start a federated session, or pass --aws-profile. Then retry.';
  }
  return (
    `No AWS credentials were loaded for region ${region}. Export keys, start a federated session, or pass --aws-profile. Then retry.`
  );
}

const entryPoint = process.argv[1];
const isMainModule = entryPoint !== undefined && import.meta.url === pathToFileURL(entryPoint).href;

if (isMainModule) {
  run().then(
    (code) => {
      if (code !== 0) process.exitCode = code;
    },
    (error: unknown) => {
      console.error(error);
      process.exitCode = 1;
    },
  );
}
