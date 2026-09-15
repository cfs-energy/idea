import {
  connectionAfterReplacement,
  createReadyDesktop,
  deleteDesktop,
  failed,
  openGatewayConnection,
  passed,
  requiredOption,
  waitForServiceRecovery,
} from "./shared.ts";
import type { ProofCheck } from "./types.ts";

/** Proves a live desktop connection survives replacement of one gateway task. */
export const gatewayTaskKillCheck: ProofCheck = {
  name: "gateway-task-kill",
  description: "Replace one gateway task while a desktop connection is open; the connection survives or a new one opens.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.desktopRequest === undefined ? ["desktop-request"] : []),
    ...(options.gatewayHost === undefined ? ["gateway-host"] : []),
    ...(options.cluster === undefined ? ["cluster"] : []),
    ...(options.region === undefined ? ["region"] : []),
    ...(options.gatewayService === undefined ? ["gateway-service"] : []),
    ...(options.gatewayTask === undefined ? ["gateway-task"] : []),
    ...(options.gatewayTargetGroup === undefined ? ["gateway-target-group"] : []),
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
      context.output(`ACTION stop gateway task ${requiredOption(context.options, "gatewayTask")}`);
      await context.cloud.stopTask(requiredOption(context.options, "cluster"), requiredOption(context.options, "gatewayTask"));
      const recovery = await waitForServiceRecovery(
        context,
        requiredOption(context.options, "gatewayService"),
        requiredOption(context.options, "gatewayTargetGroup"),
      );
      const after = await connectionAfterReplacement(context, connection, "gateway");
      connection = after.connection;
      const observed = [...recovery.observed, `desktop ready in ${desktop.elapsedMs}ms`, after.observed];
      return recovery.passed && after.passed ? passed(...observed) : failed(...observed);
    } finally {
      connection?.close();
      if (session !== undefined) {
        await deleteDesktop(context, session);
      }
    }
  },
};
