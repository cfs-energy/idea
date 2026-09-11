/** Run the shipped module integration-test cases through an injected test runner. */

import { Command } from "commander";

import { ClusterConfig, GeneralException, isEmpty } from "../../config/cluster-config.ts";

export class IntegrationTestFailed extends Error {}

export interface IntegrationTestContext {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  adminUsername: string;
  adminPassword: string;
  debug: boolean;
  extraParams: Record<string, string>;
  testCaseIds: string[];
  moduleIds: string[];
}

export interface IntegrationTestCase {
  id: string;
  run(context: IntegrationTestContext): Promise<void>;
}

export interface IntegrationTestDeps {
  config: ClusterConfig;
  casesForModule(moduleName: string): readonly IntegrationTestCase[] | undefined;
  out(line: string): void;
  err(line: string): void;
}

/** Builds dependencies for the profile selected by one integration-test command. */
export type IntegrationTestDepsFactory = (options: RunIntegrationTestsOptions) => Promise<IntegrationTestDeps>;

type IntegrationTestDepsSource = IntegrationTestDeps | IntegrationTestDepsFactory;

export interface RunIntegrationTestsOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  adminUsername: string;
  adminPassword: string;
  testCaseId?: string;
  debug?: boolean;
  param?: string[];
  moduleSet?: string;
}

/** Python keeps the last value for a duplicate parameter and ignores tokens without `=`. */
export function parseIntegrationParams(params: readonly string[] = []): Record<string, string> {
  const result: Record<string, string> = {};
  for (const param of params) {
    const separator = param.indexOf("=");
    if (separator >= 0) result[param.slice(0, separator)] = param.slice(separator + 1);
  }
  return result;
}

function dedupe(moduleIds: readonly string[]): string[] {
  const result: string[] = [];
  for (const id of moduleIds) if (!result.includes(id)) result.push(id);
  return result;
}

/** Execute each selected test case and aggregate failures separately for each module. */
export async function runIntegrationTests(
  deps: IntegrationTestDeps,
  options: RunIntegrationTestsOptions,
  moduleIds: readonly string[],
): Promise<void> {
  const ids = dedupe(moduleIds);
  const context: IntegrationTestContext = {
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    awsProfile: options.awsProfile,
    adminUsername: options.adminUsername,
    adminPassword: options.adminPassword,
    debug: options.debug === true,
    extraParams: parseIntegrationParams(options.param),
    testCaseIds: isEmpty(options.testCaseId) ? [] : (options.testCaseId as string).split(","),
    moduleIds: ids,
  };
  for (const moduleId of ids) {
    const module = deps.config.moduleInfoById(moduleId);
    if (module === undefined) throw new GeneralException(`module not found for module id: ${moduleId}`);
    if (module.status !== "deployed") throw new GeneralException(`module id: ${moduleId} is not deployed yet.`);
    const testCases = deps.casesForModule(module.name);
    if (testCases === undefined) {
      deps.out(`no test cases found for module: ${module.name}`);
      continue;
    }
    let total = 0;
    let failures = 0;
    for (const testCase of testCases) {
      if (context.testCaseIds.length > 0 && !context.testCaseIds.includes(testCase.id)) continue;
      total += 1;
      deps.out(`${testCase.id}   [STARTED]`);
      try {
        await testCase.run(context);
        deps.out(`${testCase.id}   [PASS]`);
      } catch (error) {
        deps.err(String(error));
        deps.err(`${testCase.id}   [FAIL]`);
        failures += 1;
      }
    }
    if (failures > 0) {
      const passed = total - failures;
      const rate = Math.round((passed / total) * 10000) / 100;
      throw new IntegrationTestFailed(`${failures} of ${total} test cases failed. success rate: ${rate}%`);
    }
  }
}

/** Register `run-integration-tests`. */
export function registerIntegrationTestCommands(program: Command, deps: IntegrationTestDepsSource): Command {
  const resolveDeps = async (options: RunIntegrationTestsOptions): Promise<IntegrationTestDeps> =>
    typeof deps === "function" ? deps(options) : deps;
  return program.command("run-integration-tests")
    .requiredOption("--cluster-name <cluster-name>")
    .requiredOption("--aws-region <aws-region>")
    .option("--aws-profile <aws-profile>")
    .requiredOption("--admin-username <username>")
    .requiredOption("--admin-password <password>")
    .option("--test-case-id <test-case-id>")
    .option("--debug")
    .option("-p, --param <key=value>", "Additional test case parameter", (value: string, previous: string[] = []) => [...previous, value])
    .option("--module-set <module-set>", "Name of the ModuleSet. Default: default")
    .argument("<modules...>")
    .action(async (modules: string[], options: RunIntegrationTestsOptions) => {
      await runIntegrationTests(await resolveDeps(options), options, modules);
    });
}
