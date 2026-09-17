import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

const TAR_BLOCK_SIZE = 512;

export interface TarArchiveEntry {
  name: string;
  mode: number;
  content: Buffer;
  type: string;
}

export interface RawTarArchiveEntry extends TarArchiveEntry {
  rawName: string;
}

/** Reads an octal tar header field, treating an empty field as zero. */
function readOctal(source: Buffer): number {
  const text = nullTerminatedText(source).trim();
  return text.length === 0 ? 0 : Number.parseInt(text, 8);
}

/** Reads text from a NUL-terminated tar header field. */
function nullTerminatedText(source: Buffer): string {
  const nullIndex = source.indexOf(0);
  return source.subarray(0, nullIndex === -1 ? source.length : nullIndex).toString("utf8");
}

/** Parses POSIX extended-header records into their key/value pairs. */
function paxAttributes(content: Buffer): Map<string, string> {
  const attributes = new Map<string, string>();
  let offset = 0;
  while (offset < content.length) {
    const separator = content.indexOf(0x20, offset);
    if (separator === -1) throw new Error("invalid PAX record length");
    const recordLength = Number.parseInt(content.subarray(offset, separator).toString("ascii"), 10);
    if (!Number.isSafeInteger(recordLength) || recordLength <= separator - offset) {
      throw new Error("invalid PAX record length");
    }
    const recordEnd = offset + recordLength;
    if (recordEnd > content.length) throw new Error("truncated PAX record");
    const record = content.subarray(separator + 1, recordEnd - 1).toString("utf8");
    const equals = record.indexOf("=");
    if (equals === -1) throw new Error("invalid PAX record");
    attributes.set(record.slice(0, equals), record.slice(equals + 1));
    offset = recordEnd;
  }
  return attributes;
}

/** Reads a gzip-compressed tar archive and applies per-entry PAX attributes. */
export function readTarArchive(archiveFile: string): TarArchiveEntry[] {
  return readRawTarArchive(archiveFile).filter((entry) => entry.type !== "x");
}

/** Reads every gzip-compressed tar header, including PAX extended headers. */
export function readRawTarArchive(archiveFile: string): RawTarArchiveEntry[] {
  const contents = gunzipSync(readFileSync(archiveFile));
  const entries: RawTarArchiveEntry[] = [];
  let attributes = new Map<string, string>();
  let offset = 0;

  while (offset + TAR_BLOCK_SIZE <= contents.length) {
    const header = contents.subarray(offset, offset + TAR_BLOCK_SIZE);
    if (header.every((byte) => byte === 0)) break;

    const rawName = nullTerminatedText(header.subarray(0, 100));
    const type = nullTerminatedText(header.subarray(156, 157));
    const size = readOctal(header.subarray(124, 136));
    const content = contents.subarray(offset + TAR_BLOCK_SIZE, offset + TAR_BLOCK_SIZE + size);
    if (type === "x") {
      entries.push({ name: rawName, rawName, mode: readOctal(header.subarray(100, 108)), content, type });
      attributes = paxAttributes(content);
    } else {
      entries.push({
        name: attributes.get("path") ?? rawName,
        rawName,
        mode: readOctal(header.subarray(100, 108)),
        content,
        type,
      });
      attributes = new Map<string, string>();
    }
    offset += TAR_BLOCK_SIZE + Math.ceil(size / TAR_BLOCK_SIZE) * TAR_BLOCK_SIZE;
  }

  return entries;
}
