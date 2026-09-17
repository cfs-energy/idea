import { createHash } from 'node:crypto';

/** Python `Utils.shake_256(data, num_bytes)`: shake256 of the utf-8 bytes, `num_bytes` of hex. */
export function shake256Hex(input: string, bytes: number): string {
  return createHash('shake256', { outputLength: bytes }).update(input, 'utf8').digest('hex');
}
