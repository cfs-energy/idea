import assert from "node:assert/strict";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { STSClient } from "@aws-sdk/client-sts";

import {
  AwsProfileCredentialsError,
  awsClientOptions,
  formatAwsIdentity,
} from "../../src/cli/aws-client-options.ts";
import { buildProgram, run } from "../../src/cli/main.ts";
import { fakeDeps } from "../w20a/harness.ts";

const REGION = "us-east-2";
const temporaryDirectories: string[] = [];

/** Restore one environment variable without converting an absent value to a string. */
function restoreEnvironment(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

/** Remove temporary shared configuration files after every credential resolution check. */
afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("different named profiles reach service clients as different credentials", async () => {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-profile-"));
  temporaryDirectories.push(directory);
  const credentialsFile = join(directory, "credentials");
  const configFile = join(directory, "config");
  writeFileSync(
    credentialsFile,
    [
      "[profile-one]",
      "aws_access_key_id = profile-one-access",
      "aws_secret_access_key = profile-one-secret",
      "[profile-two]",
      "aws_access_key_id = profile-two-access",
      "aws_secret_access_key = profile-two-secret",
      "",
    ].join("\n"),
  );
  writeFileSync(configFile, "");

  const previousCredentialsFile = process.env.AWS_SHARED_CREDENTIALS_FILE;
  const previousConfigFile = process.env.AWS_CONFIG_FILE;
  const previousProfile = process.env.AWS_PROFILE;
  process.env.AWS_SHARED_CREDENTIALS_FILE = credentialsFile;
  process.env.AWS_CONFIG_FILE = configFile;

  const first = new STSClient(await awsClientOptions(REGION, "profile-one"));
  const second = new STSClient(await awsClientOptions(REGION, "profile-two"));
  process.env.AWS_PROFILE = "profile-two";
  const environmentSelected = new STSClient(await awsClientOptions(REGION));
  try {
    const firstCredentials = await first.config.credentials();
    const secondCredentials = await second.config.credentials();
    const environmentCredentials = await environmentSelected.config.credentials();
    assert.equal(firstCredentials.accessKeyId, "profile-one-access");
    assert.equal(secondCredentials.accessKeyId, "profile-two-access");
    assert.equal(environmentCredentials.accessKeyId, "profile-two-access");
    assert.notEqual(firstCredentials.accessKeyId, secondCredentials.accessKeyId);
  } finally {
    first.destroy();
    second.destroy();
    environmentSelected.destroy();
    restoreEnvironment("AWS_SHARED_CREDENTIALS_FILE", previousCredentialsFile);
    restoreEnvironment("AWS_CONFIG_FILE", previousConfigFile);
    restoreEnvironment("AWS_PROFILE", previousProfile);
  }
});

test("an unusable profile fails by name instead of using ambient credentials", async () => {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-profile-"));
  temporaryDirectories.push(directory);
  const credentialsFile = join(directory, "credentials");
  const configFile = join(directory, "config");
  writeFileSync(credentialsFile, "");
  writeFileSync(configFile, "");

  const previous = {
    credentialsFile: process.env.AWS_SHARED_CREDENTIALS_FILE,
    configFile: process.env.AWS_CONFIG_FILE,
    accessKeyId: process.env.AWS_ACCESS_KEY_ID,
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
  };
  process.env.AWS_SHARED_CREDENTIALS_FILE = credentialsFile;
  process.env.AWS_CONFIG_FILE = configFile;
  process.env.AWS_ACCESS_KEY_ID = "ambient-access";
  process.env.AWS_SECRET_ACCESS_KEY = "ambient-secret";

  const client = new STSClient(await awsClientOptions(REGION, "missing-profile"));
  try {
    await assert.rejects(
      client.config.credentials(),
      (error: unknown) => {
        assert.ok(error instanceof AwsProfileCredentialsError);
        assert.equal(error.profile, "missing-profile");
        assert.match(
          error.message,
          /AWS profile missing-profile was not found in the shared config\/credentials files/,
        );
        return true;
      },
    );
  } finally {
    client.destroy();
    restoreEnvironment("AWS_SHARED_CREDENTIALS_FILE", previous.credentialsFile);
    restoreEnvironment("AWS_CONFIG_FILE", previous.configFile);
    restoreEnvironment("AWS_ACCESS_KEY_ID", previous.accessKeyId);
    restoreEnvironment("AWS_SECRET_ACCESS_KEY", previous.secretAccessKey);
  }
});

test("capture surfaces account and identity before its first account data read", () => {
  const identity = formatAwsIdentity(
    {
      account: "123456789012",
      arn: "arn:aws:sts::123456789012:assumed-role/sample-role/sample-session",
    },
    "profile-one",
  );
  assert.equal(
    identity,
    "AWS identity: account 123456789012, identity arn:aws:sts::123456789012:assumed-role/sample-role/sample-session, profile profile-one",
  );

  const source = readFileSync(
    new URL("../../tools/parity/capture.ts", import.meta.url),
    "utf8",
  );
  const identityRead = source.indexOf("const identity = await live.callerIdentity();");
  const firstAccountDataRead = source.indexOf("const items = await scan(");
  assert.notEqual(identityRead, -1);
  assert.notEqual(firstAccountDataRead, -1);
  assert.ok(identityRead < firstAccountDataRead);
  assert.match(source, /console\.log\(formatAwsIdentity\(identity, profile\)\)/);
});

test("an account command reports its resolved identity before the action runs", async () => {
  const previousProfile = process.env.AWS_PROFILE;
  const trace: string[] = [];
  const deps = fakeDeps();
  deps.callerIdentity = async (options) => {
    trace.push("identity");
    assert.deepEqual(options, {
      awsRegion: REGION,
      awsProfile: "profile-one",
    });
    return {
      account: "123456789012",
      arn: "arn:aws:sts::123456789012:assumed-role/sample-role/sample-session",
    };
  };
  deps.scan = async () => {
    trace.push("action");
    return { Items: [] };
  };

  try {
    const program = buildProgram(deps);
    await program.parseAsync(
      [
        "config",
        "show",
        "--cluster-name",
        "sample-cluster",
        "--aws-region",
        REGION,
        "--aws-profile",
        "profile-one",
      ],
      { from: "user" },
    );
    assert.deepEqual(trace, ["identity", "action"]);
    assert.equal(
      deps.stdout[0],
      "AWS identity: account 123456789012, identity arn:aws:sts::123456789012:assumed-role/sample-role/sample-session, profile profile-one",
    );
  } finally {
    restoreEnvironment("AWS_PROFILE", previousProfile);
  }
});

test("an unusable command profile stops before the action and is reported", async () => {
  const previousProfile = process.env.AWS_PROFILE;
  let actionRan = false;
  const deps = fakeDeps();
  deps.callerIdentity = async () => {
    throw new AwsProfileCredentialsError(
      "missing-profile",
      new Error("profile is unavailable"),
    );
  };
  deps.scan = async () => {
    actionRan = true;
    return { Items: [] };
  };

  try {
    const exitCode = await run(
      [
        "config",
        "show",
        "--cluster-name",
        "sample-cluster",
        "--aws-region",
        REGION,
        "--aws-profile",
        "missing-profile",
      ],
      deps,
    );
    assert.equal(exitCode, 1);
    assert.equal(actionRan, false);
    assert.deepEqual(deps.stderr, [
      "AWS profile missing-profile was not found in the shared config/credentials files. Create the profile, or pass an existing name with --aws-profile. AWS_PROFILE is also read.",
    ]);
  } finally {
    restoreEnvironment("AWS_PROFILE", previousProfile);
  }
});

test("every profile-aware client construction uses supported credentials or environment selection", () => {
  const root = new URL("../../", import.meta.url);
  const capture = readFileSync(new URL("tools/parity/capture.ts", root), "utf8");
  const synthReads = readFileSync(new URL("src/cdk/synth-reads.ts", root), "utf8");
  const main = readFileSync(new URL("src/cli/main.ts", root), "utf8");
  const adapters = readFileSync(new URL("src/cli/live-operator-adapters.ts", root), "utf8");
  const deploy = readFileSync(new URL("src/cli/commands/deploy.ts", root), "utf8");
  const upgrade = readFileSync(new URL("src/cli/commands/upgrade.ts", root), "utf8");
  const invoker = readFileSync(new URL("src/cli/cdk-invoker.ts", root), "utf8");

  assert.doesNotMatch(capture, /\bconfig\.profile\b/);
  assert.doesNotMatch(synthReads, /\bconfig\.profile\b/);
  assert.match(capture, /awsClientOptions\(region, profile\)/);
  assert.match(synthReads, /awsClientOptions\(region, profile\)/);
  assert.match(main, /awsClientOptions\(options\.awsRegion, options\.awsProfile\)/);
  assert.match(adapters, /awsClientOptions\(requiredRegion\(options\), options\.awsProfile\)/);
  assert.doesNotMatch(upgrade, /new \w+Client\(\{ region: input\.awsRegion \}\)/);
  assert.match(upgrade, /new EC2Client\(await awsClientOptions\(input\.awsRegion\)\)/);
  assert.match(deploy, /AWS_PROFILE: options\.awsProfile/);
  assert.match(invoker, /env\.AWS_PROFILE = this\.awsProfile/);
});
