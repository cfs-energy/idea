/** Build the deployment support package from local artifacts and a configuration snapshot. */

import { cpSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import * as yaml from "js-yaml";
import { Command } from "commander";

import { clusterCdkDir, clusterDeploymentsDir, clusterRegionDir } from "../cdk-invoker.ts";

export const PACKAGE_DEPLOYMENT_LOGS = "deployment-logs";
export const PACKAGE_DEPLOYMENTS_DIR = "deployments-dir";
export const PACKAGE_CDK_CONFIG = "cdk-config";
export const PACKAGE_VALUES_FILE = "config-values-file";
export const PACKAGE_CLUSTER_CONFIG_DB = "cluster-config-db";
export const PACKAGE_CLUSTER_CONFIG_LOCAL = "cluster-config-local";

export interface SupportDeps {
  databaseConfig?: () => Promise<{ configYaml: string; modulesYaml: string }>;
  now(): Date;
  chooseContents?: () => Promise<string[]>;
  archive(directory: string): Promise<string>;
  out(line: string): void;
}

/** Builds dependencies for the profile selected by one support command. */
export type SupportDepsFactory = (options: SupportOptions) => Promise<SupportDeps>;

type SupportDepsSource = SupportDeps | SupportDepsFactory;

export interface SupportOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  moduleSet?: string;
}

function timestamp(now: Date): string {
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}_${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`;
}

function copyIfPresent(source: string, destination: string): void {
  if (existsSync(source)) cpSync(source, destination, { recursive: true });
}

/**
 * Copy the requested diagnostics and create an archive through the injected archiver.
 * The archiver stays injected because the runtime image determines its available utility.
 */
export async function buildDeploymentSupportPackage(
  deps: SupportDeps,
  options: SupportOptions,
  contents: readonly string[],
): Promise<string> {
  const regionDir = clusterRegionDir(options.clusterName, options.awsRegion, false);
  const packageDir = join(regionDir, "support", `idea-deployment-debug-pkg-${timestamp(deps.now())}`);
  mkdirSync(packageDir, { recursive: true });
  writeFileSync(join(packageDir, "package.yml"), yaml.dump({
    type: "deployment-debug",
    created_on: deps.now().toISOString(),
    options: { package_contents: [...contents] },
  }, { noRefs: true, sortKeys: false }));
  if (contents.includes(PACKAGE_DEPLOYMENT_LOGS)) {
    const logsDir = join(regionDir, "logs");
    deps.out(`copying deployment logs: ${logsDir} ...`);
    copyIfPresent(logsDir, join(packageDir, "logs"));
  }
  if (contents.includes(PACKAGE_CDK_CONFIG)) {
    const cdkDir = clusterCdkDir(options.clusterName, options.awsRegion);
    deps.out(`copying cdk config: ${cdkDir} ...`);
    copyIfPresent(cdkDir, join(packageDir, "_cdk"));
  }
  if (contents.includes(PACKAGE_DEPLOYMENTS_DIR)) {
    const deployments = clusterDeploymentsDir(options.clusterName, options.awsRegion);
    deps.out(`copying deployments: ${deployments} ...`);
    copyIfPresent(deployments, join(packageDir, "deployments"));
  }
  if (contents.includes(PACKAGE_VALUES_FILE)) {
    copyIfPresent(join(regionDir, "values.yml"), join(packageDir, "values.yml"));
  }
  if (contents.includes(PACKAGE_CLUSTER_CONFIG_LOCAL)) {
    copyIfPresent(join(regionDir, "config"), join(packageDir, "config_local"));
  }
  if (contents.includes(PACKAGE_CLUSTER_CONFIG_DB) && deps.databaseConfig !== undefined) {
    const dbDir = join(packageDir, "config_db");
    mkdirSync(dbDir, { recursive: true });
    const dump = await deps.databaseConfig();
    writeFileSync(join(dbDir, "config.yml"), dump.configYaml);
    writeFileSync(join(dbDir, "modules.yml"), dump.modulesYaml);
  }
  return deps.archive(packageDir);
}

/** Register the `support deployment` command. */
export function registerSupportCommands(program: Command, deps: SupportDepsSource): Command {
  const resolveDeps = async (options: SupportOptions): Promise<SupportDeps> => typeof deps === "function" ? deps(options) : deps;
  const group = program.command("support").description("support options");
  group.command("deployment")
    .requiredOption("--cluster-name <cluster-name>")
    .requiredOption("--aws-region <aws-region>")
    .option("--aws-profile <aws-profile>")
    .option("--module-set <module-set>", "Name of the ModuleSet. Default: default", "default")
    .action(async (options: SupportOptions) => {
      const actionDeps = await resolveDeps(options);
      const defaults = [PACKAGE_DEPLOYMENT_LOGS, PACKAGE_VALUES_FILE, PACKAGE_CLUSTER_CONFIG_DB, PACKAGE_CLUSTER_CONFIG_LOCAL];
      const contents = actionDeps.chooseContents === undefined ? defaults : await actionDeps.chooseContents();
      const file = await buildDeploymentSupportPackage(actionDeps, options, contents);
      actionDeps.out(`Debug Package: ${file}`);
    });
  return group;
}
