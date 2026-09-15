import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  apiSucceeded,
  asObject,
  createReadyDesktop,
  deleteDesktop,
  failed,
  passed,
  payloadObject,
  requiredOption,
  stringField,
} from "./shared.ts";
import type { ProcessResult, ProofCheck } from "./types.ts";

const SSH_ATTEMPTS = 3;
const SSH_RETRY_MS = 60_000;

/**
 * Proves a user can reach a new desktop over SSH the documented way: the key the portal issues
 * them, through the bastion, into the desktop's private address. The cluster manager writes the
 * matching public key into the user's home on the shared file system, which both hosts mount.
 */
export const desktopSshCheck: ProofCheck = {
  name: "desktop-ssh",
  description: "Create a desktop, then SSH to it through the bastion with the user's portal-issued key.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.desktopRequest === undefined ? ["desktop-request"] : []),
    ...(options.bastionHost === undefined ? ["bastion-host"] : []),
  ],
  async run(context) {
    let session;
    const keyDirectory = mkdtempSync(join(tmpdir(), "desktop-ssh-"));
    try {
      context.output("ACTION fetch the user's SSH key from the portal");
      const keyResult = await context.api.request("Auth.GetUserPrivateKey", { key_format: "pem", platform: "linux" });
      const keyMaterial = stringField(payloadObject(keyResult), "key_material");
      if (!apiSucceeded(keyResult) || keyMaterial === undefined) {
        // The body may carry the key, so only the status is recorded.
        return failed(`the portal did not return a key, status=${keyResult.status}`);
      }
      const keyFile = join(keyDirectory, "id_rsa");
      writeFileSync(keyFile, keyMaterial, { mode: 0o600 });

      context.output("ACTION create desktop and wait for READY");
      const desktop = await createReadyDesktop(context);
      if ("passed" in desktop) {
        return desktop;
      }
      session = desktop.session;
      const privateIp = stringField(asObject(session.server), "private_ip");
      if (privateIp === undefined) {
        return failed(`desktop ready in ${desktop.elapsedMs}ms`, "the session reports no server private_ip");
      }

      const user = requiredOption(context.options, "username");
      const bastion = requiredOption(context.options, "bastionHost");
      context.output(`ACTION ssh ${user}@${privateIp} through ${bastion}`);
      const common = ["-i", keyFile, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=20", "-o", "LogLevel=ERROR"];
      const startedAt = context.now();
      let last: ProcessResult = { exitCode: 1, stderr: "not attempted", stdout: "" };
      // A fresh desktop's directory client can trail its READY state by a little.
      for (let attempt = 1; attempt <= SSH_ATTEMPTS; attempt += 1) {
        last = await context.processes.run("ssh", [
          ...common,
          "-o",
          `ProxyCommand=ssh ${common.join(" ")} -W %h:%p ${user}@${bastion}`,
          `${user}@${privateIp}`,
          "hostname && id -un",
        ]);
        if (last.exitCode === 0) {
          return passed(`desktop ready in ${desktop.elapsedMs}ms`, `ssh succeeded on attempt ${attempt} in ${context.now() - startedAt}ms: ${last.stdout.trim().replaceAll("\n", " ")}`);
        }
        if (attempt < SSH_ATTEMPTS) await context.sleep(SSH_RETRY_MS);
      }
      return failed(`desktop ready in ${desktop.elapsedMs}ms`, `ssh failed ${SSH_ATTEMPTS} times, exit ${last.exitCode}: ${last.stderr.trim().slice(-300)}`);
    } finally {
      rmSync(keyDirectory, { recursive: true, force: true });
      if (session !== undefined) {
        await deleteDesktop(context, session);
      }
    }
  },
};
