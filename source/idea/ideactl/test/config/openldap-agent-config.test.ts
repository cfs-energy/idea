/**
 * The OpenLDAP host's CloudWatch agent configuration, which the CLI renders (the SDK renders the
 * other hosts'): no retention is asked for, since hosts may not set it and the deploy creates the
 * group with it, and additional log files are kept when the list starts empty.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { buildBootstrapContext } from "../../src/cli/bootstrap-context.ts";
import { bootstrapPackagePlans } from "../../src/cli/bootstrap-package.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";

function context(): { get_cloudwatch_agent_config(...args: unknown[]): any } {
  const rows: Record<string, unknown> = {
    "cluster.cluster_name": "idea-test1", "cluster.aws.region": "us-east-2", "cluster.aws.partition": "aws",
    "cluster.aws.account_id": "123456789012", "cluster.cluster_s3_bucket": "bucket", "cluster.home_dir": "/apps/idea-test1",
    "cluster.cloudwatch_logs.enabled": true, "cluster.cloudwatch_logs.retention_in_days": 45,
    "directoryservice.provider": "openldap", "directoryservice.cloudwatch_logs.enabled": true,
    "global-settings.module_sets.default.directoryservice.module_id": "directoryservice",
  };
  const config = new ClusterConfig(Object.entries(rows).map(([key, value]) => ({ key, value })));
  const [plan] = bootstrapPackagePlans("directoryservice", "directoryservice", "d", "openldap");
  return buildBootstrapContext({ moduleName: "directoryservice", moduleId: "directoryservice", moduleSet: "default",
    baseOs: "amazonlinux2023", instanceType: "m7i.large", plan: plan!, config }) as never;
}

test("the OpenLDAP host's agent asks for no log retention", () => {
  const files = context().get_cloudwatch_agent_config().logs.logs_collected.files.collect_list as Array<Record<string, unknown>>;
  assert.ok(files.length > 0);
  for (const file of files) assert.equal("retention_in_days" in file, false, JSON.stringify(file));
});

test("additional log files are appended to the collected list", () => {
  const extra = { file_path: "/var/log/extra.log", log_group_name: "/idea-test1/directoryservice/openldap-server", log_stream_name: "extra" };
  const files = context().get_cloudwatch_agent_config([extra]).logs.logs_collected.files.collect_list as unknown[];
  assert.deepEqual(files.at(-1), extra);
});
