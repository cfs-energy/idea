/**
 * Provider-by-provider plans for the directoryservice module, pinned to the
 * previous implementation's `invoke_directoryservice`.
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

const pythonInvoker = readFileSync(
  fileURLToPath(
    new URL(
      "../../../idea-administrator/src/ideaadministrator/app/cdk/cdk_invoker.py",
      import.meta.url,
    ),
  ),
  "utf8",
);

const pythonConstants = readFileSync(
  fileURLToPath(
    new URL(
      "../../../idea-data-model/src/ideadatamodel/constants.py",
      import.meta.url,
    ),
  ),
  "utf8",
);

/** Returns the previous implementation's directoryservice invoker body. */
function pythonDirectoryserviceInvoker(): string {
  const start = pythonInvoker.indexOf("def invoke_directoryservice");
  assert.ok(start >= 0, "invoke_directoryservice must exist");
  const next = pythonInvoker.indexOf("\n    def invoke_", start + 1);
  assert.ok(next > start, "invoke_directoryservice must be followed by another invoke_ method");
  return pythonInvoker.slice(start, next);
}

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

test("the previous implementation uploads a directoryservice package only for OpenLDAP", () => {
  const body = pythonDirectoryserviceInvoker();
  assert.match(body, /if provider == constants\.DIRECTORYSERVICE_OPENLDAP:/);
  assert.match(body, /bootstrap_components=\['common', 'openldap-server'\]/);
  assert.match(body, /context_params=\{'bootstrap_package_uri': bootstrap_package_uri\}/);

  const elseBranch = body.slice(body.indexOf("        else:"));
  assert.ok(elseBranch.startsWith("        else:"), "the OpenLDAP branch must have an else");
  assert.ok(
    !elseBranch.includes("build_and_upload_bootstrap_package"),
    "the else branch must not build a package",
  );
  assert.ok(
    !elseBranch.includes("bootstrap_components"),
    "the else branch must not name components",
  );
  assert.ok(
    !elseBranch.includes("bootstrap_package_uri"),
    "the else branch must not pass bootstrap_package_uri",
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
