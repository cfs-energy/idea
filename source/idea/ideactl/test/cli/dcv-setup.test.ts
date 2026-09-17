/** The DCV setup exchange encoder and decoder: a request frame's shape, and both reply kinds decoded. */

import assert from "node:assert/strict";
import test from "node:test";

import { connectionAbortFrame, connectionConfirmFrame, connectionRequestFrame, decodeServerReply } from "../../tools/e2e/dcv-setup.ts";

test("the connection request is framed with an 8-byte header and a body padded to 8", () => {
  const frame = connectionRequestFrame("ac02cb60-e44e-433b-9b62-f17072b6c347", "token-value", "proof client");
  const bodySize = new DataView(frame.buffer).getUint32(0, true);
  assert.equal(new DataView(frame.buffer).getUint32(4, true), 0);
  assert.equal(frame.length, 8 + ((bodySize + 7) & ~7));
  // ClientMessage.connectionRequest is field 10, length-delimited: tag 0x52.
  assert.equal(frame[8], 0x52);
  const text = Buffer.from(frame).toString("latin1");
  assert.ok(text.includes("ac02cb60-e44e-433b-9b62-f17072b6c347"));
  assert.ok(text.includes("token-value"));
  assert.ok(text.includes("proof client"));
});

test("a confirm reply decodes to the server's name and connection id", () => {
  assert.deepEqual(decodeServerReply(connectionConfirmFrame(7, "NICE DCV Server")), { kind: "confirm", connectionId: 7, serverName: "NICE DCV Server" });
});

test("an abort reply decodes to its named reason", () => {
  assert.deepEqual(decodeServerReply(connectionAbortFrame(40)), { kind: "abort", reason: 40, reasonName: "SERVER_UNREACHABLE" });
  assert.deepEqual(decodeServerReply(connectionAbortFrame(20)), { kind: "abort", reason: 20, reasonName: "AUTHENTICATION_FAILED" });
});
