/**
 * The first exchange of a DCV web client session, enough to prove a desktop is reachable through
 * the gateway: the client's connection request and the server's confirm or abort. Field numbers
 * and framing are the DCV web client's (dcv.js, protocol 1.7): every WebSocket message is an
 * 8-byte little-endian header (body size, binary payload size) followed by the protobuf body,
 * padded to a multiple of 8.
 */

const CONNECTION_ABORT_REASONS: Record<number, string> = {
  0: "GENERIC_ERROR",
  1: "INTERNAL_SERVER_ERROR",
  2: "PROTOCOL_ERROR",
  10: "INVALID_SESSION_ID",
  11: "INVALID_CONNECTION_ID",
  20: "AUTHENTICATION_FAILED",
  30: "CONNECTION_LIMIT_REACHED",
  31: "GATEWAY_BUSY",
  40: "SERVER_UNREACHABLE",
  50: "UNSUPPORTED_CREDENTIAL",
  60: "UNSUPERVISED_USER_CONNECTION",
};

export type DcvServerReply =
  | { kind: "confirm"; connectionId: number; serverName: string }
  | { kind: "abort"; reason: number; reasonName: string }
  | { kind: "other"; fields: number[] };

function varint(value: number): number[] {
  const bytes: number[] = [];
  let remaining = value >>> 0;
  while (remaining >= 0x80) {
    bytes.push((remaining & 0x7f) | 0x80);
    remaining >>>= 7;
  }
  bytes.push(remaining);
  return bytes;
}

function lengthDelimited(field: number, payload: number[]): number[] {
  return [...varint((field << 3) | 2), ...varint(payload.length), ...payload];
}

function stringField(field: number, value: string): number[] {
  return lengthDelimited(field, [...Buffer.from(value, "utf8")]);
}

function varintField(field: number, value: number): number[] {
  return [...varint(field << 3), ...varint(value)];
}

/** `dcv.setup.ClientMessage { connectionRequest }`, framed for the wire. */
export function connectionRequestFrame(sessionId: string, authenticationToken: string, clientName: string): Uint8Array {
  const version = [...varintField(1, 1), ...varintField(2, 10), ...varintField(3, 1)];
  const clientInfo = [...stringField(1, clientName), ...lengthDelimited(2, version), ...stringField(3, "linux"), ...stringField(4, "web")];
  const protocol = [...varintField(1, 1), ...varintField(2, 7)];
  const request = [
    ...stringField(1, sessionId),
    ...stringField(10, authenticationToken),
    ...lengthDelimited(20, clientInfo),
    ...lengthDelimited(30, protocol),
  ];
  const body = lengthDelimited(10, request);
  const padded = (body.length + 7) & ~7;
  const frame = new Uint8Array(8 + padded);
  new DataView(frame.buffer).setUint32(0, body.length, true);
  new DataView(frame.buffer).setUint32(4, 0, true);
  frame.set(body, 8);
  return frame;
}

class Reader {
  public position = 0;
  private readonly bytes: Uint8Array;
  public readonly end: number;
  public constructor(bytes: Uint8Array) {
    this.bytes = bytes;
    this.end = bytes.length;
  }
  public done(): boolean { return this.position >= this.end; }
  public varint(): number {
    let result = 0;
    let shift = 0;
    for (;;) {
      const byte = this.bytes[this.position];
      if (byte === undefined) throw new Error("truncated varint");
      this.position += 1;
      result += (byte & 0x7f) * 2 ** shift;
      if ((byte & 0x80) === 0) return result;
      shift += 7;
    }
  }
  public bytesOf(length: number): Uint8Array {
    const slice = this.bytes.subarray(this.position, this.position + length);
    this.position += length;
    return slice;
  }
  public skip(wireType: number): void {
    if (wireType === 0) this.varint();
    else if (wireType === 1) this.position += 8;
    else if (wireType === 2) this.bytesOf(this.varint());
    else if (wireType === 5) this.position += 4;
    else throw new Error(`unsupported wire type ${wireType}`);
  }
}

function serverName(softwareInfo: Uint8Array): string {
  const reader = new Reader(softwareInfo);
  while (!reader.done()) {
    const tag = reader.varint();
    if (tag >>> 3 === 1 && (tag & 7) === 2) return Buffer.from(reader.bytesOf(reader.varint())).toString("utf8");
    reader.skip(tag & 7);
  }
  return "";
}

/** The first `dcv.setup.ServerMessage` after the connection request, taken off the wire frame. */
export function decodeServerReply(frame: Uint8Array): DcvServerReply {
  if (frame.length < 8) throw new Error(`frame of ${frame.length} bytes has no header`);
  const bodySize = new DataView(frame.buffer, frame.byteOffset, frame.byteLength).getUint32(0, true);
  const body = frame.subarray(8, 8 + bodySize);
  const reader = new Reader(body);
  const fields: number[] = [];
  while (!reader.done()) {
    const tag = reader.varint();
    const field = tag >>> 3;
    fields.push(field);
    if (field === 10 && (tag & 7) === 2) {
      const confirm = new Reader(reader.bytesOf(reader.varint()));
      let connectionId = 0;
      let name = "";
      while (!confirm.done()) {
        const inner = confirm.varint();
        if (inner >>> 3 === 1 && (inner & 7) === 0) connectionId = confirm.varint();
        else if (inner >>> 3 === 20 && (inner & 7) === 2) name = serverName(confirm.bytesOf(confirm.varint()));
        else confirm.skip(inner & 7);
      }
      return { kind: "confirm", connectionId, serverName: name };
    }
    if (field === 20 && (tag & 7) === 2) {
      const abort = new Reader(reader.bytesOf(reader.varint()));
      let reason = 0;
      while (!abort.done()) {
        const inner = abort.varint();
        if (inner >>> 3 === 1 && (inner & 7) === 0) reason = abort.varint();
        else abort.skip(inner & 7);
      }
      return { kind: "abort", reason, reasonName: CONNECTION_ABORT_REASONS[reason] ?? `reason ${reason}` };
    }
    reader.skip(tag & 7);
  }
  return { kind: "other", fields };
}

/** A `ServerMessage.connectionAbort` frame, for tests. */
export function connectionAbortFrame(reason: number): Uint8Array {
  const body = lengthDelimited(20, varintField(1, reason));
  const padded = (body.length + 7) & ~7;
  const frame = new Uint8Array(8 + padded);
  new DataView(frame.buffer).setUint32(0, body.length, true);
  frame.set(body, 8);
  return frame;
}

/** A `ServerMessage.connectionConfirm` frame, for tests. */
export function connectionConfirmFrame(connectionId: number, name: string): Uint8Array {
  const body = lengthDelimited(10, [...varintField(1, connectionId), ...lengthDelimited(20, stringField(1, name))]);
  const padded = (body.length + 7) & ~7;
  const frame = new Uint8Array(8 + padded);
  new DataView(frame.buffer).setUint32(0, body.length, true);
  frame.set(body, 8);
  return frame;
}
