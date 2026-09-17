/**
 * Provider-by-provider plans for the directoryservice module, pinned to the
 * previous implementation's `invoke_directoryservice`.
 *
 * The expectations below were read out of that Python method before the
 * administrator source tree was deleted; only the port is read now.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  bootstrapPackagePlans,
  type BootstrapPackagePlan,
} from "../../src/cli/bootstrap-package.ts";

const DEPLOYMENT_ID = "deployment-1";

const pythonConstants = readFileSync(
  fileURLToPath(
    new URL(
      "../../../idea-data-model/src/ideadatamodel/constants.py",
      import.meta.url,
    ),
  ),
  "utf8",
);

/** Summarises a plan list the way the previous implementation names components. */
function planSummary(plans: BootstrapPackagePlan[]): string[][] {
  return plans.map((plan) => plan.components);
}

const openldapPlan: BootstrapPackagePlan[] = [
  {
    basename: `bootstrap-directoryservice-${DEPLOYMENT_ID}`,
    components: ["common", "openldap-server"],
    contextParameter: "bootstrap_package_uri",
  },
];

const directoryProviders: Array<{
  provider: string | undefined;
  pythonUploads: boolean;
}> = [
  { provider: "openldap", pythonUploads: true },
  { provider: "activedirectory", pythonUploads: false },
  { provider: "aws_managed_activedirectory", pythonUploads: false },
  { provider: "unknown-provider", pythonUploads: false },
  { provider: undefined, pythonUploads: false },
];

test("the previous implementation names exactly three directory providers", () => {
  assert.match(pythonConstants, /DIRECTORYSERVICE_OPENLDAP = 'openldap'/);
  assert.match(pythonConstants, /DIRECTORYSERVICE_ACTIVE_DIRECTORY = 'activedirectory'/);
  assert.match(
    pythonConstants,
    /DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY = 'aws_managed_activedirectory'/,
  );
});

describe("directoryservice plans match the previous implementation", () => {
  for (const row of directoryProviders) {
    const label = row.provider ?? "undefined";
    test(`provider ${label}`, () => {
      const plans = bootstrapPackagePlans(
        "directoryservice",
        "directoryservice",
        DEPLOYMENT_ID,
        row.provider,
      );
      if (row.pythonUploads) {
        assert.deepEqual(plans, openldapPlan);
        return;
      }
      assert.deepEqual(plans, []);
    });
  }
});

const hostModules: Array<{
  moduleName: string;
  moduleId: string;
  components: string[][];
}> = [
  {
    moduleName: "cluster-manager",
    moduleId: "cluster-manager",
    components: [["cluster-manager"]],
  },
  {
    moduleName: "scheduler",
    moduleId: "scheduler",
    components: [["scheduler"]],
  },
  {
    moduleName: "bastion-host",
    moduleId: "bastion-host",
    components: [["common", "bastion-host"]],
  },
  {
    moduleName: "virtual-desktop-controller",
    moduleId: "vdc",
    components: [
      ["virtual-desktop-controller"],
      ["dcv-broker"],
      ["dcv-connection-gateway"],
    ],
  },
];

describe("other host modules keep their plans on every directory provider", () => {
  for (const host of hostModules) {
    for (const row of directoryProviders) {
      const label = row.provider ?? "undefined";
      test(`${host.moduleName} with provider ${label}`, () => {
        assert.deepEqual(
          planSummary(
            bootstrapPackagePlans(host.moduleName, host.moduleId, DEPLOYMENT_ID, row.provider),
          ),
          host.components,
        );
      });
    }
  }
});
