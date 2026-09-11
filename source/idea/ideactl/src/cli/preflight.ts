/**
 * Collect and run read-only checks before a command can mutate a cluster.
 */

/** Severity displayed with each pre-flight result. */
export type PreflightSeverity = "error" | "warning";

/** Cluster and command values shared by every pre-flight check. */
export interface PreflightContext {
  command: string;
  account: string;
  region: string;
  cluster: string;
  profile?: string;
}

/** The result returned by one pre-flight predicate. */
export type PreflightOutcome =
  | { passed: true }
  | { passed: false; message: string };

/** One named check and the commands that require it. */
export interface PreflightCheck {
  name: string;
  description: string;
  severity: PreflightSeverity;
  commands: readonly string[];
  run(context: Readonly<PreflightContext>): Promise<PreflightOutcome>;
}

/** One completed check in a grouped report. */
export interface PreflightResult {
  name: string;
  description: string;
  severity: PreflightSeverity;
  passed: boolean;
  message?: string;
}

/** All checks selected for one command invocation. */
export interface PreflightReport {
  context: Readonly<PreflightContext>;
  results: readonly PreflightResult[];
  passed: boolean;
}

/** Read-only predicate used by checks with a boolean result. */
export type BooleanPreflightProbe = (
  context: Readonly<PreflightContext>,
) => Promise<boolean>;

/** Target-template comparison evidence supplied by the parity adapter. */
export interface TemplateComparisonEvidence {
  matches: boolean;
  remedyCommand: string;
}

/** Drift evidence supplied by the configuration preview adapter. */
export interface ConfigurationDriftEvidence {
  reportHash: string;
  lossKeys: readonly string[];
  acceptedReportHash?: string;
}

const MIGRATION_COMMANDS = ["upgrade-cluster", "migrate"] as const;
const ECS_COMMANDS = ["deploy", ...MIGRATION_COMMANDS] as const;

/** Reject empty identifiers before they enter operator-facing output. */
function requireText(value: string, label: string): void {
  if (value.trim() === "") {
    throw new TypeError(`${label} must not be empty`);
  }
}

/** Validate a check when it is registered so runner failures stay actionable. */
function validateCheck(check: PreflightCheck): void {
  requireText(check.name, "Pre-flight check name");
  requireText(check.description, `Description for ${check.name}`);
  if (check.severity !== "error" && check.severity !== "warning") {
    throw new TypeError(`Invalid severity for pre-flight check ${check.name}`);
  }
  if (check.commands.length === 0) {
    throw new TypeError(`Pre-flight check ${check.name} must name at least one command`);
  }
  for (const command of check.commands) {
    requireText(command, `Command for ${check.name}`);
  }
}

/** Validate invocation values before checks use them in probes or messages. */
function validateContext(context: Readonly<PreflightContext>): void {
  requireText(context.command, "Pre-flight command");
  requireText(context.account, "Account");
  requireText(context.region, "Region");
  requireText(context.cluster, "Cluster");
  if (context.profile !== undefined) {
    requireText(context.profile, "Profile");
  }
}

/** Convert an unexpected probe failure into an operator-facing refusal. */
function probeFailure(check: PreflightCheck, context: Readonly<PreflightContext>, error: unknown): PreflightResult {
  const detail = error instanceof Error ? error.message : String(error);
  return {
    name: check.name,
    description: check.description,
    severity: check.severity,
    passed: false,
    message: `Could not evaluate ${check.name} for account ${context.account} in region ${context.region}: ${detail}. Resolve the read failure, then rerun ${context.command}.`,
  };
}

/**
 * Registry for checks contributed by command and feature owners.
 *
 * Registration order is preserved in reports.
 */
export class PreflightRegistry {
  readonly #checks = new Map<string, PreflightCheck>();

  /** Add one uniquely named check. */
  register(check: PreflightCheck): this {
    validateCheck(check);
    if (this.#checks.has(check.name)) {
      throw new TypeError(`Duplicate pre-flight check name: ${check.name}`);
    }
    this.#checks.set(check.name, check);
    return this;
  }

  /** Return checks required by the named command. */
  forCommand(command: string): readonly PreflightCheck[] {
    requireText(command, "Pre-flight command");
    return [...this.#checks.values()].filter((check) => check.commands.includes(command));
  }
}

/**
 * Run every relevant check and retain every failure in one report.
 */
export async function runPreflight(
  registry: PreflightRegistry,
  context: Readonly<PreflightContext>,
): Promise<PreflightReport> {
  validateContext(context);
  const checks = registry.forCommand(context.command);

  // All probes are read-only, so one failed predicate must not suppress its peers.
  const results = await Promise.all(
    checks.map(async (check): Promise<PreflightResult> => {
      try {
        const outcome = await check.run(context);
        if (outcome.passed) {
          return {
            name: check.name,
            description: check.description,
            severity: check.severity,
            passed: true,
          };
        }
        requireText(outcome.message, `Failure message for ${check.name}`);
        return {
          name: check.name,
          description: check.description,
          severity: check.severity,
          passed: false,
          message: outcome.message,
        };
      } catch (error) {
        return probeFailure(check, context, error);
      }
    }),
  );

  return {
    context: { ...context },
    results,
    passed: results.every((result) => result.passed),
  };
}

/** Render a compact grouped report suitable for terminal output or a journal. */
export function renderPreflightReport(report: Readonly<PreflightReport>): string {
  const failed = report.results.filter((result) => !result.passed).length;
  const lines = [
    `Pre-flight checks for ${report.context.command} on ${report.context.cluster}, account ${report.context.account}, region ${report.context.region}`,
  ];

  for (const result of report.results) {
    lines.push(`${result.passed ? "PASS" : "FAIL"} [${result.severity}] ${result.name}: ${result.description}`);
    if (result.message !== undefined) {
      lines.push(...result.message.split("\n").map((line) => `  ${line}`));
    }
  }
  lines.push(`${report.passed ? "PASS" : "FAIL"}: ${report.results.length - failed} passed, ${failed} failed`);
  return lines.join("\n");
}

/** Build the exact one-time command that enables task network interface trunking. */
export function awsvpcTrunkingRemedy(context: Readonly<PreflightContext>): string {
  return [
    "aws",
    "ecs",
    "put-account-setting-default",
    "--name",
    "awsvpcTrunking",
    "--value",
    "enabled",
    "--region",
    context.region,
    ...(context.profile === undefined ? [] : ["--profile", context.profile]),
  ].join(" ");
}

/** Create the account-wide task network interface trunking prerequisite. */
export function createAwsvpcTrunkingCheck(
  probe: BooleanPreflightProbe,
  commands: readonly string[] = ECS_COMMANDS,
): PreflightCheck {
  return {
    name: "awsvpc-trunking",
    description: "Account-wide task network interface trunking is enabled",
    severity: "error",
    commands,
    async run(context): Promise<PreflightOutcome> {
      if (await probe(context)) {
        return { passed: true };
      }
      return {
        passed: false,
        message: `Task network interface trunking is not enabled for account ${context.account} in region ${context.region}. Run this command once, then rerun ${context.command}: ${awsvpcTrunkingRemedy(context)}`,
      };
    },
  };
}

/** Create the target-cluster template comparison prerequisite. */
export function createTemplateComparisonCheck(
  probe: (context: Readonly<PreflightContext>) => Promise<TemplateComparisonEvidence>,
  commands: readonly string[] = MIGRATION_COMMANDS,
): PreflightCheck {
  return {
    name: "template-comparison",
    description: "Target release templates match the current cluster",
    severity: "error",
    commands,
    async run(context): Promise<PreflightOutcome> {
      const evidence = await probe(context);
      if (evidence.matches) {
        return { passed: true };
      }
      requireText(evidence.remedyCommand, "Template comparison remedy command");
      return {
        passed: false,
        message: `Template comparison is not green for cluster ${context.cluster}, account ${context.account}, region ${context.region}. Run ${evidence.remedyCommand}, resolve every reported difference, then rerun ${context.command}.`,
      };
    },
  };
}

/** Create the check that blocks only configuration values the run would lose. */
export function createConfigurationDriftCheck(
  probe: (context: Readonly<PreflightContext>) => Promise<ConfigurationDriftEvidence>,
  commands: readonly string[] = MIGRATION_COMMANDS,
): PreflightCheck {
  return {
    name: "configuration-drift",
    description: "Configuration writes will not lose unacknowledged operator edits",
    severity: "error",
    commands,
    async run(context): Promise<PreflightOutcome> {
      const evidence = await probe(context);
      for (const key of evidence.lossKeys) {
        requireText(key, "Configuration drift key");
      }
      if (evidence.lossKeys.length === 0) {
        return { passed: true };
      }
      requireText(evidence.reportHash, "Configuration drift report hash");
      if (evidence.acceptedReportHash === evidence.reportHash) {
        return { passed: true };
      }
      return {
        passed: false,
        message: `Configuration changes would lose operator edits for account ${context.account} in region ${context.region}: ${evidence.lossKeys.join(", ")}. Preserve those typed values, or rerun ${context.command} with --accept-drift ${evidence.reportHash}.`,
      };
    },
  };
}
