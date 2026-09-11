import {
  apiSucceeded,
  createReadyDesktop,
  deleteDesktop,
  failed,
  openGatewayConnection,
  requiredOption,
  waitForServiceRecovery,
} from "./shared.ts";
import type { ProofCheck } from "./types.ts";

/** Proves broker replacement reforms its service and leaves a desktop connection usable. */
export const brokerTaskKillCheck: ProofCheck = {
  name: "broker-task-kill",
  description: "Keep a desktop connection open while one broker task is replaced.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.desktopRequest === undefined ? ["desktop-request"] : []),
    ...(options.gatewayHost === undefined ? ["gateway-host"] : []),
    ...(options.cluster === undefined ? ["cluster"] : []),
    ...(options.region === undefined ? ["region"] : []),
    ...(options.brokerService === undefined ? ["broker-service"] : []),
    ...(options.brokerTask === undefined ? ["broker-task"] : []),
    ...(options.brokerTargetGroup === undefined ? ["broker-target-group"] : []),
  ],
  async run(context) {
    let session;
    let connection;
    try {
      context.output("ACTION create desktop and wait for READY");
      const desktop = await createReadyDesktop(context);
      if ("passed" in desktop) {
        return desktop;
      }
      session = desktop.session;
      context.output("ACTION open a gateway TLS connection");
      connection = await openGatewayConnection(context);
      context.output(`ACTION stop broker task ${requiredOption(context.options, "brokerTask")}`);
      await context.cloud.stopTask(requiredOption(context.options, "cluster"), requiredOption(context.options, "brokerTask"));
      const recovery = await waitForServiceRecovery(
        context,
        requiredOption(context.options, "brokerService"),
        requiredOption(context.options, "brokerTargetGroup"),
      );
      const sessionInfo = await context.api.request("VirtualDesktop.GetSessionConnectionInfo", {
        connection_info: session,
      });
      if (!apiSucceeded(sessionInfo)) {
        return failed(...recovery.observed, `desktop ready in ${desktop.elapsedMs}ms`, `session response=${JSON.stringify(sessionInfo.body)}`);
      }
      if (!connection.isOpen()) {
        return failed(...recovery.observed, `desktop ready in ${desktop.elapsedMs}ms`, "connection closed during broker replacement");
      }
      return { ...recovery, observed: [...recovery.observed, `desktop ready in ${desktop.elapsedMs}ms`, "connection remained open"] };
    } finally {
      connection?.close();
      if (session !== undefined) {
        await deleteDesktop(context, session);
      }
    }
  },
};
