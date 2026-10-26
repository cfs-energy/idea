/**
 * Missing template values must be observed, not guessed. Isolated trees use the
 * real builder so the bytes are what a host script would run.
 */

import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { BootstrapPackageBuilder, BootstrapPackageError } from "../../src/cli/bootstrap-package.ts";
import {
  buildComponent,
  clusterConfigContext,
  fullContext,
  leftoverJinja,
  renderMini,
  sparseContext,
  unpackLikeLinux,
  withWorkdir,
} from "./bootstrap-helpers.ts";

/** Flattens `error.cause` so a wrapped template failure is still matchable. */
function errorChain(error: unknown): string {
  const parts: string[] = [];
  const seen = new Set<unknown>();
  let current: unknown = error;
  while (current instanceof Error && !seen.has(current)) {
    seen.add(current);
    parts.push(current.name, current.message);
    current = current.cause;
  }
  return parts.join(" ");
}

test("a missing optional get_string becomes an empty assignment, not a leftover tag", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "app/setup.sh.jinja2":
          "IDEA_JOB_STATUS_SQS_QUEUE_URL={{ context.config.get_string('scheduler.job_status_sqs_queue_url') }}\n",
      },
      sparseContext({}),
      "app/setup.sh",
    );
    assert.equal(rendered, "IDEA_JOB_STATUS_SQS_QUEUE_URL=");
    assert.deepEqual(leftoverJinja(rendered), []);
  });
});

test("a missing required get_string fails the render instead of substituting empty", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        renderMini(
          workDirectory,
          {
            "app/setup.sh.jinja2":
              "PBS_SERVER={{ context.config.get_string('scheduler.private_dns_name', required=True) }}\n",
          },
          sparseContext({}),
          "app/setup.sh",
        ),
      /missing required config: scheduler\.private_dns_name/,
    );
  });
});

test("a required list that is absent fails closed before ' '.join can run", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        renderMini(
          workDirectory,
          {
            "app/setup.sh.jinja2":
              "SYSTEM_PKGS=({{ ' '.join(context.config.get_list('global-settings.package_config.linux_packages.system', required=True)) }})\n",
          },
          sparseContext({}),
          "app/setup.sh",
        ),
      /missing required list/,
    );
  });
});

test("an optional list with default=[] renders an empty array instead of joining None", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "app/setup.sh.jinja2":
          "SYSTEM_PKGS_8=({{ ' '.join(context.config.get_list('global-settings.package_config.linux_packages.system_8', default=[])) }})\n",
      },
      sparseContext({}),
      "app/setup.sh",
    );
    assert.equal(rendered, "SYSTEM_PKGS_8=()");
  });
});

test("an optional list without a default still reaches join(undefined) and fails the render", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        renderMini(
          workDirectory,
          {
            "app/setup.sh.jinja2":
              "SYSTEM_PKGS=({{ ' '.join(context.config.get_list('global-settings.package_config.linux_packages.system_8')) }})\n",
          },
          sparseContext({}),
          "app/setup.sh",
        ),
      (error: unknown) => {
        assert.match(errorChain(error), /Cannot read properties of undefined \(reading 'join'\)/);
        return true;
      },
    );
  });
});

test("a missing scheduler private IP does not write an /etc/hosts pin", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "compute-node/setup.sh.jinja2":
          "{% set scheduler_private_ip = context.config.get_string('scheduler.private_ip') %}\n{% if scheduler_private_ip %}\necho \"{{ scheduler_private_ip }} scheduler.example.invalid\" >> /etc/hosts\n{% endif %}\n",
      },
      sparseContext({}),
      "compute-node/setup.sh",
    );
    assert.equal(rendered.includes("/etc/hosts"), false);
    assert.equal(rendered.trim(), "");
  });
});

test("a missing shared-storage data mount dir writes an empty PBS usecp path", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "compute-node/setup.sh.jinja2":
          "$usecp *:{{ context.config.get_string('shared-storage.data.mount_dir') }} {{ context.config.get_string('shared-storage.data.mount_dir') }}\n",
      },
      sparseContext({}),
      "compute-node/setup.sh",
    );
    assert.equal(rendered, "$usecp *: ");
  });
});

test("cloudwatch agent always rm -rf a hardcoded bootstrap directory, not a template path", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "app/setup.sh.jinja2":
          "CLOUDWATCH_AGENT_BOOTSTRAP_DIR=\"/root/bootstrap/amazon-cloudwatch-agent\"\nrm -rf ${CLOUDWATCH_AGENT_BOOTSTRAP_DIR}\n",
      },
      sparseContext({}),
      "app/setup.sh",
    );
    assert.match(rendered, /rm -rf \$\{CLOUDWATCH_AGENT_BOOTSTRAP_DIR\}/);
    assert.match(rendered, /\/root\/bootstrap\/amazon-cloudwatch-agent/);
  });
});

test("an empty wget URL from a missing optional download link is still invoked", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "app/setup.sh.jinja2":
          "DOWNLOAD_LINK=\"{{ context.config.get_string('global-settings.package_config.amazon_cloudwatch_agent.download_link', default='') }}\"\nwget \"${DOWNLOAD_LINK}\"\n",
      },
      sparseContext({}),
      "app/setup.sh",
    );
    assert.match(rendered, /^DOWNLOAD_LINK=""$/m);
    assert.match(rendered, /wget "\$\{DOWNLOAD_LINK\}"/);
  });
});

test("a missing session type is rewritten to an empty string, so the console branch is skipped", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "virtual-desktop-host-linux/setup.sh.jinja2":
          "{% if context.vars.session.type.lower() == 'console' %}CONSOLE{% else %}VIRTUAL{% endif %}\n",
      },
      sparseContext({ vars: {} }),
      "virtual-desktop-host-linux/setup.sh",
    );
    assert.equal(rendered, "VIRTUAL");
  });
});

test("a missing enabled_drivers list fails the 'in' test used by the AMI builder", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        renderMini(
          workDirectory,
          {
            "compute-node-ami-builder/setup.sh.jinja2":
              "{% if 'efa' in context.vars.enabled_drivers %}EFA{% endif %}\n",
          },
          sparseContext({ vars: {} }),
          "compute-node-ami-builder/setup.sh",
        ),
      /Cannot use "in" operator/,
    );
  });
});

test("the real compute-node PBS template still emits an empty usecp when the mount dir is absent", () => {
  withWorkdir((workDirectory) => {
    const full = fullContext();
    const inner = full.config as { get_string: (key: string, ...args: unknown[]) => unknown };
    const archiveFile = buildComponent("compute-node", workDirectory, {
      config: {
        ...inner,
        get_string(key: string, ...args: unknown[]): unknown {
          if (key === "shared-storage.data.mount_dir") return undefined;
          return inner.get_string(key, ...args);
        },
      },
    });
    const extracted = unpackLikeLinux(archiveFile, join(workDirectory, "extracted"));
    const content = readFileSync(join(extracted, "compute-node/compute_node.sh"), "utf8");
    assert.match(content, /\$usecp \*: $/m);
  });
});

test("ClusterConfig treats a missing required key as a render failure", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        renderMini(
          workDirectory,
          {
            "app/setup.sh.jinja2":
              "PBS_SERVER={{ context.config.get_string('scheduler.private_dns_name', required=True) }}\n",
          },
          clusterConfigContext({ "cluster.cluster_name": "sample-cluster" }),
          "app/setup.sh",
        ),
      (error: unknown) => {
        assert.match(errorChain(error), /private_dns_name|ConfigKeyNotFound/);
        return true;
      },
    );
  });
});

test("ClusterConfig required=True on a stored empty string still substitutes empty", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "app/setup.sh.jinja2":
          "PBS_SERVER={{ context.config.get_string('scheduler.private_dns_name', required=True) }}\n",
      },
      clusterConfigContext({ "scheduler.private_dns_name": "   " }),
      "app/setup.sh",
    );
    assert.equal(rendered, "PBS_SERVER=");
  });
});

test("ClusterConfig required=True on a NULL list returns no sequence and join fails at render", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        renderMini(
          workDirectory,
          {
            "app/setup.sh.jinja2":
              "SYSTEM_PKGS=({{ ' '.join(context.config.get_list('global-settings.package_config.linux_packages.system', required=True)) }})\n",
          },
          clusterConfigContext({ "global-settings.package_config.linux_packages.system": null }),
          "app/setup.sh",
        ),
      (error: unknown) => {
        assert.match(errorChain(error), /Cannot read properties of undefined \(reading 'join'\)/);
        return true;
      },
    );
  });
});

test("a missing optional openmpi checksum is rewritten to empty rather than failing the render", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "app/setup.sh.jinja2":
          "OPENMPI_HASH=\"{{ context.config.get_string('global-settings.package_config.openmpi.checksum').lower().strip() }}\"\nwget \"${OPENMPI_URL}\"\n",
      },
      sparseContext({}),
      "app/setup.sh",
    );
    assert.equal(rendered, "OPENMPI_HASH=\"\"\nwget \"${OPENMPI_URL}\"");
  });
});

test("concatenating a missing apps mount dir onto /python writes undefined/python", () => {
  withWorkdir((workDirectory) => {
    const rendered = renderMini(
      workDirectory,
      {
        "app/setup.sh.jinja2":
          "{% set install_dir = context.config.get_string('shared-storage.apps.mount_dir') + '/python' %}\nINSTALL_DIR={{ install_dir }}\n",
      },
      sparseContext({}),
      "app/setup.sh",
    );
    assert.equal(rendered, "\nINSTALL_DIR=undefined/python");
  });
});

test("a missing optional openmpi URL fails on .split rather than baking an empty tarball name", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        renderMini(
          workDirectory,
          {
            "app/setup.sh.jinja2":
              "OPENMPI_TGZ=\"{{ context.config.get_string('global-settings.package_config.openmpi.url').split('/')[-1] }}\"\n",
          },
          sparseContext({}),
          "app/setup.sh",
        ),
      (error: unknown) => {
        assert.match(errorChain(error), /split|undefined or falsey/i);
        return true;
      },
    );
  });
});

test("Windows Configure.ps1 fails the render when generate_password is absent from utils", () => {
  withWorkdir((workDirectory) => {
    assert.throws(
      () =>
        buildComponent("virtual-desktop-host-windows", workDirectory, {
          utils: {
            to_json(value: unknown): string {
              return JSON.stringify(value);
            },
            to_yaml(value: unknown): string {
              return `${JSON.stringify(value)}\n`;
            },
          },
        }),
      (error: unknown) => {
        assert.match(errorChain(error), /generate_password|Unable to call/i);
        return true;
      },
    );
  });
});

test("an empty components list is rejected before any file is written", () => {
  withWorkdir((workDirectory) => {
    mkdirSync(join(workDirectory, "source"));
    writeFileSync(join(workDirectory, "source", "ignored.txt"), "x", "utf8");
    assert.throws(
      () =>
        new BootstrapPackageBuilder({
          sourceDirectory: join(workDirectory, "source"),
          targetPackageBasename: "bootstrap-empty",
          components: [],
          context: {},
          tmpDir: workDirectory,
        }),
      BootstrapPackageError,
    );
  });
});
