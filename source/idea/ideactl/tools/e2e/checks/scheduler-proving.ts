import { spawn } from "node:child_process";

/** One completed local or remote command invocation. */
export interface CommandResult {
  exitCode: number;
  stderr: string;
  stdout: string;
}

/** Runs a command without a shell. */
export interface CommandRunner {
  run(executable: string, args: readonly string[]): Promise<CommandResult>;
}

/** Runs a command on one SSH-reachable host. */
export interface RemoteRunner {
  run(host: string, command: string): Promise<CommandResult>;
}

/** Options shared by scheduler proof checks that connect over SSH. */
export interface SshOptions {
  identityFile?: string;
  options: readonly string[];
  port?: number;
  user: string;
}

/** Runs a local command and collects both output streams. */
export function runCommand(executable: string, args: readonly string[]): Promise<CommandResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { shell: false });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => {
      resolve({
        exitCode: code ?? 1,
        stderr: Buffer.concat(stderr).toString("utf8"),
        stdout: Buffer.concat(stdout).toString("utf8"),
      });
    });
  });
}

/** Creates an SSH runner that adds only explicitly supplied connection options. */
export function createSshRunner(options: SshOptions, runner: CommandRunner = { run: runCommand }): RemoteRunner {
  return {
    async run(host: string, command: string): Promise<CommandResult> {
      const args = ["-o", "BatchMode=yes"];
      if (options.port !== undefined) {
        args.push("-p", String(options.port));
      }
      if (options.identityFile !== undefined) {
        args.push("-i", options.identityFile);
      }
      for (const option of options.options) {
        args.push("-o", option);
      }
      args.push(`${options.user}@${host}`, command);
      return runner.run("ssh", args);
    },
  };
}

/** Quotes one value for the POSIX shell used by the remote command. */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

/** Reads a required CLI value or reports the missing flag. */
export function requiredFlag(values: ReadonlyMap<string, string[]>, flag: string): string {
  const value = values.get(flag)?.[0];
  if (value === undefined || value.trim() === "") {
    throw new Error(`missing required --${flag}`);
  }
  return value;
}

/** Reads an optional CLI value that may be supplied once. */
export function optionalFlag(values: ReadonlyMap<string, string[]>, flag: string): string | undefined {
  const supplied = values.get(flag);
  if (supplied === undefined) {
    return undefined;
  }
  if (supplied.length !== 1 || supplied[0] === undefined || supplied[0].trim() === "") {
    throw new Error(`--${flag} must be supplied once with a non-empty value`);
  }
  return supplied[0];
}

/**
 * Parses `--flag value` arguments and rejects repeated flags unless declared repeatable.
 * It deliberately does not accept `--flag=value`, matching the rest of the E2E tools.
 */
export function parseValueFlags(
  argv: readonly string[],
  knownFlags: ReadonlySet<string>,
  repeatableFlags: ReadonlySet<string>,
  flagsWithOptionLikeValues: ReadonlySet<string> = new Set(),
): Map<string, string[]> {
  const values = new Map<string, string[]>();
  for (let index = 0; index < argv.length; index += 1) {
    const rawFlag = argv[index];
    if (!rawFlag.startsWith("--")) {
      throw new Error(`unexpected argument: ${rawFlag}`);
    }
    const flag = rawFlag.slice(2);
    if (!knownFlags.has(flag)) {
      throw new Error(`unknown flag: --${flag}`);
    }
    const value = argv[index + 1];
    if (value === undefined || (value.startsWith("--") && !flagsWithOptionLikeValues.has(flag))) {
      throw new Error(`missing value for --${flag}`);
    }
    const prior = values.get(flag) ?? [];
    if (!repeatableFlags.has(flag) && prior.length > 0) {
      throw new Error(`--${flag} may be supplied only once`);
    }
    prior.push(value);
    values.set(flag, prior);
    index += 1;
  }
  return values;
}
