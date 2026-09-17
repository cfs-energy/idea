import { createReadyDesktop, deleteDesktop, failed, openGatewayConnection, passed } from "./shared.ts";
import type { ProofCheck } from "./types.ts";

/** Proves a new virtual desktop reaches READY and the gateway accepts a connection. */
export const desktopEndToEndCheck: ProofCheck = {
  name: "desktop-end-to-end",
  description: "Create a desktop, wait for READY, verify the gateway, then delete it.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.desktopRequest === undefined ? ["desktop-request"] : []),
    ...(options.gatewayHost === undefined ? ["gateway-host"] : []),
  ],
  async run(context) {
    let session;
    try {
      context.output("ACTION create desktop and wait for READY");
      const desktop = await createReadyDesktop(context);
      if ("passed" in desktop) {
        return desktop;
      }
      session = desktop.session;
      context.output("ACTION open a gateway TLS connection");
      const connection = await openGatewayConnection(context);
      try {
        if (!connection.isOpen()) {
          return failed(`desktop ready in ${desktop.elapsedMs}ms`, "gateway connection was not reachable");
        }
        return passed(`desktop ready in ${desktop.elapsedMs}ms`, "gateway connection remained open");
      } finally {
        connection.close();
      }
    } finally {
      if (session !== undefined) {
        await deleteDesktop(context, session);
      }
    }
  },
};
