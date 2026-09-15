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
import type { ProofCheck } from "./types.ts";

const SETUP_TIMEOUT_MS = 30_000;

/**
 * Proves a desktop is reachable the way a user reaches it: the portal's connection info, then a
 * DCV web session through the gateway to the desktop's DCV server. The gateway resolves the
 * session with the broker and splices to the server; its reply is the server's confirm, or an
 * abort naming why (SERVER_UNREACHABLE when the gateway cannot reach the host). Only this check
 * exercises the gateway-to-desktop leg.
 */
export const desktopStreamCheck: ProofCheck = {
  name: "desktop-stream",
  description: "Create a desktop, then open a DCV session to it through the gateway as the web client does.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.desktopRequest === undefined ? ["desktop-request"] : []),
  ],
  async run(context) {
    if (context.gateway.openSession === undefined) {
      return failed("this runner cannot open DCV sessions");
    }
    let session;
    try {
      context.output("ACTION create desktop and wait for READY");
      const desktop = await createReadyDesktop(context);
      if ("passed" in desktop) {
        return desktop;
      }
      session = desktop.session;
      const owner = requiredOption(context.options, "username");
      const info = await context.api.request("VirtualDesktop.GetSessionConnectionInfo", {
        connection_info: { idea_session_id: session.idea_session_id, idea_session_owner: owner },
      });
      const connection = asObject(payloadObject(info)?.connection_info);
      const endpoint = stringField(connection, "endpoint");
      const token = stringField(connection, "access_token");
      const dcvSessionId = stringField(connection, "dcv_session_id");
      if (!apiSucceeded(info) || connection === undefined || endpoint === undefined || token === undefined || dcvSessionId === undefined) {
        return failed(`desktop ready in ${desktop.elapsedMs}ms`, `connection info incomplete: ${stringField(connection, "failure_reason") ?? `status ${info.status}`}`);
      }
      // dcv.js: the page URL without query and fragment, http to ws, trailing slashes off, plus /ws.
      const base = `${endpoint}${stringField(connection, "web_url_path") ?? "/"}`.split("?")[0]?.split("#")[0] ?? endpoint;
      const url = `${base.replace(/^http/, "ws").replace(/\/+$/, "")}/ws`;
      context.output(`ACTION open a DCV session to ${dcvSessionId} through ${url}`);
      let outcome;
      try {
        outcome = await context.gateway.openSession({ url, sessionId: dcvSessionId, authenticationToken: token, timeoutMs: SETUP_TIMEOUT_MS });
      } catch (error) {
        return failed(`desktop ready in ${desktop.elapsedMs}ms`, `the gateway closed the session before answering: ${(error as Error).message}`);
      }
      const { reply } = outcome;
      if (reply.kind === "confirm") {
        return passed(`desktop ready in ${desktop.elapsedMs}ms`, `session confirmed by ${reply.serverName || "the DCV server"} (connection ${reply.connectionId}) in ${outcome.elapsedMs}ms`);
      }
      if (reply.kind === "abort") {
        return failed(`desktop ready in ${desktop.elapsedMs}ms`, `the gateway aborted the session: ${reply.reasonName}`);
      }
      return failed(`desktop ready in ${desktop.elapsedMs}ms`, `unexpected first reply, fields ${reply.fields.join(",")}`);
    } finally {
      if (session !== undefined) {
        await deleteDesktop(context, session);
      }
    }
  },
};
