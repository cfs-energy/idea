/**
 * The scheduler container's health check, as the day-zero rehearsal synthesizes it, run under
 * bash with the batch tools stood in for: it fails when the job scheduler has stopped or the
 * module answers with an application error, which nothing else in the task watches.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";

import { rehearseDayZero } from "../../tools/day-zero/rehearse.ts";

type Json = Record<string, any>;

describe("scheduler container health check", () => {
  const workDir = mkdtempSync(join(tmpdir(), "ideactl-scheduler-health-"));
  let command = "";
  process.on("exit", () => rmSync(workDir, { recursive: true, force: true }));

  before(async () => {
    const report = await rehearseDayZero({ valuesFile: join(import.meta.dirname, "day-zero-new-cluster.values.yml"), workDir });
    assert.equal(report.stacks.find((stack) => stack.moduleId === "scheduler")?.status, "SYNTHESIZED");
    const outdir = join(workDir, "cdk.out.scheduler");
    const template = JSON.parse(readFileSync(join(outdir, readdirSync(outdir).find((name) => name.endsWith(".template.json"))!), "utf8"));
    const definitions = Object.values(template.Resources as Record<string, Json>).filter((resource) => resource.Type === "AWS::ECS::TaskDefinition")
      .flatMap((resource) => resource.Properties.ContainerDefinitions as Json[]);
    const checks = definitions.map((definition) => definition.HealthCheck?.Command as string[] | undefined)
      .filter((check) => check?.[1]?.includes("qstat -B"));
    assert.equal(checks.length, 1);
    command = checks[0]![1]!;
  });

  const run = (state: { sched?: boolean; success?: boolean }) => {
    const bin = mkdtempSync(join(workDir, "bin-"));
    const tool = (name: string, body: string) => {
      writeFileSync(join(bin, name), `#!/bin/bash\n${body}\n`);
      chmodSync(join(bin, name), 0o755);
    };
    tool("qstat", "exit 0");
    tool("pgrep", state.sched === false ? "exit 1" : '[[ "$*" == "-x pbs_sched" ]]');
    tool("curl", `printf '%s' '{"success":${state.success === false ? "false" : "true"},"payload":{}}'`);
    return spawnSync("bash", ["-c", command], { env: { ...process.env, PATH: `${bin}:/usr/bin:/bin` } }).status;
  };

  it("passes with the server, the job scheduler and a successful API answer", () => {
    assert.equal(run({}), 0);
  });
  it("fails when the job scheduler has stopped", () => {
    assert.notEqual(run({ sched: false }), 0);
  });
  it("fails when the module answers with an application error", () => {
    assert.notEqual(run({ success: false }), 0);
  });
});
