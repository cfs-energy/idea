/**
 * Every new cluster runs its control plane as container tasks. The installer asks no question
 * about it, and the values file it writes has to carry the key the generator splices the container
 * module in from. Without that, a newly generated module set contains no container module, the
 * account pre-flight is never reached, and nothing reads the image setting.
 *
 * The first check drives the real question flow through a driver so it fails if a shape question
 * reappears; the rest assert the generated result rather than the values map alone.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { flattenConfigDir, generateConfigFromTemplates } from "../../src/config/generator.ts";
import {
  collectInstallerValues,
  InstallerValidationError,
  type InstallerIdentity,
} from "../../src/cli/installer-params.ts";
import type { InstallerQuestion, PromptDriver } from "../../src/cli/prompts.ts";

const IDENTITY: InstallerIdentity = {
  accountId: "123456789012",
  partition: "aws",
  dnsSuffix: "amazonaws.com",
};

const MODULES = ["metrics", "scheduler", "virtual-desktop-controller"];
const work: string[] = [];

after(() => {
  for (const dir of work) rmSync(dir, { recursive: true, force: true });
});

/** Answers from a fixed map and keeps every question it was asked. */
class RecordingDriver implements PromptDriver {
  readonly questions: InstallerQuestion[] = [];
  readonly messages: string[] = [];
  readonly #answers: Record<string, unknown>;

  constructor(answers: Record<string, unknown>) {
    this.#answers = answers;
  }

  async ask(question: InstallerQuestion): Promise<unknown> {
    this.questions.push(question);
    return Object.hasOwn(this.#answers, question.name)
      ? this.#answers[question.name]
      : question.defaultValue;
  }

  report(message: string): void {
    this.messages.push(message);
  }
}

function answers(modules: readonly string[] = MODULES): Record<string, unknown> {
  return {
    aws_profile: "default",
    aws_partition: "aws",
    aws_region: "us-east-2",
    cluster_name: "sample",
    administrator_email: "admin@example.invalid",
    vpc_cidr_block: "192.0.2.0/24",
    ssh_key_pair_name: "sample-key",
    cluster_access: "client-ip",
    client_ip: "192.0.2.10",
    alb_public: true,
    use_vpc_endpoints: false,
    directory_service_provider: "aws_managed_activedirectory",
    enable_aws_backup: true,
    kms_key_type: "aws-managed",
    enabled_modules: [...modules],
    metrics_provider: "cloudwatch",
    base_os: "amazonlinux2023",
    instance_type: "m7i.large",
    volume_size: 200,
  };
}

/** The interactive path: every answer arrives through the driver. */
async function interactiveInstall(): Promise<{
  values: Record<string, unknown>;
  driver: RecordingDriver;
}> {
  const driver = new RecordingDriver(answers());
  const values = await collectInstallerValues({ driver, identity: async () => IDENTITY });
  return { values, driver };
}

/** The scripted path: a bad answer is raised rather than re-asked. */
function scriptedInstall(modules: readonly string[]): Promise<Record<string, unknown>> {
  return collectInstallerValues({
    answers: answers(modules),
    driver: new RecordingDriver({}),
    identity: async () => IDENTITY,
  });
}

describe("a new cluster's control plane runs in containers", () => {
  it("is not a question the installer asks", async () => {
    const { driver } = await interactiveInstall();
    const asked = driver.questions.map((question) => question.name);
    assert.ok(asked.includes("enabled_modules"), asked.join(","));
    const offered = driver.questions
      .flatMap((question) => question.choices)
      .map((choice) => choice.value);
    assert.equal(offered.includes("ecs"), false, offered.join(","));
    assert.equal(
      driver.questions.some((question) => question.name === "enable_ecs"),
      false,
    );
  });

  it("is recorded in the values file the installer writes", async () => {
    const { values } = await interactiveInstall();
    assert.equal(values["enable_ecs"], true);
    assert.deepEqual(values["enabled_modules"], MODULES);
  });

  it("reaches the generated module set, the flag and a pullable image", async () => {
    const { values } = await interactiveInstall();
    const dir = mkdtempSync(join(tmpdir(), "ideactl-container-install-"));
    work.push(dir);
    const modules = generateConfigFromTemplates(values, dir);
    const flat = flattenConfigDir(dir);
    assert.deepEqual(
      modules.filter((module) => module.id === "ecs").map((module) => module.type),
      ["stack"],
    );
    assert.equal(flat["global-settings.module_sets.default.ecs.module_id"], "ecs");
    assert.equal(flat["ecs.enabled"], true);
    assert.equal(typeof flat["ecs.image"], "string");
  });

  it("derives the task architecture from the host family rather than naming one", async () => {
    // The default is the provider's own family, and an operator who overrides to the other family
    // has to keep working, so the generated setting must stay a family name the stack resolves.
    const { values } = await interactiveInstall();
    const dir = mkdtempSync(join(tmpdir(), "ideactl-container-install-hosts-"));
    work.push(dir);
    generateConfigFromTemplates(values, dir);
    const instanceType = flattenConfigDir(dir)["ecs.hosts.instance_type"];
    assert.equal(typeof instanceType, "string");
    assert.match(String(instanceType), /^[a-z0-9]+\.[a-z0-9]+$/);
  });

  it("refuses a module selection the container stack cannot synthesize", async () => {
    await assert.rejects(
      scriptedInstall(["metrics"]),
      (error: unknown) =>
        error instanceof InstallerValidationError &&
        error.message.includes("scheduler") &&
        error.message.includes("virtual-desktop-controller"),
    );
  });
});

describe("a prompt driver that cannot be corrected", () => {
  it("raises the validation failure instead of asking forever", async () => {
    // A person re-answers a rejected question. A scripted or piped driver repeats itself, so an
    // unbounded retry becomes a silent hang that ends in an out-of-memory failure.
    let asked = 0;
    const driver: PromptDriver = {
      async ask(question: InstallerQuestion): Promise<unknown> {
        asked += 1;
        // Always answer the module question with a selection the container stack refuses.
        if (question.name === "enabled_modules") return ["metrics"];
        return Object.hasOwn(answers(), question.name)
          ? answers()[question.name]
          : question.defaultValue;
      },
      report: () => {},
    };
    await assert.rejects(
      collectInstallerValues({ driver, identity: async () => IDENTITY }),
      (error: unknown) => error instanceof InstallerValidationError,
    );
    assert.ok(asked < 200, `the question was asked ${asked} times`);
  });
});
