import type { JsonObject } from "../api.ts";
import { randomBytes, randomUUID } from "node:crypto";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { apiSucceeded, asObject, failed, passed, payloadObject, waitUntil } from "./shared.ts";
import type { ProofCheck } from "./types.ts";

export const accountReconcileCheck: ProofCheck = {
  name: "account-reconcile",
  description: "Disable a disposable directory user over LDAP and prove reconciliation stops its desktop.",
  requiredFlags: (options) => options.ldapUri ? [
    ...(!options.ldapBindDn ? ["ldap-bind-dn"] : []),
    ...(!options.ldapPasswordFile ? ["ldap-password-file"] : []),
    ...(!options.ldapUserBase ? ["ldap-user-base"] : []),
    ...(!options.desktopRequest ? ["desktop-request"] : []),
    ...(!options.albHost ? ["alb-host"] : []),
    ...(!options.username ? ["username"] : []),
    ...(!options.passwordFile ? ["password-file"] : []),
  ] : [],
  async run(context) {
    const options = context.options;
    const skip = (reason: string) => ({ passed: false, skipped: true, observed: [reason] });
    if (!options.ldapUri || !options.ldapBindDn || !options.ldapPasswordFile || !options.ldapUserBase) {
      return skip("no writable directory supplied; requires LDAP URI, bind DN, password file and user base");
    }
    const settings = await context.api.request("ClusterSettings.GetModuleSettings", { module_id: "directoryservice" });
    if (!apiSucceeded(settings)) return failed("cannot read directory provider");
    const provider = asObject(payloadObject(settings)?.settings)?.provider;
    if (provider !== "activedirectory" && provider !== "aws_managed_activedirectory") {
      return skip("cluster has no Active Directory to write to");
    }
    // A plain ldap:// is accepted only for a local tunnel (SSM port forwarding encrypts it); AWS
    // Managed Microsoft AD has no LDAPS unless a CA is attached, and it refuses password writes
    // over plain LDAP, so the tunnel path enables a passwordless account (PASSWD_NOTREQD).
    const tunnel = /^ldap:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/u.test(options.ldapUri);
    if (!options.ldapUri.startsWith("ldaps://") && !tunnel) return failed("LDAP proof requires ldaps://, or ldap:// to a local tunnel");
    const username = `proof${randomUUID().replaceAll("-", "").slice(0, 12)}`;
    const dn = `CN=${username},${options.ldapUserBase}`;
    const directory = await mkdtemp(join(tmpdir(), "account-reconcile-"));
    const args = ["-x", "-H", options.ldapUri, "-D", options.ldapBindDn, "-y", options.ldapPasswordFile];
    let directoryCreated = false;
    let ideaCreated = false;
    let session: JsonObject | undefined;
    const modify = async (text: string, command = "ldapmodify") => {
      const file = join(directory, "change.ldif");
      await writeFile(file, text, { mode: 0o600 });
      return context.processes.run(command, [...args, "-f", file]);
    };
    try {
      const password = `"${randomBytes(24).toString("base64")}aA1!"`;
      const unicodePassword = Buffer.from(password, "utf16le").toString("base64");
      // The external AD provider never creates directory objects; Managed AD sync does.
      // Use exactly one directory writer so replication cannot produce duplicate identities.
      if (provider === "activedirectory") {
        const added = await modify(`dn: ${dn}\nobjectClass: top\nobjectClass: person\nobjectClass: organizationalPerson\nobjectClass: user\ncn: ${username}\nsn: ${username}\nsAMAccountName: ${username}\nmail: ${username}@example.invalid${tunnel ? "" : `\nunicodePwd:: ${unicodePassword}`}\nuserAccountControl: ${tunnel ? 544 : 512}\n\n`, "ldapadd");
        if (added.exitCode !== 0) return skip("directory fixture could not be created with the supplied LDAP access");
        directoryCreated = true;
      }
      context.output(`ACTION create disposable IDEA user ${username}`);
      const created = await context.api.request("Accounts.CreateUser", { user: { username, email: `${username}@example.invalid` } });
      if (!apiSucceeded(created)) return failed("IDEA user creation failed");
      ideaCreated = true;
      const user = asObject(payloadObject(created)?.user);
      if (typeof user?.uid !== "number" || typeof user.gid !== "number") return failed("created user has no POSIX IDs");
      // The sync worker may use a different controller from the tunnel; wait for replication
      // before modifying its object. External AD objects are created above instead.
      const visible = await waitUntil(context, 600, "directory user visible", async () => {
        const found = await context.processes.run("ldapsearch", [...args, "-LLL", "-b", dn, "-s", "base", "(objectClass=user)", "sAMAccountName"]);
        return found.exitCode === 0 && found.stdout.split(/\r?\n/u).includes(`sAMAccountName: ${username}`);
      });
      if (!visible) return failed("directory user did not become visible through LDAP");
      directoryCreated = true;
      const enabled = await modify(`dn: ${dn}\nchangetype: modify${tunnel ? "" : `\nreplace: unicodePwd\nunicodePwd:: ${unicodePassword}\n-`}\nreplace: userAccountControl\nuserAccountControl: ${tunnel ? 544 : 512}\n\n`);
      if (enabled.exitCode !== 0) return failed("directory fixture could not be enabled with the supplied LDAP access");
      const mapped = await modify(`dn: ${dn}\nchangetype: modify\nreplace: uidNumber\nuidNumber: ${user.uid}\n-\nreplace: gidNumber\ngidNumber: ${user.gid}\n-\nreplace: unixHomeDirectory\nunixHomeDirectory: /home/${username}\n-\nreplace: loginShell\nloginShell: /bin/bash\n\n`);
      if (mapped.exitCode !== 0) return failed("directory POSIX mapping failed");
      // A desktop needs its owner in the desktop module's users group; a fresh account is in none.
      const modules = await context.api.request("ClusterSettings.ListClusterModules", {});
      const listing = payloadObject(modules)?.listing;
      const desktopModule = (Array.isArray(listing) ? listing : []).map((entry) => asObject(entry))
        .find((entry) => entry?.name === "virtual-desktop-controller")?.module_id;
      const grouped = await context.api.request("Accounts.AddUserToGroup", { usernames: [username], group_name: `${typeof desktopModule === "string" ? desktopModule : "vdc"}-users-module-group` });
      if (!apiSucceeded(grouped)) return failed(`could not add the proof user to the desktop users group: ${JSON.stringify(grouped).slice(0, 300)}`);
      const desktopRequest = asObject(options.desktopRequest?.session);
      if (!desktopRequest) return failed("desktop-request.session is required");
      // Project membership is by directory group: join one the desktop's project already lists.
      const projectId = asObject(desktopRequest.project)?.project_id;
      if (typeof projectId !== "string") return failed("desktop-request.session.project.project_id is required");
      const project = await context.api.request("Projects.GetProject", { project_id: projectId });
      const projectRecord = asObject(payloadObject(project)?.project);
      const originalGroups = Array.isArray(projectRecord?.ldap_groups) ? (projectRecord.ldap_groups as unknown[]).filter((entry): entry is string => typeof entry === "string") : [];
      if (!apiSucceeded(project) || !projectRecord) return failed(`could not read the desktop's project: ${JSON.stringify(project).slice(0, 300)}`);
      // Project membership is rebuilt from group membership by an asynchronous task, so the proof
      // joins a group the project already lists and waits for the mapping.
      const projectGroup = originalGroups[0];
      if (projectGroup === undefined) return failed("the desktop's project lists no group to join");
      const joined = await context.api.request("Accounts.AddUserToGroup", { usernames: [username], group_name: projectGroup });
      if (!apiSucceeded(joined)) return failed(`could not add the proof user to the project's group: ${JSON.stringify(joined).slice(0, 300)}`);
      let lastProjects: unknown;
      const member = await waitUntil(context, 600, "proof user visible in the desktop's project", async () => {
        const result = await context.api.request("Projects.GetUserProjects", { username });
        lastProjects = result;
        const projects = payloadObject(result)?.projects;
        return apiSucceeded(result) && Array.isArray(projects) && projects.some((entry) => asObject(entry)?.project_id === projectId);
      });
      if (!member) return failed(`the proof user never appeared in the desktop's project; last answer ${JSON.stringify(lastProjects).slice(0, 300)}`);
      const desktop = await context.api.request("VirtualDesktopAdmin.CreateSession", { session: { ...desktopRequest, owner: username } });
      session = asObject(payloadObject(desktop)?.session);
      if (!apiSucceeded(desktop) || !session) return failed(`proof desktop creation failed: ${JSON.stringify(desktop).slice(0, 400)}`);
      const ready = await waitUntil(context, options.readyTimeoutSeconds ?? 1800, "proof desktop ready", async () => {
        const result = await context.api.request("VirtualDesktopAdmin.GetSessionInfo", { session: session ?? {} });
        return apiSucceeded(result) && asObject(payloadObject(result)?.session)?.state === "READY";
      });
      if (!ready) return failed("proof desktop did not reach READY");
      const disabled = await modify(`dn: ${dn}\nchangetype: modify\nreplace: userAccountControl\nuserAccountControl: 514\n\n`);
      if (disabled.exitCode !== 0) return failed("LDAP disable failed");
      const preview = await context.api.request("Accounts.ReconcileUsers", { dry_run: true });
      const previewReport = payloadObject(preview);
      const changes = previewReport?.changes;
      if (!apiSucceeded(preview) || previewReport?.refused || previewReport?.errors || !Array.isArray(changes)
        || changes.some((change) => asObject(change)?.username !== username)) {
        return failed("dry run refused or contains changes outside the disposable user");
      }
      context.output("ACTION reconcile accounts with dry_run=false (normal safety cap applies)");
      const reconciled = await context.api.request("Accounts.ReconcileUsers", { dry_run: false });
      const report = payloadObject(reconciled);
      if (!apiSucceeded(reconciled) || report?.refused || report?.errors) return failed("reconciliation failed or was refused; inspect the API report");
      const stopped = await waitUntil(context, options.readyTimeoutSeconds ?? 1800, "disabled user and stopped desktop", async () => {
        const account = await context.api.request("Accounts.GetUser", { username });
        const desktop = await context.api.request("VirtualDesktopAdmin.GetSessionInfo", { session: session ?? {} });
        return apiSucceeded(account) && asObject(payloadObject(account)?.user)?.enabled === false
          && apiSucceeded(desktop) && asObject(payloadObject(desktop)?.session)?.state === "STOPPED";
      });
      return stopped ? passed("LDAP-disabled user is disabled in IDEA; witnessed READY desktop is STOPPED") : failed("user or desktop did not reach the disabled/stopped state");
    } finally {
      try {
        if (session) {
          const deleted = await context.api.request("VirtualDesktopAdmin.DeleteSessions", { sessions: [{ ...session, force: true }] });
          if (!apiSucceeded(deleted)) throw new Error("proof desktop cleanup failed");
        }
        if (ideaCreated) {
          const deleted = await context.api.request("Accounts.DeleteUser", { username });
          if (!apiSucceeded(deleted)) throw new Error("proof IDEA user cleanup failed");
        }
      } finally {
        try {
          if (directoryCreated) {
            const deleted = await context.processes.run("ldapdelete", [...args, dn]);
            if (deleted.exitCode !== 0 && deleted.exitCode !== 32) throw new Error("proof directory cleanup failed");
          }
        } finally {
          await rm(directory, { recursive: true, force: true });
        }
      }
    }
  },
};
