#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

import type { Argument, Command, Option as CommandOption } from "commander";

import { buildProgram, liveDeps } from "../../src/cli/main.ts";

export interface OptionSurface {
  key: string;
  spellings: string[];
  takesValue: boolean;
  required: boolean;
  defaultValue: string | null;
}

export interface ArgumentSurface {
  name: string;
  required: boolean;
  variadic: boolean;
  defaultValue: string | null;
}

export interface CommandSurface {
  path: string;
  options: OptionSurface[];
  arguments: ArgumentSurface[];
}

export interface CliSurface {
  commands: CommandSurface[];
  exitCodes: number[];
}

export type DifferenceClass = "original-only" | "new-only" | "different";

export interface Difference {
  id: string;
  class: DifferenceClass;
  description: string;
}

export interface EvaluatedDifference extends Difference {
  reason?: string;
}

export interface Evaluation {
  differences: EvaluatedDifference[];
  staleIntentions: string[];
}

interface DecoratedDefinition {
  functionName: string;
  decorators: string[];
}

interface ParsedDecorator {
  callee: string;
  positional: string[];
  keywords: Map<string, string>;
  source: string;
}

interface GroupDefinition {
  functionName: string;
  name: string;
  parentFunction?: string;
  decorators: string[];
  commandDecorator: ParsedDecorator;
}

interface ParsedOptionWithPolarity {
  surface: OptionSurface;
  positive: boolean;
}

const packageRoot = new URL("../../", import.meta.url);
const pythonSourceUrl = new URL(
  "../idea-administrator/src/ideaadministrator/app_main.py",
  packageRoot,
);
const newMainSourceUrl = new URL("../../src/cli/main.ts", import.meta.url);
const intentionsUrl = new URL("./intended-differences.json", import.meta.url);

/** Convert underscore-based function names to the command spelling used by the original parser. */
function commandName(functionName: string): string {
  return functionName.replaceAll("_", "-");
}

/** Split a call argument list without splitting nested calls, collections, or quoted strings. */
function splitTopLevel(source: string): string[] {
  const parts: string[] = [];
  let start = 0;
  let round = 0;
  let square = 0;
  let curly = 0;
  let quote: "'" | "\"" | undefined;
  let escaped = false;

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (quote !== undefined) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === quote) {
        quote = undefined;
      }
      continue;
    }
    if (character === "'" || character === "\"") {
      quote = character;
    } else if (character === "(") {
      round += 1;
    } else if (character === ")") {
      round -= 1;
    } else if (character === "[") {
      square += 1;
    } else if (character === "]") {
      square -= 1;
    } else if (character === "{") {
      curly += 1;
    } else if (character === "}") {
      curly -= 1;
    } else if (character === "," && round === 0 && square === 0 && curly === 0) {
      parts.push(source.slice(start, index).trim());
      start = index + 1;
    }
  }

  const finalPart = source.slice(start).trim();
  if (finalPart !== "") {
    parts.push(finalPart);
  }
  return parts;
}

/** Find a keyword separator that is not inside a string or nested expression. */
function topLevelEquals(source: string): number {
  let round = 0;
  let square = 0;
  let curly = 0;
  let quote: "'" | "\"" | undefined;
  let escaped = false;

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (quote !== undefined) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === quote) {
        quote = undefined;
      }
      continue;
    }
    if (character === "'" || character === "\"") {
      quote = character;
    } else if (character === "(") {
      round += 1;
    } else if (character === ")") {
      round -= 1;
    } else if (character === "[") {
      square += 1;
    } else if (character === "]") {
      square -= 1;
    } else if (character === "{") {
      curly += 1;
    } else if (character === "}") {
      curly -= 1;
    } else if (character === "=" && round === 0 && square === 0 && curly === 0) {
      return index;
    }
  }
  return -1;
}

/** Decode the simple quoted literals used for command and option declarations. */
function pythonString(source: string): string | undefined {
  const token = source.trim();
  if (token.length < 2) {
    return undefined;
  }
  const quote = token[0];
  if ((quote !== "'" && quote !== "\"") || token[token.length - 1] !== quote) {
    return undefined;
  }
  return token
    .slice(1, -1)
    .replaceAll(`\\${quote}`, quote)
    .replaceAll("\\\\", "\\")
    .replaceAll("\\n", "\n")
    .replaceAll("\\t", "\t");
}

/** Parse the decorator call shape needed by the source extractor. */
function parseDecorator(source: string): ParsedDecorator {
  const trimmed = source.trim();
  const match = /^@([A-Za-z_][\w.]*)\s*(?:\(([\s\S]*)\))?$/.exec(trimmed);
  if (match === null) {
    throw new Error(`Unsupported decorator declaration: ${trimmed}`);
  }
  const callee = match[1] ?? "";
  const body = match[2] ?? "";
  const positional: string[] = [];
  const keywords = new Map<string, string>();
  for (const part of splitTopLevel(body)) {
    const separator = topLevelEquals(part);
    if (separator < 0) {
      positional.push(part);
      continue;
    }
    const name = part.slice(0, separator).trim();
    const value = part.slice(separator + 1).trim();
    if (!/^[A-Za-z_]\w*$/.test(name)) {
      throw new Error(`Unsupported decorator keyword: ${part}`);
    }
    keywords.set(name, value);
  }
  return { callee, positional, keywords, source: trimmed };
}

/** Read one complete decorator, including declarations split over several lines. */
function readDecorator(lines: string[], start: number): { source: string; end: number } {
  let source = "";
  let depth = 0;
  let sawParenthesis = false;
  let quote: "'" | "\"" | undefined;
  let escaped = false;

  for (let lineIndex = start; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex] ?? "";
    source = source === "" ? line.trim() : `${source}\n${line.trim()}`;
    for (const character of line) {
      if (quote !== undefined) {
        if (escaped) {
          escaped = false;
        } else if (character === "\\") {
          escaped = true;
        } else if (character === quote) {
          quote = undefined;
        }
        continue;
      }
      if (character === "'" || character === "\"") {
        quote = character;
      } else if (character === "(") {
        sawParenthesis = true;
        depth += 1;
      } else if (character === ")") {
        depth -= 1;
        if (depth < 0) {
          throw new Error(`Unbalanced decorator at line ${start + 1}`);
        }
      }
    }
    if (!sawParenthesis || depth === 0) {
      return { source, end: lineIndex };
    }
  }
  throw new Error(`Unterminated decorator at line ${start + 1}`);
}

/** Associate each decorator block with the function it declares. */
function decoratedDefinitions(source: string): DecoratedDefinition[] {
  const lines = source.split(/\r?\n/u);
  const definitions: DecoratedDefinition[] = [];
  let pending: string[] = [];

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex] ?? "";
    if (line.trimStart().startsWith("@")) {
      const decorator = readDecorator(lines, lineIndex);
      pending.push(decorator.source);
      lineIndex = decorator.end;
      continue;
    }
    const functionMatch = /^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/.exec(line);
    if (functionMatch !== null && pending.length > 0) {
      definitions.push({
        functionName: functionMatch[1] ?? "",
        decorators: pending,
      });
      pending = [];
      continue;
    }
    if (line.trim() !== "" && !line.trimStart().startsWith("#")) {
      pending = [];
    }
  }
  return definitions;
}

/** Normalize declared scalar defaults so equivalent text and boolean defaults compare equally. */
function sourceDefault(source: string | undefined): string | null {
  if (source === undefined || source === "None") {
    return null;
  }
  const literal = pythonString(source);
  if (literal !== undefined) {
    return literal;
  }
  if (source === "True") {
    return "true";
  }
  if (source === "False") {
    return "false";
  }
  if (/^-?\d+(?:\.\d+)?$/u.test(source)) {
    return source;
  }
  return `expression:${source}`;
}

/** Read a simple documented default when command code applies it after parsing. */
function documentedDefault(source: string): string | null {
  const match = /\bDefaults?:\s*["']?([A-Za-z0-9_.-]+)/iu.exec(source);
  return match?.[1] ?? null;
}

/** Choose a stable option identity while preserving every accepted spelling separately. */
function optionKey(spellings: string[]): string {
  const long = spellings.filter((spelling) => spelling.startsWith("--"));
  const positive = long.find((spelling) => !spelling.startsWith("--no-"));
  return positive ?? long[0] ?? spellings[0] ?? "";
}

/** Extract one option from an original source decorator. */
function pythonOption(decorator: ParsedDecorator): OptionSurface {
  const declarations = decorator.positional
    .map((part) => pythonString(part))
    .filter((part): part is string => part?.startsWith("-") === true);
  if (declarations.length === 0) {
    throw new Error(`Option has no spelling: ${decorator.source}`);
  }
  const spellings = declarations.flatMap((declaration) => declaration.split("/")).sort();
  const isFlag = decorator.keywords.get("is_flag") === "True" || declarations.some((value) => value.includes("/"));
  const key = optionKey(spellings);
  let defaultValue = sourceDefault(decorator.keywords.get("default"));
  if (decorator.keywords.get("default") === undefined && isFlag) {
    defaultValue = "false";
  }
  if (decorator.keywords.get("default") === undefined && !isFlag) {
    defaultValue = documentedDefault(decorator.source);
  }
  return {
    key,
    spellings,
    takesValue: !isFlag,
    required: decorator.keywords.get("required") === "True",
    defaultValue,
  };
}

/** Extract one positional argument from an original source decorator. */
function pythonArgument(decorator: ParsedDecorator): ArgumentSurface {
  const name = pythonString(decorator.positional[0] ?? "");
  if (name === undefined) {
    throw new Error(`Argument has no name: ${decorator.source}`);
  }
  return {
    name,
    required: decorator.keywords.get("required") === "True",
    variadic: decorator.keywords.get("nargs") === "-1",
    defaultValue: sourceDefault(decorator.keywords.get("default")),
  };
}

/** Build the options and arguments declared on one original command function. */
function pythonParameters(decorators: string[], commandDecorator: ParsedDecorator): {
  options: OptionSurface[];
  arguments: ArgumentSurface[];
} {
  const parsed = decorators.map(parseDecorator);
  const options = parsed
    .filter((decorator) => decorator.callee === "click.option")
    .map(pythonOption);
  const argumentsList = parsed
    .filter((decorator) => decorator.callee === "click.argument")
    .map(pythonArgument);
  const hasShortHelp = commandDecorator.keywords.get("context_settings") === "CLICK_SETTINGS";
  options.push({
    key: "--help",
    spellings: hasShortHelp ? ["--help", "-h"] : ["--help"],
    takesValue: false,
    required: false,
    defaultValue: "false",
  });
  return {
    options: options.sort((left, right) => left.key.localeCompare(right.key)),
    arguments: argumentsList,
  };
}

/** Extract literal process exit codes from the original wrapper. */
function pythonExitCodes(source: string): number[] {
  const codes = new Set<number>();
  for (const match of source.matchAll(/raise\s+SystemExit(?:\(\s*(\d+)\s*\))?/gu)) {
    codes.add(match[1] === undefined ? 0 : Number.parseInt(match[1], 10));
  }
  for (const match of source.matchAll(/exit_code\s*=\s*(\d+)/gu)) {
    codes.add(Number.parseInt(match[1] ?? "", 10));
  }
  // A declared Click command reports malformed command usage with status 2.
  if (/@click\.(?:group|command|option|argument)\b/u.test(source)) {
    codes.add(2);
  }
  if (codes.size === 0) {
    throw new Error("No original exit codes found");
  }
  return [...codes].sort((left, right) => left - right);
}

/** Extract the complete original command tree from decorators and registrations. */
export function extractOriginalSurface(source: string): CliSurface {
  const definitions = decoratedDefinitions(source);
  const registrations = new Set(
    [...source.matchAll(/^\s*main\.add_command\(\s*([A-Za-z_]\w*)\s*\)/gmu)]
      .map((match) => match[1] ?? ""),
  );
  const groups = new Map<string, GroupDefinition>();

  for (const definition of definitions) {
    const parsed = definition.decorators.map(parseDecorator);
    const groupDecorator = parsed.find((decorator) => decorator.callee.endsWith(".group"));
    if (groupDecorator === undefined) {
      continue;
    }
    const receiver = groupDecorator.callee.slice(0, -".group".length);
    const explicitName = pythonString(groupDecorator.positional[0] ?? "");
    groups.set(definition.functionName, {
      functionName: definition.functionName,
      name: explicitName ?? commandName(definition.functionName),
      ...(receiver === "click" ? {} : { parentFunction: receiver }),
      decorators: definition.decorators,
      commandDecorator: groupDecorator,
    });
  }

  const resolving = new Set<string>();
  const resolveGroupPath = (functionName: string): string[] => {
    if (functionName === "main") {
      return [];
    }
    const group = groups.get(functionName);
    if (group === undefined) {
      throw new Error(`Unknown command group: ${functionName}`);
    }
    if (resolving.has(functionName)) {
      throw new Error(`Command group cycle: ${functionName}`);
    }
    resolving.add(functionName);
    let parentPath: string[];
    if (group.parentFunction !== undefined) {
      parentPath = resolveGroupPath(group.parentFunction);
    } else {
      if (!registrations.has(functionName)) {
        resolving.delete(functionName);
        throw new Error(`Unregistered command group: ${functionName}`);
      }
      parentPath = [];
    }
    resolving.delete(functionName);
    return [...parentPath, group.name];
  };

  const commands: CommandSurface[] = [];
  const mainDefinition = groups.get("main");
  if (mainDefinition === undefined) {
    throw new Error("Original root command declaration not found");
  }
  const rootParameters = pythonParameters(mainDefinition.decorators, mainDefinition.commandDecorator);
  rootParameters.options.push({
    key: "--version",
    spellings: ["--version"],
    takesValue: false,
    required: false,
    defaultValue: "false",
  });
  rootParameters.options.sort((left, right) => left.key.localeCompare(right.key));
  commands.push({ path: "", ...rootParameters });

  for (const group of groups.values()) {
    if (group.functionName === "main") {
      continue;
    }
    const path = resolveGroupPath(group.functionName).join(" ");
    const parameters = pythonParameters(group.decorators, group.commandDecorator);
    commands.push({ path, ...parameters });
  }

  for (const definition of definitions) {
    const parsed = definition.decorators.map(parseDecorator);
    const commandDecorator = parsed.find((decorator) => decorator.callee.endsWith(".command"));
    if (commandDecorator === undefined) {
      continue;
    }
    const receiver = commandDecorator.callee.slice(0, -".command".length);
    let parentPath: string[];
    if (receiver === "click") {
      if (!registrations.has(definition.functionName)) {
        continue;
      }
      parentPath = [];
    } else {
      parentPath = resolveGroupPath(receiver);
    }
    const explicitName = pythonString(commandDecorator.positional[0] ?? "");
    const name = explicitName ?? commandName(definition.functionName);
    const parameters = pythonParameters(definition.decorators, commandDecorator);
    commands.push({ path: [...parentPath, name].join(" "), ...parameters });
  }

  commands.sort((left, right) => left.path.localeCompare(right.path));
  const paths = new Set<string>();
  for (const command of commands) {
    if (paths.has(command.path)) {
      throw new Error(`Duplicate original command path: ${command.path}`);
    }
    paths.add(command.path);
  }
  return { commands, exitCodes: pythonExitCodes(source) };
}

/** Normalize a runtime option default to the same scalar representation as source literals. */
function runtimeDefault(value: unknown, takesValue: boolean, negate: boolean): string | null {
  if (value !== undefined) {
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
    throw new Error(`Unsupported command option default: ${JSON.stringify(value)}`);
  }
  if (!takesValue) {
    return negate ? "true" : "false";
  }
  return null;
}

/** Extract one runtime option before positive and negative spellings are merged. */
function runtimeOption(option: CommandOption): ParsedOptionWithPolarity {
  const spellings = [option.long, option.short]
    .filter((spelling): spelling is string => spelling !== undefined)
    .sort();
  if (spellings.length === 0) {
    throw new Error(`Option has no spelling: ${option.flags}`);
  }
  const takesValue = option.required || option.optional;
  const declaredDefault = runtimeDefault(option.defaultValue, takesValue, option.negate);
  return {
    surface: {
      key: optionKey(spellings),
      spellings,
      takesValue,
      required: option.mandatory,
      defaultValue: declaredDefault ?? documentedDefault(option.description),
    },
    positive: !option.negate,
  };
}

/** Merge separately declared positive and negative flags into one public option. */
function runtimeOptions(command: Command): OptionSurface[] {
  const parsed = command.createHelp().visibleOptions(command).map(runtimeOption);
  const positiveLongNames = new Set(
    parsed
      .filter((option) => option.positive)
      .flatMap((option) => option.surface.spellings)
      .filter((spelling) => spelling.startsWith("--")),
  );
  const merged = new Map<string, ParsedOptionWithPolarity>();

  for (const option of parsed) {
    const long = option.surface.spellings.find((spelling) => spelling.startsWith("--"));
    const positiveName = long?.startsWith("--no-") === true
      ? `--${long.slice("--no-".length)}`
      : undefined;
    const key = positiveName !== undefined && positiveLongNames.has(positiveName)
      ? positiveName
      : option.surface.key;
    const current = merged.get(key);
    if (current === undefined) {
      merged.set(key, {
        ...option,
        surface: { ...option.surface, key },
      });
      continue;
    }
    if (current.surface.takesValue !== option.surface.takesValue) {
      throw new Error(`Conflicting value arity for ${key} on ${command.name()}`);
    }
    merged.set(key, {
      positive: current.positive || option.positive,
      surface: {
        key,
        spellings: [...new Set([...current.surface.spellings, ...option.surface.spellings])].sort(),
        takesValue: current.surface.takesValue,
        required: current.surface.required || option.surface.required,
        defaultValue: option.positive ? option.surface.defaultValue : current.surface.defaultValue,
      },
    });
  }
  return [...merged.values()]
    .map((option) => option.surface)
    .sort((left, right) => left.key.localeCompare(right.key));
}

/** Extract one runtime positional argument. */
function runtimeArgument(argument: Argument): ArgumentSurface {
  const defaultValue = argument.defaultValue;
  if (
    defaultValue !== undefined &&
    typeof defaultValue !== "string" &&
    typeof defaultValue !== "number" &&
    typeof defaultValue !== "boolean"
  ) {
    throw new Error(`Unsupported command argument default: ${JSON.stringify(defaultValue)}`);
  }
  return {
    name: argument.name(),
    required: argument.required,
    variadic: argument.variadic,
    defaultValue: defaultValue === undefined ? null : String(defaultValue),
  };
}

/** Extract literal process exit codes from the new command wrapper source. */
function newExitCodes(source: string): number[] {
  const codes = new Set<number>();
  for (const match of source.matchAll(/\breturn\s+([01])\s*;/gu)) {
    codes.add(Number.parseInt(match[1] ?? "", 10));
  }
  for (const match of source.matchAll(/process\.exitCode\s*=\s*(\d+)/gu)) {
    codes.add(Number.parseInt(match[1] ?? "", 10));
  }
  if (codes.size === 0) {
    throw new Error("No new command exit codes found");
  }
  return [...codes].sort((left, right) => left - right);
}

/** Extract the complete new command tree from its registered command objects. */
export function extractNewSurface(program: Command, mainSource: string): CliSurface {
  const commands: CommandSurface[] = [];
  const visit = (command: Command, parentPath: string[], isRoot = false): void => {
    const pathParts = isRoot ? [] : [...parentPath, command.name()];
    commands.push({
      path: pathParts.join(" "),
      options: runtimeOptions(command),
      arguments: command.registeredArguments.map(runtimeArgument),
    });
    const childParent = isRoot ? [] : pathParts;
    for (const child of command.commands) {
      visit(child, childParent, false);
    }
  };
  visit(program, [], true);
  commands.sort((left, right) => left.path.localeCompare(right.path));
  return { commands, exitCodes: newExitCodes(mainSource) };
}

/** Render one option compactly for missing and added surface reports. */
function optionDescription(option: OptionSurface): string {
  return [
    option.spellings.join(","),
    `takesValue=${option.takesValue}`,
    `required=${option.required}`,
    `default=${JSON.stringify(option.defaultValue)}`,
  ].join(" ");
}

/** Compare two command surfaces without applying accepted-difference policy. */
export function compareSurfaces(original: CliSurface, current: CliSurface): Difference[] {
  const differences: Difference[] = [];
  const originalCommands = new Map(original.commands.map((command) => [command.path, command]));
  const currentCommands = new Map(current.commands.map((command) => [command.path, command]));

  for (const [path] of originalCommands) {
    if (!currentCommands.has(path)) {
      differences.push({
        id: `original-only:command:${path}`,
        class: "original-only",
        description: `command ${path === "" ? "<root>" : path}`,
      });
    }
  }
  for (const [path] of currentCommands) {
    if (!originalCommands.has(path)) {
      differences.push({
        id: `new-only:command:${path}`,
        class: "new-only",
        description: `command ${path === "" ? "<root>" : path}`,
      });
    }
  }

  for (const [path, originalCommand] of originalCommands) {
    const currentCommand = currentCommands.get(path);
    if (currentCommand === undefined) {
      continue;
    }
    const displayPath = path === "" ? "<root>" : path;
    const originalOptions = new Map(originalCommand.options.map((option) => [option.key, option]));
    const currentOptions = new Map(currentCommand.options.map((option) => [option.key, option]));
    for (const [key, option] of originalOptions) {
      const currentOption = currentOptions.get(key);
      if (currentOption === undefined) {
        differences.push({
          id: `original-only:option:${path}:${key}`,
          class: "original-only",
          description: `option ${displayPath} ${optionDescription(option)}`,
        });
        continue;
      }
      const changed: string[] = [];
      if (JSON.stringify(option.spellings) !== JSON.stringify(currentOption.spellings)) {
        changed.push(`spellings ${JSON.stringify(option.spellings)} -> ${JSON.stringify(currentOption.spellings)}`);
      }
      if (option.takesValue !== currentOption.takesValue) {
        changed.push(`takesValue ${option.takesValue} -> ${currentOption.takesValue}`);
      }
      if (option.required !== currentOption.required) {
        changed.push(`required ${option.required} -> ${currentOption.required}`);
      }
      if (option.defaultValue !== currentOption.defaultValue) {
        changed.push(`default ${JSON.stringify(option.defaultValue)} -> ${JSON.stringify(currentOption.defaultValue)}`);
      }
      if (changed.length > 0) {
        differences.push({
          id: `different:option:${path}:${key}`,
          class: "different",
          description: `option ${displayPath} ${key}: ${changed.join("; ")}`,
        });
      }
    }
    for (const [key, option] of currentOptions) {
      if (!originalOptions.has(key)) {
        differences.push({
          id: `new-only:option:${path}:${key}`,
          class: "new-only",
          description: `option ${displayPath} ${optionDescription(option)}`,
        });
      }
    }

    const argumentCount = Math.max(originalCommand.arguments.length, currentCommand.arguments.length);
    for (let index = 0; index < argumentCount; index += 1) {
      const originalArgument = originalCommand.arguments[index];
      const currentArgument = currentCommand.arguments[index];
      const position = index + 1;
      if (originalArgument === undefined && currentArgument !== undefined) {
        differences.push({
          id: `new-only:argument:${path}:${position}`,
          class: "new-only",
          description: `argument ${displayPath} #${position} ${JSON.stringify(currentArgument)}`,
        });
      } else if (originalArgument !== undefined && currentArgument === undefined) {
        differences.push({
          id: `original-only:argument:${path}:${position}`,
          class: "original-only",
          description: `argument ${displayPath} #${position} ${JSON.stringify(originalArgument)}`,
        });
      } else if (
        originalArgument !== undefined &&
        currentArgument !== undefined &&
        JSON.stringify(originalArgument) !== JSON.stringify(currentArgument)
      ) {
        differences.push({
          id: `different:argument:${path}:${position}`,
          class: "different",
          description: `argument ${displayPath} #${position}: ${JSON.stringify(originalArgument)} -> ${JSON.stringify(currentArgument)}`,
        });
      }
    }
  }

  if (JSON.stringify(original.exitCodes) !== JSON.stringify(current.exitCodes)) {
    differences.push({
      id: "different:exit-codes",
      class: "different",
      description: `exit codes ${JSON.stringify(original.exitCodes)} -> ${JSON.stringify(current.exitCodes)}`,
    });
  }

  return differences.sort((left, right) =>
    left.class.localeCompare(right.class) || left.id.localeCompare(right.id));
}

/** Parse and validate the committed reasons for accepted differences. */
export function parseIntentions(source: string): Record<string, string> {
  const parsed: unknown = JSON.parse(source);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Intended differences must be a JSON object");
  }
  const intentions: Record<string, string> = {};
  for (const [id, reason] of Object.entries(parsed)) {
    if (typeof reason !== "string" || reason.trim() === "") {
      throw new Error(`Intended difference ${id} must have a non-empty reason`);
    }
    intentions[id] = reason.trim();
  }
  return intentions;
}

/** Attach accepted reasons and identify policy entries that no longer match a real difference. */
export function evaluateDifferences(
  differences: Difference[],
  intentions: Record<string, string>,
): Evaluation {
  const seen = new Set(differences.map((difference) => difference.id));
  return {
    differences: differences.map((difference) => ({
      ...difference,
      ...(intentions[difference.id] === undefined ? {} : { reason: intentions[difference.id] }),
    })),
    staleIntentions: Object.keys(intentions).filter((id) => !seen.has(id)).sort(),
  };
}

/** Return zero only when every difference is explained and every explanation is still used. */
export function parityExitCode(evaluation: Evaluation): number {
  return evaluation.differences.some((difference) => difference.reason === undefined) ||
    evaluation.staleIntentions.length > 0
    ? 1
    : 0;
}

/** Print the required three difference classes and a deterministic summary. */
export function formatReport(evaluation: Evaluation): string {
  const sections: Array<{ title: string; class: DifferenceClass }> = [
    { title: "PRESENT_IN_ORIGINAL_AND_MISSING_HERE", class: "original-only" },
    { title: "PRESENT_HERE_AND_NOT_IN_ORIGINAL", class: "new-only" },
    { title: "PRESENT_IN_BOTH_BUT_DIFFERING", class: "different" },
  ];
  const lines = ["CLI_PARITY_REPORT"];
  for (const section of sections) {
    lines.push(section.title);
    const members = evaluation.differences.filter((difference) => difference.class === section.class);
    if (members.length === 0) {
      lines.push("(none)");
    } else {
      for (const difference of members) {
        const status = difference.reason === undefined
          ? "UNEXPLAINED"
          : `ACCEPTED: ${difference.reason}`;
        lines.push(`- ${difference.description} [${status}]`);
      }
    }
  }
  lines.push("STALE_INTENDED_DIFFERENCES");
  lines.push(...(evaluation.staleIntentions.length === 0
    ? ["(none)"]
    : evaluation.staleIntentions.map((id) => `- ${id}`)));
  const accepted = evaluation.differences.filter((difference) => difference.reason !== undefined).length;
  const unexplained = evaluation.differences.length - accepted;
  lines.push(
    `SUMMARY total=${evaluation.differences.length} accepted=${accepted} unexplained=${unexplained} stale=${evaluation.staleIntentions.length}`,
  );
  return lines.join("\n");
}

/** Read both implementations, compare them, and return the report plus its process status. */
export function runChecker(): { report: string; exitCode: number } {
  const originalSource = readFileSync(pythonSourceUrl, "utf8");
  const mainSource = readFileSync(newMainSourceUrl, "utf8");
  const original = extractOriginalSurface(originalSource);
  const current = extractNewSurface(buildProgram(liveDeps()), mainSource);
  const intentions = parseIntentions(readFileSync(intentionsUrl, "utf8"));
  const evaluation = evaluateDifferences(compareSurfaces(original, current), intentions);
  return {
    report: formatReport(evaluation),
    exitCode: parityExitCode(evaluation),
  };
}

const entryPoint = process.argv[1];
if (entryPoint !== undefined && import.meta.url === pathToFileURL(entryPoint).href) {
  try {
    const result = runChecker();
    console.log(result.report);
    process.exitCode = result.exitCode;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
