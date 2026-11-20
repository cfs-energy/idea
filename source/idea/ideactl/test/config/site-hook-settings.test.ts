/**
 * The documented way to set a site hook checksum reaches the reader, and a row under the module
 * name that nothing reads is reported.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseSetEntries } from "../../src/cli/commands/config.ts";
import { siteHookDigest, unreadSiteHookRow } from "../../src/cdk/site-hook.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";

const digest = "a".repeat(64);
const moduleSets = [
  { key: "global-settings.module_sets.default.virtual-desktop-controller.module_id", value: "vdc" },
  { key: "global-settings.module_sets.default.scheduler.module_id", value: "scheduler" },
];

test("the documented config set command for the desktop boot hook is the row the stack reads", () => {
  const rows = parseSetEntries([`Key=vdc.dcv_host_site_hook_sha256,Type=string,Value=${digest}`]);
  const config = new ClusterConfig([...moduleSets, ...rows]);
  assert.equal(siteHookDigest(config, "virtual-desktop-controller.dcv_host_site_hook_sha256"), digest);
  assert.equal(unreadSiteHookRow(config, "virtual-desktop-controller.dcv_host_site_hook_sha256"), undefined);
});

test("a checksum set under the module name is read by nothing, and is reported with the key to use", () => {
  const rows = parseSetEntries([`Key=virtual-desktop-controller.dcv_host_site_hook_sha256,Type=string,Value=${digest}`]);
  const config = new ClusterConfig([...moduleSets, ...rows]);
  assert.equal(siteHookDigest(config, "virtual-desktop-controller.dcv_host_site_hook_sha256"), "");
  assert.equal(unreadSiteHookRow(config, "virtual-desktop-controller.dcv_host_site_hook_sha256"), "vdc.dcv_host_site_hook_sha256");
  // where the id equals the name, there is only one form
  const same = new ClusterConfig([...moduleSets, ...parseSetEntries([`Key=scheduler.compute_node_site_hook_sha256,Type=string,Value=${digest}`])]);
  assert.equal(unreadSiteHookRow(same, "scheduler.compute_node_site_hook_sha256"), undefined);
});

test("upper case and surrounding spaces are normalised as the hosts compare them; another shape is refused", () => {
  const upper = new ClusterConfig([...moduleSets, { key: "scheduler.compute_node_site_hook_sha256", value: ` ${"A".repeat(64)} ` }]);
  assert.equal(siteHookDigest(upper, "scheduler.compute_node_site_hook_sha256"), digest);
  const bad = new ClusterConfig([...moduleSets, { key: "scheduler.compute_node_site_hook_sha256", value: "abc" }]);
  assert.throws(() => siteHookDigest(bad, "scheduler.compute_node_site_hook_sha256"), /must be empty or a 64-character hex SHA-256/);
});
