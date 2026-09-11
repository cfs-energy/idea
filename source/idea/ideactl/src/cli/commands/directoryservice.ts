/**
 * Directory service operator commands.
 *
 * Secret creation deliberately does not read the cluster configuration because
 * it is used before a cluster has been initialized.
 */

import { Command } from "commander";

import { ClusterConfigError, isEmpty } from "../../config/cluster-config.ts";

export interface DirectorySecretsApi {
  createSecret(input: {
    Name: string;
    Description: string;
    SecretString: string;
    Tags: Array<{ Key: string; Value: string }>;
    KmsKeyId?: string;
  }): Promise<{ ARN?: string }>;
}

export interface DirectoryServiceDeps {
  secrets: DirectorySecretsApi;
  promptCredentials?: (defaults: { purpose?: string; username?: string; password?: string }) => Promise<{
    purpose: string;
    username: string;
    password: string;
  }>;
  out(line: string): void;
}

/** Builds dependencies for the profile selected by one directory-service command. */
export type DirectoryServiceDepsFactory = (options: {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
}) => Promise<DirectoryServiceDeps>;

type DirectoryServiceDepsSource = DirectoryServiceDeps | DirectoryServiceDepsFactory;

export interface CreateServiceAccountSecretsOptions {
  clusterName: string;
  username?: string;
  password?: string;
  kmsKeyId?: string;
  purpose?: string;
}

/** Create the paired username and password secrets in a fixed order. */
export async function createServiceAccountSecrets(
  deps: DirectoryServiceDeps,
  options: CreateServiceAccountSecretsOptions,
): Promise<{ purpose: string; usernameSecretArn: string | undefined; passwordSecretArn: string | undefined }> {
  let { purpose, username, password } = options;
  if (isEmpty(username) || isEmpty(password)) {
    if (deps.promptCredentials === undefined) {
      throw new ClusterConfigError("username and password are required when interactive credentials are unavailable");
    }
    const entered = await deps.promptCredentials({ purpose, username, password });
    purpose = entered.purpose;
    username = entered.username;
    password = entered.password;
  }
  // A supplied credential pair with no purpose uses this literal in its secret name.
  const resolvedPurpose = purpose === undefined ? "None" : purpose;
  if (isEmpty(username) || isEmpty(password)) throw new ClusterConfigError("username and password are required");
  const resolvedUsername = username as string;
  const resolvedPassword = password as string;
  const tags = [
    { Key: "idea:ClusterName", Value: options.clusterName },
    { Key: "idea:ModuleName", Value: "directoryservice" },
    { Key: "idea:ModuleId", Value: "directoryservice" },
  ];
  const common = {
    Tags: tags,
    ...(isEmpty(options.kmsKeyId) ? {} : { KmsKeyId: options.kmsKeyId as string }),
  };
  const usernameSecret = await deps.secrets.createSecret({
    Name: `${options.clusterName}-directoryservice-${resolvedPurpose}-username`,
    Description: `DirectoryService ${resolvedPurpose} username, Cluster: ${options.clusterName}`,
    SecretString: resolvedUsername,
    ...common,
  });
  const passwordSecret = await deps.secrets.createSecret({
    Name: `${options.clusterName}-directoryservice-${resolvedPurpose}-password`,
    Description: `DirectoryService ${resolvedPurpose} password, Cluster: ${options.clusterName}`,
    SecretString: resolvedPassword,
    ...common,
  });
  return { purpose: resolvedPurpose, usernameSecretArn: usernameSecret.ARN, passwordSecretArn: passwordSecret.ARN };
}

/** Register the `directoryservice` command group. */
export function registerDirectoryServiceCommands(program: Command, deps: DirectoryServiceDepsSource): Command {
  const resolveDeps = async (options: CreateServiceAccountSecretsOptions & {
    awsRegion: string;
    awsProfile?: string;
  }): Promise<DirectoryServiceDeps> => typeof deps === "function" ? deps(options) : deps;
  const group = program.command("directoryservice").description("directory service commands");
  group.command("create-service-account-secrets")
    .requiredOption("--cluster-name <cluster-name>")
    .requiredOption("--aws-region <aws-region>")
    .option("--aws-profile <aws-profile>")
    .option("--username <username>")
    .option("--password <password>")
    .option("--kms-key-id <kms-key-id>")
    .option("--purpose <purpose>")
    .action(async (options: CreateServiceAccountSecretsOptions & { awsRegion: string; awsProfile?: string }) => {
      const actionDeps = await resolveDeps(options);
      const result = await createServiceAccountSecrets(actionDeps, options);
      actionDeps.out(`directory service ${result.purpose} secrets created successfully: `);
      actionDeps.out(`Account Purpose: ${result.purpose}`);
      actionDeps.out(`Username Secret ARN: ${result.usernameSecretArn}`);
      actionDeps.out(`Password Secret ARN: ${result.passwordSecretArn}`);
    });
  return group;
}
