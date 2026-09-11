/**
 * Checks docs/COMMANDS.md against the live command tree and against
 * `node dist/src/cli/main.js <command> --help`.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import type { Command } from "commander";

import { buildProgram } from "../../src/cli/main.ts";
import type { Deps } from "../../src/cli/cdk-invoker.ts";

const PKG = join(dirname(fileURLToPath(import.meta.url)), "../..");
const COMMANDS_MD = join(PKG, "docs/COMMANDS.md");
const DIST_MAIN = join(PKG, "dist/src/cli/main.js");

function stubDeps(): Deps {
  return {
    spawn: async () => 0,
    scan: async () => ({}),
    cfn: {
      describeChangeSet: async () => ({}),
      executeChangeSet: async () => {},
      describeStack: async () => ({}),
    },
    s3: {
      putObject: async () => {},
      getObject: async () => "",
    },
    configWriter: async () => ({
      syncModulesInDb: async () => {},
      syncClusterSettingsInDb: async () => {},
      setConfigEntry: async () => {},
      deleteConfigEntries: async () => {},
    }),
    accountId: async () => "123456789012",
    httpStatus: async () => 200,
    sleep: async () => {},
    now: () => Date.now(),
    uuid: () => "00000000-0000-0000-0000-000000000000",
    out: () => {},
    err: () => {},
    prompt: async () => true,
  };
}

interface DocumentedCommand {
  name: string;
  flags: string[];
}

/** Pulls each `## \`command\`` section and the Flag column of its options table. */
function parseCommandsDoc(markdown: string): Map<string, DocumentedCommand> {
  const documented = new Map<string, DocumentedCommand>();
  const sections = markdown.split(/^## /m).slice(1);
  for (const section of sections) {
    const firstLine = section.split("\n")[0]?.trim() ?? "";
    const heading = firstLine.match(/^`([^`]+)`/);
    if (heading === null) continue;
    const name = heading[1];
    const flags: string[] = [];
    const tableMatch = section.match(/\| Flag \| Value \| Default \| Required \|\n\| --- \| --- \| --- \| --- \|\n([\s\S]*?)(?:\n\n|\n## |\n\*\*|$)/);
    if (tableMatch !== null && tableMatch[1] !== undefined) {
      for (const row of tableMatch[1].split("\n")) {
        const cell = row.match(/^\| `([^`]+)` \|/);
        if (cell !== null && cell[1] !== undefined) flags.push(cell[1]);
      }
    }
    documented.set(name, { name, flags });
  }
  return documented;
}

function walkCommands(command: Command, path: string[]): Array<{ name: string; flags: string[] }> {
  const name = path.length === 0 ? "ideactl" : path.join(" ");
  const flags = command.options.map((option) => option.flags);
  const result = [{ name, flags }];
  for (const child of command.commands) {
    result.push(...walkCommands(child, [...path, child.name()]));
  }
  return result;
}

/** Flag names from a COMMANDS.md Flag cell or a commander `option.flags` string. */
function flagTokens(flags: string): string[] {
  return flags
    .split(",")
    .map((part) => part.trim().split(/\s+/)[0] ?? "")
    .filter((token) => token.startsWith("-"));
}

/**
 * Flag names printed in the Options column of `--help`.
 * Descriptions are ignored so a mention of `--force` in another option's text does not count.
 */
function helpFlagTokens(help: string): Set<string> {
  const tokens = new Set<string>();
  const optionsStart = help.search(/^Options:/mu);
  const section = optionsStart === -1 ? help : help.slice(optionsStart);
  const nextHeading = section.slice(1).search(/\n\n|\nCommands:|\nArguments:/u);
  const body = nextHeading === -1 ? section : section.slice(0, nextHeading + 1);
  for (const line of body.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("-")) {
      continue;
    }
    const flagsColumn = trimmed.split(/\s{2,}/u)[0] ?? trimmed;
    for (const token of flagTokens(flagsColumn)) {
      tokens.add(token);
    }
  }
  return tokens;
}

function spawnHelp(args: string[]): { status: number | null; stdout: string; stderr: string } {
  const result = spawnSync(process.execPath, [DIST_MAIN, ...args, "--help"], {
    cwd: PKG,
    encoding: "utf8",
    env: { ...process.env, AWS_PROFILE: "", AWS_DEFAULT_PROFILE: "" },
  });
  return {
    status: result.status,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
  };
}

describe("COMMANDS.md matches --help option for option", () => {
  const markdown = readFileSync(COMMANDS_MD, "utf8");
  const documented = parseCommandsDoc(markdown);
  const program = buildProgram(stubDeps());
  const registered = walkCommands(program, []);

  it("documents every registered command", () => {
    const missing = registered.map((entry) => entry.name).filter((name) => !documented.has(name));
    assert.deepEqual(missing, [], `COMMANDS.md missing sections: ${missing.join(", ")}`);
  });

  for (const entry of registered) {
    it(`${entry.name} option flags match the implementation`, () => {
      const doc = documented.get(entry.name);
      assert.ok(doc !== undefined, `no COMMANDS.md section for ${entry.name}`);
      assert.deepEqual(doc.flags, entry.flags, `${entry.name} flags`);
    });
  }

  it("the built binary exists", () => {
    assert.equal(existsSync(DIST_MAIN), true, `missing ${DIST_MAIN}; run npm run build`);
  });

  for (const entry of registered) {
    it(`dist --help for ${entry.name} lists every documented flag`, () => {
      const doc = documented.get(entry.name);
      assert.ok(doc !== undefined, `no COMMANDS.md section for ${entry.name}`);
      const args = entry.name === "ideactl" ? [] : entry.name.split(" ");
      const help = spawnHelp(args);
      assert.equal(help.status, 0, `${entry.name} --help exited ${help.status}\n${help.stderr}`);
      const listed = helpFlagTokens(`${help.stdout}\n${help.stderr}`);
      for (const flags of doc.flags) {
        for (const token of flagTokens(flags)) {
          assert.equal(
            listed.has(token),
            true,
            `${entry.name} --help missing ${token} (from ${flags}); listed: ${[...listed].join(" ")}`,
          );
        }
      }
    });
  }
});
