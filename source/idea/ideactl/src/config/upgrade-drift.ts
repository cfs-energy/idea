/**
 * Compare the settings table with every configuration write planned by an upgrade.
 *
 * The comparison reports the write classes used by the upgrade analysis. Values are
 * deliberately excluded from rendered output so configuration secrets cannot be printed.
 */

import { isDeepStrictEqual } from "node:util";

import type { ConfigEntry } from "./generator.ts";

/** Stable display order for the upgrade action classes. */
export const UPGRADE_DRIFT_ACTIONS = [
  "GLOBAL_CHANGE",
  "GLOBAL_TYPE_CHANGE",
  "GLOBAL_REMOVE",
  "GLOBAL_REWRITE_SAME",
  "ADD",
  "PRESERVE_DRIFT",
  "PRESERVE_TYPE_DRIFT",
  "ORPHAN_PRESERVED",
  "PHASE3_OVERWRITE",
  "STACK_OVERWRITE",
  "STACK_DELETE",
] as const;

export type UpgradeDriftAction = (typeof UPGRADE_DRIFT_ACTIONS)[number];
export type UpgradeDriftEffect = "ADD" | "CHANGE" | "DELETE" | "PRESERVE";

/** One current table row, including the metadata the preview is allowed to print. */
export interface CurrentConfigRow extends ConfigEntry {
  source?: string;
  version?: number;
}

/**
 * Settings owned by one stack.
 *
 * `previous` is the settings map in the deployed custom resource. `target` is the
 * settings map that the selected deployment will submit. A missing target means the
 * caller knows ownership but cannot resolve the future values or removed keys.
 */
export interface StackSettingsPlan {
  moduleId: string;
  selected: boolean;
  previous: Readonly<Record<string, unknown>>;
  target?: Readonly<Record<string, unknown>>;
}

/** Complete, read-only input needed to model an upgrade. */
export interface UpgradeDriftInput {
  current: readonly CurrentConfigRow[];
  generated: readonly ConfigEntry[];
  phase3?: readonly ConfigEntry[];
  stacks?: readonly StackSettingsPlan[];
  replaceGlobalSettings?: boolean;
  syncFullConfiguration?: boolean;
}

/** One affected key. The actual values are retained only in the caller's input. */
export interface UpgradeDriftFinding {
  action: UpgradeDriftAction;
  effect: UpgradeDriftEffect;
  key: string;
  currentType: string;
  targetType: string;
  source?: string;
  version?: number;
  differsFromGenerated: boolean;
}

/** Findings are sorted by action and then key for deterministic terminal output. */
export interface UpgradeDriftReport {
  findings: UpgradeDriftFinding[];
  totals: Readonly<Record<UpgradeDriftEffect, number>>;
  changedRowsDifferingFromGenerated: string[];
}

interface ExpandedStackPlan {
  selected: boolean;
  previous: Map<string, ConfigEntry>;
  target?: Map<string, ConfigEntry>;
}

/** Return the DynamoDB document type represented by a JavaScript value. */
export function dynamoValueType(value: unknown): string {
  if (value === undefined || value === null) return "NULL";
  if (typeof value === "string") return "S";
  if (typeof value === "number" || typeof value === "bigint") return "N";
  if (typeof value === "boolean") return "BOOL";
  if (value instanceof Uint8Array) return "B";
  if (Array.isArray(value)) return "L";
  if (value instanceof Set) {
    const values = [...value];
    if (values.every((entry) => typeof entry === "string")) return "SS";
    if (values.every((entry) => typeof entry === "number" || typeof entry === "bigint")) return "NS";
    if (values.every((entry) => entry instanceof Uint8Array)) return "BS";
    return "SET";
  }
  if (typeof value === "object") return "M";
  throw new TypeError(`Unsupported configuration value type: ${typeof value}`);
}

/** Build a unique key map and reject malformed snapshots before comparing them. */
function keyedRows<T extends ConfigEntry>(rows: readonly T[], label: string): Map<string, T> {
  const result = new Map<string, T>();
  for (const row of rows) {
    if (typeof row.key !== "string" || row.key.trim() === "") {
      throw new TypeError(`${label} contains an empty configuration key`);
    }
    if (result.has(row.key)) {
      throw new TypeError(`${label} contains duplicate configuration key: ${row.key}`);
    }
    result.set(row.key, row);
  }
  return result;
}

/** Expand stack-relative setting names into full table keys. */
function expandStackPlans(plans: readonly StackSettingsPlan[]): {
  plans: ExpandedStackPlan[];
  ownedKeys: Set<string>;
} {
  const expanded: ExpandedStackPlan[] = [];
  const ownedKeys = new Set<string>();

  for (const plan of plans) {
    if (plan.moduleId.trim() === "" || plan.moduleId.includes(".")) {
      throw new TypeError(`Invalid stack module id: ${plan.moduleId}`);
    }
    const expand = (settings: Readonly<Record<string, unknown>>, label: string): Map<string, ConfigEntry> => {
      const entries = Object.entries(settings).map(([key, value]) => {
        if (key.trim() === "") throw new TypeError(`${label} contains an empty setting key`);
        return { key: `${plan.moduleId}.${key}`, value };
      });
      return keyedRows(entries, label);
    };
    const previous = expand(plan.previous, `${plan.moduleId} previous stack settings`);
    const target =
      plan.target === undefined
        ? undefined
        : expand(plan.target, `${plan.moduleId} target stack settings`);
    for (const key of previous.keys()) ownedKeys.add(key);
    for (const key of target?.keys() ?? []) ownedKeys.add(key);
    expanded.push({ selected: plan.selected, previous, target });
  }

  return { plans: expanded, ownedKeys };
}

/** True when the table row differs in value or DynamoDB type from generated configuration. */
function differsFromGenerated(
  current: CurrentConfigRow | undefined,
  generated: ConfigEntry | undefined,
): boolean {
  return (
    current !== undefined &&
    generated !== undefined &&
    (dynamoValueType(current.value) !== dynamoValueType(generated.value) ||
      !isDeepStrictEqual(current.value, generated.value))
  );
}

/** Construct one finding without exposing either value. */
function finding(
  action: UpgradeDriftAction,
  effect: UpgradeDriftEffect,
  key: string,
  current: CurrentConfigRow | undefined,
  generated: ConfigEntry | undefined,
  target: ConfigEntry | undefined,
  targetKnown: boolean,
): UpgradeDriftFinding {
  return {
    action,
    effect,
    key,
    currentType: current === undefined ? "-" : dynamoValueType(current.value),
    targetType: targetKnown ? (target === undefined ? "-" : dynamoValueType(target.value)) : "?",
    source: current?.source,
    version: current?.version,
    // A row whose future value is unknown cannot be claimed to differ from anything. The writer
    // there is a stack, not the generator, and a value a stack resolves at deploy time is normally
    // absent from generated configuration, so comparing the two flags every such row as an operator
    // edit at risk. The row still appears in the report, with `?` as its target type.
    differsFromGenerated: targetKnown && differsFromGenerated(current, generated),
  };
}

/** Compare one generated key using the add-only sync policy. */
function addOnlyFinding(
  key: string,
  current: CurrentConfigRow | undefined,
  generated: ConfigEntry,
): UpgradeDriftFinding | undefined {
  if (current === undefined) {
    return finding("ADD", "ADD", key, undefined, generated, generated, true);
  }
  if (dynamoValueType(current.value) !== dynamoValueType(generated.value)) {
    return finding("PRESERVE_TYPE_DRIFT", "PRESERVE", key, current, generated, generated, true);
  }
  if (!isDeepStrictEqual(current.value, generated.value)) {
    return finding("PRESERVE_DRIFT", "PRESERVE", key, current, generated, generated, true);
  }
  return undefined;
}

/**
 * Apply the upgrade policies in their real precedence order.
 *
 * Baseline generated settings are classified first. Phase 3 replaces that result
 * for its fixed keys, then a selected stack replaces it again because deployment
 * is the final writer.
 */
export function compareUpgradeDrift(input: UpgradeDriftInput): UpgradeDriftReport {
  const current = keyedRows(input.current, "current settings");
  const generated = keyedRows(input.generated, "generated settings");
  const phase3 = keyedRows(input.phase3 ?? [], "Phase 3 settings");
  const { plans: stacks, ownedKeys } = expandStackPlans(input.stacks ?? []);
  const findings = new Map<string, UpgradeDriftFinding>();
  const replaceGlobals = input.replaceGlobalSettings !== false;
  const syncFull = input.syncFullConfiguration !== false;

  // Global rows are replaced wholesale unless the upgrade explicitly skips that phase.
  if (replaceGlobals) {
    const globalKeys = new Set(
      [...current.keys(), ...generated.keys()].filter((key) => key.startsWith("global-settings.")),
    );
    for (const key of [...globalKeys].sort()) {
      const currentRow = current.get(key);
      const generatedRow = generated.get(key);
      if (currentRow === undefined && generatedRow !== undefined) {
        findings.set(key, finding("ADD", "ADD", key, undefined, generatedRow, generatedRow, true));
      } else if (currentRow !== undefined && generatedRow === undefined) {
        findings.set(
          key,
          finding("GLOBAL_REMOVE", "DELETE", key, currentRow, undefined, undefined, true),
        );
      } else if (currentRow !== undefined && generatedRow !== undefined) {
        const typeChanged = dynamoValueType(currentRow.value) !== dynamoValueType(generatedRow.value);
        const valueChanged = !isDeepStrictEqual(currentRow.value, generatedRow.value);
        const action: UpgradeDriftAction = typeChanged
          ? "GLOBAL_TYPE_CHANGE"
          : valueChanged
            ? "GLOBAL_CHANGE"
            : "GLOBAL_REWRITE_SAME";
        findings.set(
          key,
          finding(action, "CHANGE", key, currentRow, generatedRow, generatedRow, true),
        );
      }
    }
  }

  // The full sync is add-only. Existing values and existing types win.
  if (syncFull) {
    for (const [key, generatedRow] of generated) {
      if (replaceGlobals && key.startsWith("global-settings.")) continue;
      const result = addOnlyFinding(key, current.get(key), generatedRow);
      if (result !== undefined) findings.set(key, result);
    }
  }

  // Non-generated, non-stack rows survive because the full sync never deletes them.
  for (const [key, currentRow] of current) {
    if (generated.has(key) || ownedKeys.has(key)) continue;
    if (replaceGlobals && key.startsWith("global-settings.")) continue;
    findings.set(
      key,
      finding("ORPHAN_PRESERVED", "PRESERVE", key, currentRow, undefined, undefined, true),
    );
  }

  // Fixed operating-system, image, and default-type writes occur after generated sync.
  for (const [key, target] of phase3) {
    findings.set(
      key,
      finding(
        "PHASE3_OVERWRITE",
        current.has(key) ? "CHANGE" : "ADD",
        key,
        current.get(key),
        generated.get(key),
        target,
        true,
      ),
    );
  }

  // Selected stack writes are last. They can overwrite, add, or remove their owned rows.
  const selectedStackKeys = new Set<string>();
  for (const stack of stacks) {
    if (!stack.selected) continue;
    const targetRows = stack.target ?? stack.previous;
    for (const [key, target] of targetRows) {
      if (selectedStackKeys.has(key)) throw new TypeError(`Multiple selected stacks own configuration key: ${key}`);
      selectedStackKeys.add(key);
      findings.set(
        key,
        finding(
          "STACK_OVERWRITE",
          current.has(key) ? "CHANGE" : "ADD",
          key,
          current.get(key),
          generated.get(key),
          stack.target === undefined ? undefined : target,
          stack.target !== undefined,
        ),
      );
    }
    if (stack.target === undefined) continue;
    for (const key of stack.previous.keys()) {
      if (stack.target.has(key) || !current.has(key)) continue;
      if (selectedStackKeys.has(key)) throw new TypeError(`Multiple selected stacks own configuration key: ${key}`);
      selectedStackKeys.add(key);
      findings.set(
        key,
        finding("STACK_DELETE", "DELETE", key, current.get(key), generated.get(key), undefined, true),
      );
    }
  }

  const actionOrder = new Map(UPGRADE_DRIFT_ACTIONS.map((action, index) => [action, index]));
  const sorted = [...findings.values()].sort(
    (left, right) =>
      (actionOrder.get(left.action) ?? 0) - (actionOrder.get(right.action) ?? 0) ||
      left.key.localeCompare(right.key),
  );
  const totals: Record<UpgradeDriftEffect, number> = {
    ADD: 0,
    CHANGE: 0,
    DELETE: 0,
    PRESERVE: 0,
  };
  for (const row of sorted) totals[row.effect] += 1;

  return {
    findings: sorted,
    totals,
    changedRowsDifferingFromGenerated: sorted
      .filter((row) => row.effect === "CHANGE" && row.differsFromGenerated)
      .map((row) => row.key),
  };
}

/** Render a compact, value-free report grouped by upgrade action class. */
export function renderUpgradeDrift(report: UpgradeDriftReport): string {
  const lines = [
    "Configuration preview",
    `  Rows: ${report.totals.ADD} added, ${report.totals.CHANGE} changed, ${report.totals.DELETE} deleted, ${report.totals.PRESERVE} preserved`,
  ];

  for (const action of UPGRADE_DRIFT_ACTIONS) {
    const rows = report.findings.filter((row) => row.action === action);
    if (rows.length === 0) continue;
    const effect = rows[0]?.effect ?? "PRESERVE";
    lines.push(`  ${action.padEnd(21)} ${effect.padEnd(8)} ${String(rows.length).padStart(3)}`);
    for (const row of rows) {
      const metadata = [
        `${row.currentType} -> ${row.targetType}`,
        `source=${row.source ?? "-"}`,
        `version=${row.version ?? "-"}`,
      ].join(", ");
      const marker = row.effect === "CHANGE" && row.differsFromGenerated
        ? " [differs from generated configuration]"
        : "";
      lines.push(`    ${row.key} (${metadata})${marker}`);
    }
  }

  if (report.findings.length === 0) lines.push("  No changes or preserved drift detected.");
  lines.push(
    `  Potential operator edits overwritten, changed rows differing from generated configuration: ${
      report.changedRowsDifferingFromGenerated.length === 0
        ? "none"
        : report.changedRowsDifferingFromGenerated.join(", ")
    }`,
  );
  lines.push("  Values are hidden. The source marker does not identify the last writer.");
  return lines.join("\n");
}
