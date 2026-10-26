// Copyright (c) 2020 Phillip Dupuis
//
// Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files
// (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge,
// publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
// subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
// MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
// FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
// CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

// Schema post-processing preserves the portal's existing generated interface contract.
// Adapted from the former typings_generator.py (Phillip Dupuis, MIT license).
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pythonEnv, root, run, source } from './common.mjs';

export function cleanTypes(content) {
  content = content.replace(/export interface _Master_ \{[^}]*\}\n?/s, '')
    .replace(/(string|number|boolean|unknown\[\]|string\[\]) \| null/g, '$1')
    .replace(/(\w+(?:\[\])?|\{[^{}]*\}) \| null/g, '$1')
    .replace(/(\{\n(?:[^{}]|(?:\{[^{}]*\}))*\n\s*\}) \| null/gs, (_, value) => value.replaceAll(' | null', ''));
  let depth = 0;
  content = content.split('\n').filter(line => {
    if (/^export interface \w+ \{/.test(line)) depth = 0;
    const keep = !(depth === 1 && line.trim() === '[k: string]: unknown;');
    depth += (line.match(/\{/g) ?? []).length - (line.match(/\}/g) ?? []).length;
    return keep;
  }).join('\n').replace(/(export interface \w+) \{\n\s*\}/g, '$1 {}');
  const groups = new Map();
  for (const match of content.matchAll(/export interface (\w+?)(\d+) \{([^}]*)\}/gs)) {
    const variants = groups.get(match[1]) ?? [];
    variants.push([Number(match[2]), match[3]]);
    groups.set(match[1], variants);
  }
  for (const [base, variants] of groups) {
    variants.sort((a, b) => a[0] - b[0]);
    const exists = new RegExp(`export interface ${base} \\{`).test(content);
    for (const [number] of variants) {
      if (!exists && number === variants[0][0]) {
        content = content.replace(`export interface ${base}${number} {`, `export interface ${base} {`);
      } else {
        content = content.replace(new RegExp(`export interface ${base}${number} \\{[^}]*\\}`, 'gs'), '');
      }
      content = content.replace(new RegExp(`${base}${number}\\b`, 'g'), base);
    }
  }
  return content.replace(/(\s+listing\??:\s+)unknown(\[\];)/g, '$1(SocaBaseModel | unknown)$2')
    .split('\n').filter(line => line.trim()).join('\n') + '\n';
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  const directory = mkdtempSync(join(tmpdir(), 'idea-typings-'));
  try {
    const schema = join(directory, 'schema.json');
    writeFileSync(schema, run('python', ['-m', 'ideasdk.model_schema'], { env: pythonEnv, stdio: 'pipe' }));
    const output = join(directory, 'data-model.ts');
    run('json2ts', ['-i', schema, '-o', output, '--no-strict-nulls', '--no-union-null']);
    const notice = readFileSync(join(source, 'idea-sdk/src/ideasdk/notice.py'), 'utf8')
      .match(/COPYRIGHT_NOTICE\s*=\s*'''([\s\S]*?)'''|COPYRIGHT_NOTICE\s*=\s*"""([\s\S]*?)"""/);
    if (!notice) throw new Error('Copyright notice not found');
    const header = ['/* tslint:disable */', '/* eslint-disable */',
      '/* This file is generated using mise run web-portal:typings. */',
      '/* Do not modify this file manually. */', '/**',
      ...(notice[1] ?? notice[2]).trim().split('\n').map(line => ` *${line.trimEnd() ? ` ${line.trimEnd()}` : ''}`), ' */'].join('\n');
    const content = cleanTypes(readFileSync(output, 'utf8')).replace(/^[\s\S]*?(?=export\s+(?:type|interface|enum))/, '');
    const target = join(root, 'source/idea/idea-cluster-manager/webapp/src/client/data-model.ts');
    writeFileSync(target, header + '\n' + content);
    console.log(`Generated ${target}`);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}
