import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cleanTypes } from './typings.mjs';

test('portal interfaces keep nested maps, strip nulls, and remove the master model', () => {
  assert.equal(cleanTypes(`export interface _Master_ {
  item: Item;
}
export interface Item {
  name?: string | null;
  custom?: {
    [k: string]: string | null;
  };
  listing?: unknown[];
  [k: string]: unknown;
}
export interface Empty {
}
`), `export interface Item {
  name?: string;
  custom?: {
    [k: string]: string;
  };
  listing?: (SocaBaseModel | unknown)[];
}
export interface Empty {}
`);
});

test('numbered interfaces retain the legacy reference normalization', () => {
  const result = cleanTypes('export interface Item { x: string; }\nexport interface Item1 { x: string; }\nexport type Items = Item1[];');
  assert.equal(result, 'export interface Item { x: string; }\nexport type Items = Item[];\n');
});
