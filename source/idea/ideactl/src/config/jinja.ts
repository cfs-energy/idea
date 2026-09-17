/**
 * The nunjucks environment that stands in for the Python `Jinja2Utils.env_using_file_system_loader`
 * environment, shared by the config generator, the IAM policy renderer and the bootstrap package
 * builder.
 *
 * nunjucks is close enough to Jinja2 for these templates once the differences below are patched.
 * Two kinds of patch live here:
 *
 * 1. Runtime shims (globals and filters): the `True`/`False`/`None` names Python templates use as
 *    literals, `lower`/`upper` on a boolean (nunjucks' own filters throw on a non-string), `indent`
 *    (nunjucks indents the first line and blank lines, Jinja2 3.x indents neither), `tojson`
 *    (Jinja2's `htmlsafe_json_dumps`, which nunjucks does not have at all), `plus` (Python `+` on
 *    two lists), `pyindex` (Python negative indexing) and `utils.to_yaml`, which has to emit exactly
 *    what PyYAML's `yaml.dump` emits because the result is spliced into YAML that is then parsed.
 *
 * 2. A source rewrite in the loader, for Python forms nunjucks either cannot parse or parses into
 *    something else without complaining. The dangerous member of that set is `x in ('a', 'b')`:
 *    nunjucks reads the tuple as a parenthesised expression whose value is its last element, so
 *    `in` degrades to a substring test that is usually false, every gated block silently
 *    disappears, and nothing raises. `list + list`, `seq[-1]`, `dict.items()`, `str.lower()` and
 *    friends, `'sep'.join(seq)` and `{% with %}` are in the same set.
 *
 * The rewrite walks the source the way a lexer does. It knows the three delimiter kinds
 * (`{{ }}`, `{% %}`, `{# #}`), it copies a `{% raw %}` body and a comment through untouched, and
 * inside a tag it masks every string literal before any expression rewrite runs, so text that is
 * data rather than code is never edited. The `{{-`, `-}}`, `{%-` and `-%}` markers are carried
 * across unchanged; the `{%+` spelling is dropped, because it only cancels `lstrip_blocks` and
 * `trim_blocks`, which this environment leaves off, and nunjucks does not accept it.
 *
 * A Python form that has no equivalent this layer can produce raises at load time rather than
 * rendering different bytes: see `unsupported`.
 *
 * Divergence this layer leaves to its callers: Python treats an empty list and an empty dict as
 * false, JavaScript treats both as true, and nunjucks compiles conditions straight to JavaScript
 * truthiness (`if (expr)`, `a || b`, `a ? b : c` in compiler.js), so no filter or global can
 * intercept it. The rewrite could reach it, by turning every truthiness boundary into a call: `a or
 * b` into `a if truthy(a) else b`, `a and b` into `b if truthy(a) else a`, `{% if e %}` into
 * `{% if truthy(e) %}`. That is not done, because it evaluates an operand twice, so a template
 * calling `config.get_list()` in a condition would call it twice, and every boundary would have to
 * be found for the result to be trustworthy. Context builders hand empty containers to the renderer
 * as `undefined` instead. `pythonTruthy` is exported for that.
 */

import nunjucks from 'nunjucks';
import { CORE_SCHEMA, dump } from 'js-yaml';

/** `Utils.to_yaml`: `yaml.dump(json_round_trip(payload), sort_keys=False, width=140)`. */
export function toYaml(value: unknown): string {
  return dump(JSON.parse(JSON.stringify(value ?? null)), {
    noRefs: true,
    lineWidth: 140,
    sortKeys: false,
    // YAML 1.2 quoting only, which is what the reference emits for a json round trip.
    schema: CORE_SCHEMA,
  });
}

/**
 * Jinja2 3.x `do_indent(s, width=4, first=False, blank=False)`, including the trailing-newline
 * quirk: a newline is appended before splitting, so a value ending in `\n` keeps its final
 * (unindented) empty line. A string `width` is the indentation itself, as it is in Jinja2 3.x.
 */
export function jinjaIndent(value: unknown, width: number | string = 4, first = false, blank = false): string {
  const indention = typeof width === 'string' ? width : ' '.repeat(width);
  const source = `${pythonText(value, 'indent')}\n`;
  const lines = source.split('\n');
  lines.pop(); // splitlines(): the final newline does not open a new line
  let rv: string;
  if (blank) {
    rv = lines.join(`\n${indention}`);
  } else {
    const head = lines.shift() ?? '';
    rv = lines.length ? `${head}\n${lines.map((line) => (line ? indention + line : line)).join('\n')}` : head;
  }
  return first ? indention + rv : rv;
}

/**
 * Python truthiness for the values a template context carries: an empty string, an empty list, an
 * empty dict, `0`, `false`, `null` and `undefined` are all false. JavaScript disagrees about the
 * two empty containers, and nunjucks has no hook to correct it, so a context builder whose
 * template branches on a list or a dict has to apply this itself.
 */
export function pythonTruthy(value: unknown): boolean {
  if (value === undefined || value === null || value === false || value === '' || value === 0) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value as object).length > 0;
  return Boolean(value);
}

/** Python compares strings by code point; JavaScript `<` compares UTF-16 code units. */
function compareByCodePoint(left: string, right: string): number {
  const a = Array.from(left);
  const b = Array.from(right);
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    const difference = (a[index].codePointAt(0) as number) - (b[index].codePointAt(0) as number);
    if (difference !== 0) return difference;
  }
  return a.length - b.length;
}

/** Python `json.dumps(value, sort_keys=True)`: sorted keys, `', '`/`': '` separators, ASCII only. */
function pythonJsonDumps(value: unknown): string {
  // JSON.stringify already escapes everything Python's ensure_ascii does, bar the non-ASCII range.
  const nonAscii = new RegExp('[\\u007f-\\uffff]', 'g');
  const escapeString = (text: string): string =>
    JSON.stringify(text).replace(nonAscii, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
  const write = (node: unknown): string => {
    if (node === null || node === undefined) return 'null';
    if (typeof node === 'string') return escapeString(node);
    if (typeof node === 'boolean') return node ? 'true' : 'false';
    if (typeof node === 'number') {
      // Python's default JSON policy writes the three non-finite floats as bare words.
      if (Number.isNaN(node)) return 'NaN';
      if (node === Infinity) return 'Infinity';
      if (node === -Infinity) return '-Infinity';
      // Negative zero is the one JavaScript number that has to have come from a float, so Python's
      // float repr applies. A whole float such as `1.0` is indistinguishable from `1` here.
      if (Object.is(node, -0)) return '-0.0';
      return JSON.stringify(node);
    }
    if (Array.isArray(node)) return `[${node.map(write).join(', ')}]`;
    const entries = Object.entries(node as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => compareByCodePoint(left, right));
    return `{${entries.map(([key, item]) => `${escapeString(key)}: ${write(item)}`).join(', ')}}`;
  };
  return write(value);
}

/**
 * Jinja2 `tojson` = `htmlsafe_json_dumps`: `json.dumps` under the environment's default
 * `sort_keys=True` policy, then `<`, `>`, `&` and `'` replaced by their `\u` escapes so the result
 * is safe inside a `<script>` block.
 */
export function toJson(value: unknown): string {
  return pythonJsonDumps(value)
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026')
    .replace(/'/g, '\\u0027');
}

/**
 * How Python's `str()` renders the values a template interpolates, applied to every `{{ … }}`.
 *
 * `bool` and `None` are corrected: Python writes `True`/`False`/`None` where JavaScript writes
 * `true`/`false`/nothing. `undefined` stays empty, which is what Jinja2's `Undefined` renders, so a
 * context builder distinguishes a Python `None` from a missing name the way the two languages do.
 * A list or a plain object raises, because Python writes a `repr` (`['a', 'b']`, `{'k': 'v'}`)
 * where JavaScript writes `a,b` and `[object Object]`, and that difference is silent. Everything
 * else is handed back untouched, so nunjucks renders it as before.
 */
export function pythonStr(value: unknown): unknown {
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (value === null) return 'None';
  if (Array.isArray(value) || (typeof value === 'object' && value !== null && value.constructor === Object)) {
    throw new Error(`jinja compatibility: Python writes a repr for ${describe(value)} written bare, JavaScript does not`);
  }
  return value;
}

/** Python `+`: list concatenation for two lists, ordinary addition otherwise. */
export function pythonPlus(left: unknown, right: unknown): unknown {
  if (Array.isArray(left) && Array.isArray(right)) return [...left, ...right];
  if (Array.isArray(left) || Array.isArray(right)) {
    throw new Error(`jinja compatibility: Python cannot add ${describe(left)} and ${describe(right)}`);
  }
  if (typeof left === 'number' && typeof right === 'number') return left + right;
  return `${left as string}${right as string}`;
}

function describe(value: unknown): string {
  return Array.isArray(value) ? 'a list' : value === null ? 'None' : isPlainObject(value) ? 'a dict' : `a ${typeof value}`;
}

function isPlainObject(value: unknown): boolean {
  return typeof value === 'object' && value !== null && value.constructor === Object;
}

/**
 * What a string filter sees. Python renders `None` as the text `None` and an Undefined name as
 * nothing. A list or a dict raises: Python would apply the filter to a `repr` (`['a', 'b']`), and
 * JavaScript's `String()` gives `a,b`, so coercing would quietly change the bytes.
 */
function pythonText(value: unknown, filter: string): string {
  if (value === undefined) return '';
  if (value === null) return 'None';
  if (Array.isArray(value) || isPlainObject(value)) {
    throw new Error(`jinja compatibility: \`${filter}\` on ${describe(value)} would need Python's repr`);
  }
  return String(value);
}

/**
 * Python indexing, including the negative form JavaScript does not have. A string is indexed by
 * code point, and an index outside the sequence raises the way Python's `IndexError` does rather
 * than rendering empty.
 */
export function pyIndex(value: unknown, index: number): unknown {
  const items = typeof value === 'string' ? Array.from(value) : value;
  if (!Array.isArray(items)) {
    throw new Error(`jinja compatibility: ${describe(value)} cannot be indexed by position`);
  }
  const at = index < 0 ? items.length + index : index;
  // Jinja2's `getitem` turns a LookupError into `Undefined`, which renders empty.
  return at < 0 || at >= items.length ? undefined : items[at];
}

/**
 * A Python form with no equivalent in this layer. Raising at load time is the point: the whole
 * reason the rewrite exists is that a mistranslation is silent, so anything it cannot translate
 * has to stop the render instead of producing different bytes.
 */
function unsupported(what: string, where: string): never {
  throw new Error(`jinja compatibility: ${what} has no nunjucks equivalent, in ${where.trim()}`);
}

/* ------------------------------------------------------------------------------------------- *
 * Source rewrite
 * ------------------------------------------------------------------------------------------- */

/** Stands in for a string literal while an expression is rewritten. */
const LITERAL_MARK = '\u0001';
/** Marks a `.` whose call the rewrite has already looked at and left alone. */
const SKIP_MARK = '\u0002';
/** Marks a `+` the rewrite has already looked at and left alone. */
const PLUS_MARK = '\u0003';
/** An identifier character, or one of the digits inside a masked string literal. */
const WORD = /[\w\u0001]/;
/** Words that can stand immediately before `(` without making it a call. */
const KEYWORDS = new Set(['and', 'else', 'elif', 'for', 'if', 'in', 'is', 'not', 'or', 'return', 'set', 'with']);

/** Index just past the closing quote of the literal that starts at `start`, or -1. */
function literalEnd(text: string, start: number): number {
  const quote = text[start];
  for (let index = start + 1; index < text.length; index += 1) {
    if (text[index] === '\\') {
      index += 1;
      continue;
    }
    if (text[index] === quote) return index + 1;
  }
  return -1;
}

/** Index just past `closer`, skipping quoted text the way the Jinja2 lexer does. Or -1. */
function tagEnd(source: string, from: number, closer: string): number {
  for (let index = from; index < source.length; index += 1) {
    const character = source[index];
    if (character === "'" || character === '"') {
      const end = literalEnd(source, index);
      if (end < 0) return -1;
      index = end - 1;
      continue;
    }
    if (source.startsWith(closer, index)) return index + closer.length;
  }
  return -1;
}

const CLOSERS: Record<string, string> = { '(': ')', '[': ']', '{': '}' };

/** Index of the bracket closing the one at `openIndex`, or -1. */
function matchingClose(text: string, openIndex: number): number {
  const open = text[openIndex];
  const close = CLOSERS[open];
  let depth = 0;
  for (let index = openIndex; index < text.length; index += 1) {
    if (text[index] === open) depth += 1;
    else if (text[index] === close) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return -1;
}

/** Index of the bracket opening the one at `closeIndex`, or -1. */
function matchingOpen(text: string, closeIndex: number): number {
  const close = text[closeIndex];
  const open = Object.keys(CLOSERS).find((candidate) => CLOSERS[candidate] === close) as string;
  let depth = 0;
  for (let index = closeIndex; index >= 0; index -= 1) {
    if (text[index] === close) depth += 1;
    else if (text[index] === open) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return -1;
}

/**
 * Start of the primary expression that ends at `end`: an identifier, a masked literal, a bracketed
 * group, and any attribute, subscript or call chained onto it. -1 when there is no primary there.
 *
 * One leading space is crossed, because Python allows `mapping . items()` and `mapping .items()`.
 * Inside the chain a space ends the primary, so `not x` stays two tokens, and a chain that starts
 * with a keyword is not a primary at all, so the `[-1]` in `x in [-1]` is a list literal.
 */
function primaryStart(text: string, end: number): number {
  let index = end;
  while (index > 0 && /\s/.test(text[index - 1])) index -= 1;
  const stop = index;
  for (;;) {
    if (index === 0) break;
    const previous = text[index - 1];
    if (previous === ')' || previous === ']') {
      const open = matchingOpen(text, index - 1);
      if (open < 0) return -1;
      index = open;
      continue;
    }
    if (WORD.test(previous)) {
      while (index > 0 && WORD.test(text[index - 1])) index -= 1;
      continue;
    }
    if (previous === '.') {
      index -= 1;
      continue;
    }
    break;
  }
  if (index >= stop) return -1;
  const first = /^[\w\u0001]+/.exec(text.slice(index));
  return first !== null && KEYWORDS.has(first[0]) ? -1 : index;
}

/** End of the primary expression that starts at or after `from`, and where it started. */
function primaryRange(text: string, from: number): { start: number; end: number } | null {
  let index = from;
  while (index < text.length && /\s/.test(text[index])) index += 1;
  const start = index;
  if (index < text.length && CLOSERS[text[index]] !== undefined) {
    const close = matchingClose(text, index);
    if (close < 0) return null;
    index = close + 1;
  } else if (index < text.length && WORD.test(text[index])) {
    while (index < text.length && WORD.test(text[index])) index += 1;
  } else return null;
  for (;;) {
    if (index < text.length && (text[index] === '(' || text[index] === '[')) {
      const close = matchingClose(text, index);
      if (close < 0) return null;
      index = close + 1;
      continue;
    }
    if (index + 1 < text.length && text[index] === '.' && WORD.test(text[index + 1])) {
      index += 1;
      while (index < text.length && WORD.test(text[index])) index += 1;
      continue;
    }
    break;
  }
  return { start, end: index };
}

/** True when the `(` at `openIndex` belongs to a call rather than opening a tuple. */
function isCallParen(text: string, openIndex: number): boolean {
  let index = openIndex;
  while (index > 0 && /\s/.test(text[index - 1])) index -= 1;
  if (index === 0) return false;
  const previous = text[index - 1];
  if (previous === ')' || previous === ']') return true;
  if (!WORD.test(previous)) return false;
  let start = index;
  while (start > 0 && WORD.test(text[start - 1])) start -= 1;
  return !KEYWORDS.has(text.slice(start, index));
}

/** Splits `a = 1, b = 2` on the commas that are not inside quotes, brackets or parentheses. */
function splitTopLevel(text: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let quote = '';
  let start = 0;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quote !== '') {
      if (character === quote && text[index - 1] !== '\\') quote = '';
      continue;
    }
    if (character === "'" || character === '"') quote = character;
    else if (character === '(' || character === '[' || character === '{') depth += 1;
    else if (character === ')' || character === ']' || character === '}') depth -= 1;
    else if (character === ',' && depth === 0) {
      parts.push(text.slice(start, index));
      start = index + 1;
    }
  }
  parts.push(text.slice(start));
  return parts.map((part) => part.trim()).filter((part) => part !== '');
}

/**
 * Python's escapes inside a string literal. nunjucks' lexer knows only `\n`, `\t` and `\r` and
 * drops the backslash from everything else, where Python keeps it: `'\ '` is a backslash and a
 * space in Jinja2 and a bare space in nunjucks, so a value escaped for a shell arrives unescaped.
 * Literals are decoded the way Python decodes them and re-emitted so nunjucks' lexer produces the
 * same string. A literal with no backslash in it is passed through untouched.
 */
const PYTHON_ESCAPES: Record<string, string> = {
  '\\': '\\',
  "'": "'",
  '"': '"',
  a: '\u0007',
  b: '\b',
  f: '\f',
  n: '\n',
  r: '\r',
  t: '\t',
  v: '\v',
  '\n': '',
};

function decodePythonLiteral(body: string): string {
  let out = '';
  for (let index = 0; index < body.length; index += 1) {
    if (body[index] !== '\\') {
      out += body[index];
      continue;
    }
    const next = body[index + 1] ?? '';
    const simple = PYTHON_ESCAPES[next];
    if (simple !== undefined) {
      out += simple;
      index += 1;
      continue;
    }
    if (next === 'N') {
      // Python resolves `\N{SNOWMAN}` from the Unicode name table, which Node does not carry.
      unsupported('a named Unicode escape', body);
    }
    if (next === 'x' || next === 'u' || next === 'U') {
      const widths: Record<string, number> = { x: 2, u: 4, U: 8 };
      const digits = body.slice(index + 2, index + 2 + widths[next]);
      if (!new RegExp(`^[0-9a-fA-F]{${widths[next]}}$`).test(digits)) {
        unsupported(`the truncated escape \\${next}${digits}`, body);
      }
      out += String.fromCodePoint(Number.parseInt(digits, 16));
      index += 1 + widths[next];
      continue;
    }
    const octal = /^[0-7]{1,3}/.exec(body.slice(index + 1));
    if (octal !== null) {
      out += String.fromCharCode(Number.parseInt(octal[0], 8));
      index += octal[0].length;
      continue;
    }
    out += '\\'; // not an escape Python knows: the backslash stays
  }
  return out;
}

/** Re-emits one literal so nunjucks' lexer yields the string Python's lexer yields. */
function reencodeStringLiteral(literal: string): string {
  if (!literal.includes('\\')) return literal;
  const quote = literal[0];
  const value = decodePythonLiteral(literal.slice(1, -1));
  const encoded = value
    .replace(/\\/g, '\\\\')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\t/g, '\\t')
    .split(quote)
    .join(`\\${quote}`);
  return `${quote}${encoded}${quote}`;
}

interface Masked {
  readonly text: string;
  readonly literals: readonly string[];
}

/** Replaces every string literal with a mark, so no expression rewrite can see inside one. */
function maskLiterals(body: string): Masked {
  const literals: string[] = [];
  let text = '';
  let index = 0;
  while (index < body.length) {
    const character = body[index];
    if (character === "'" || character === '"') {
      const end = literalEnd(body, index);
      if (end < 0) unsupported('an unterminated string literal', body);
      literals.push(body.slice(index, end));
      text += `${LITERAL_MARK}${literals.length - 1}${LITERAL_MARK}`;
      index = end;
      continue;
    }
    if (character === LITERAL_MARK || character === SKIP_MARK || character === PLUS_MARK) {
      unsupported('a C0 control character in an expression', body);
    }
    text += character;
    index += 1;
  }
  return { text, literals };
}

function unmask(text: string, literals: readonly string[]): string {
  return text.replace(/\u0001(\d+)\u0001/g, (_whole, digits: string) => reencodeStringLiteral(literals[Number(digits)]));
}

function isMaskedLiteral(text: string): boolean {
  return /^\u0001\d+\u0001$/.test(text);
}

/** A `+` operand Python adds rather than concatenates as a list. */
function isScalarOperand(text: string): boolean {
  return isMaskedLiteral(text) || /^-?\d/.test(text) || /^(True|False|None|true|false|null)$/.test(text);
}

/** `('a', 'b')` is a tuple wherever it appears, and nunjucks only understands the list form. */
function rewriteTuples(text: string): string {
  let out = text;
  let index = 0;
  while (index < out.length) {
    if (out[index] !== '(' || isCallParen(out, index)) {
      index += 1;
      continue;
    }
    const close = matchingClose(out, index);
    if (close < 0) break;
    const inner = out.slice(index + 1, close);
    const parts = splitTopLevel(inner);
    // `('efs',)` is a one-element tuple; nunjucks rejects the trailing comma in a list literal.
    if (parts.length > 1 || parts.length === 0 || /,\s*$/.test(inner)) {
      out = `${out.slice(0, index)}[${parts.join(', ')}]${out.slice(close + 1)}`;
    }
    index += 1; // and on into the group, so a nested tuple is converted too
  }
  return out;
}

const METHOD_CALL = /\.\s*([A-Za-z_]\w*)\s*\(/g;

/**
 * Python string and dict methods, rewritten right to left so a chained call sees the already
 * rewritten receiver. A method this layer cannot reproduce raises rather than rendering.
 */
function rewriteMethods(text: string, twoTargetFor: boolean, where: string): string {
  let out = text;
  for (let guard = 0; guard <= out.length + 16; guard += 1) {
    const matches = [...out.matchAll(METHOD_CALL)];
    if (matches.length === 0) return out.replaceAll(SKIP_MARK, '.');
    const match = matches[matches.length - 1];
    const dot = match.index;
    const name = match[1];
    const close = matchingClose(out, dot + match[0].length - 1);
    const start = primaryStart(out, dot);
    const skip = (): void => {
      out = `${out.slice(0, dot)}${SKIP_MARK}${out.slice(dot + 1)}`;
    };
    if (close < 0 || start < 0) {
      skip();
      continue;
    }
    const receiver = out.slice(start, dot).trimEnd();
    const args = splitTopLevel(out.slice(dot + match[0].length, close));
    const put = (replacement: string): void => {
      out = `${out.slice(0, start)}${replacement}${out.slice(close + 1)}`;
    };
    if (name === 'lower' || name === 'upper' || name === 'strip') {
      if (args.length !== 0) unsupported(`\`.${name}()\` with an argument`, where);
      put(`(${receiver} | ${name === 'strip' ? 'trim' : name})`);
    } else if (name === 'replace') {
      if (args.length !== 2) unsupported('`.replace()` with a count argument', where);
      put(`(${receiver} | replace(${args.join(', ')}))`);
    } else if (name === 'join') {
      if (args.length !== 1) unsupported('`.join()` with no sequence argument', where);
      put(`((${args[0]}) | join(${receiver}))`);
    } else if (name === 'items') {
      // nunjucks unpacks a plain object in a two-target loop; anywhere else Python writes a repr.
      if (args.length !== 0 || !twoTargetFor) unsupported('`.items()` outside a two-target for loop', where);
      put(receiver);
    } else if (name === 'split') {
      // JavaScript's `split` matches Python's for a string separator, but not for no separator.
      if (args.length === 0) unsupported('`.split()` with no separator', where);
      skip();
    } else {
      skip();
    }
  }
  unsupported('an expression too deeply chained to rewrite', where);
}

/** `a + b + c` on lists: Python concatenates, JavaScript stringifies. */
function rewritePlus(text: string): string {
  let out = text;
  for (let guard = 0; guard <= out.length + 16; guard += 1) {
    const at = out.indexOf('+');
    if (at < 0) return out.replaceAll(PLUS_MARK, '+');
    let leftEnd = at;
    while (leftEnd > 0 && /\s/.test(out[leftEnd - 1])) leftEnd -= 1;
    const leftStart = primaryStart(out, leftEnd);
    const right = primaryRange(out, at + 1);
    const mark = (): void => {
      out = `${out.slice(0, at)}${PLUS_MARK}${out.slice(at + 1)}`;
    };
    if (leftStart < 0 || right === null) {
      mark();
      continue;
    }
    const leftText = out.slice(leftStart, leftEnd);
    const rightText = out.slice(right.start, right.end);
    if (isScalarOperand(leftText) || isScalarOperand(rightText)) {
      mark();
      continue;
    }
    out = `${out.slice(0, leftStart)}(${leftText} | plus(${rightText}))${out.slice(right.end)}`;
  }
  return out.replaceAll(PLUS_MARK, '+');
}

/** `seq[-1]`: JavaScript has no negative index and returns undefined, which renders empty. */
function rewriteNegativeIndex(text: string): string {
  let out = text;
  let index = 0;
  while (index < out.length) {
    if (out[index] !== '[') {
      index += 1;
      continue;
    }
    const close = matchingClose(out, index);
    if (close < 0) break;
    const inner = out.slice(index + 1, close).trim();
    const start = primaryStart(out, index);
    if (start >= 0 && inner.startsWith('-')) {
      const replacement = `(${out.slice(start, index).trimEnd()} | pyindex(${inner}))`;
      out = `${out.slice(0, start)}${replacement}${out.slice(close + 1)}`;
      index = start + replacement.length;
      continue;
    }
    index += 1;
  }
  return out;
}

/** The Python-only expression forms, rewritten wherever an expression may appear. */
function rewriteExpression(body: string, twoTargetFor: boolean): string {
  const { text, literals } = maskLiterals(body);
  const rewritten = rewriteNegativeIndex(rewritePlus(rewriteMethods(rewriteTuples(text), twoTargetFor, body)));
  return unmask(rewritten, literals);
}

/** Index just past the `{% endraw %}` that closes the raw block open at `from`. */
function endOfRawBlock(source: string, from: number): number {
  let index = from;
  for (;;) {
    const at = source.indexOf('{%', index);
    if (at < 0) return source.length;
    const end = tagEnd(source, at + 2, '%}');
    if (end < 0) return source.length;
    if (/^\{%[-+]?\s*endraw\s*[-+]?%\}$/.test(source.slice(at, end))) return end;
    index = end;
  }
}

interface WithBlock {
  readonly id: number;
  readonly names: readonly string[];
}

/**
 * Rewrites the Python-only forms inside `{{ … }}` and `{% … %}` into the nunjucks forms with the
 * same meaning. A comment, a `{% raw %}` body, the text between tags and the inside of every
 * string literal are left byte for byte alone.
 */
export function normalizePythonSyntax(source: string): string {
  let out = '';
  let index = 0;
  let withCount = 0;
  let stripNextText = false;
  const withStack: WithBlock[] = [];
  // A `{% raw %}` tag is emitted without its whitespace-control markers, because nunjucks rejects
  // that spelling, so the rewrite applies the trim itself.
  const emitText = (text: string): void => {
    out += stripNextText ? text.replace(/^\s+/, '') : text;
    stripNextText = false;
  };
  while (index < source.length) {
    const open = source.indexOf('{', index);
    if (open < 0 || open + 1 >= source.length) {
      emitText(source.slice(index));
      return out;
    }
    const kind = source[open + 1];
    if (kind !== '{' && kind !== '%' && kind !== '#') {
      emitText(source.slice(index, open + 1));
      index = open + 1;
      continue;
    }
    emitText(source.slice(index, open));
    if (kind === '#') {
      const end = source.indexOf('#}', open + 2);
      if (end < 0) {
        out += source.slice(open);
        return out;
      }
      out += source.slice(open, end + 2);
      index = end + 2;
      continue;
    }
    const end = tagEnd(source, open + 2, kind === '{' ? '}}' : '%}');
    if (end < 0) {
      out += source.slice(open);
      return out;
    }
    const tag = source.slice(open, end);
    index = end;
    const parts = splitTag(tag);
    if (parts === null) {
      out += tag;
      continue;
    }
    const { openMark, body, closeMark } = parts;
    const keyword = /^\s*([A-Za-z_]\w*)/.exec(body)?.[1] ?? '';
    if (kind === '%' && keyword === 'raw') {
      const closeRaw = endOfRawBlock(source, index);
      const endTag = splitTag(/\{%[-+]?\s*endraw\s*[-+]?%\}$/.exec(source.slice(index, closeRaw))?.[0] ?? '{% endraw %}');
      if (openMark.endsWith('-')) out = out.replace(/\s+$/, '');
      let raw = source.slice(index, closeRaw - (endTag === null ? 0 : endTag.length));
      if (closeMark.startsWith('-')) raw = raw.replace(/^\s+/, '');
      if (endTag !== null && endTag.openMark.endsWith('-')) raw = raw.replace(/\s+$/, '');
      stripNextText = endTag !== null && endTag.closeMark.startsWith('-');
      out += `{% raw %}${raw}{% endraw %}`;
      index = closeRaw;
      continue;
    }
    if (kind === '%' && keyword === 'with') {
      out += openWith(body, openMark, closeMark, withCount, withStack);
      withCount += 1;
      continue;
    }
    if (kind === '%' && keyword === 'endwith') {
      const block = withStack.pop();
      if (block === undefined) unsupported('an `endwith` with no `with`', tag);
      out += setTags(
        block.names.map((name, position) => `${name} = ${temporary(block.id, `s${position}`)}`),
        openMark,
        closeMark,
        block.id,
      );
      continue;
    }
    // `{% for k, v in … %}` is the one place `.items()` has a nunjucks equivalent.
    const twoTargetFor = /^\s*for\s+[A-Za-z_]\w*\s*,\s*[A-Za-z_]\w*\s+in\s/.test(body);
    const rewritten = rewriteExpression(body, twoTargetFor);
    if (kind === '%') {
      out += `${openMark}${rewritten}${closeMark}`;
      continue;
    }
    // `{{ flag }}` writes `True`/`False` in Python. `pystr` returns anything else unchanged, so
    // wrapping every output tag is safe and catches a boolean a filter produced.
    out += rewritten.trim() === '' ? tag : `${openMark} (${rewritten.trim()}) | pystr ${closeMark}`;
  }
  return out;
}

interface TagParts {
  readonly openMark: string;
  readonly body: string;
  readonly closeMark: string;
  readonly length: number;
}

/**
 * Splits a tag into its delimiters and its expression. `{%+` and `+%}` only cancel
 * `lstrip_blocks`/`trim_blocks`, which this environment leaves off, so they mean exactly what the
 * bare delimiter means and are dropped: nunjucks does not accept that spelling.
 */
function splitTag(tag: string): TagParts | null {
  const opening = /^\{[{%][-+]?/.exec(tag);
  const closing = /[-+]?[}%]\}$/.exec(tag);
  if (opening === null || closing === null || opening[0].length + closing[0].length > tag.length) return null;
  return {
    openMark: opening[0].replace('+', ''),
    body: tag.slice(opening[0].length, tag.length - closing[0].length),
    closeMark: closing[0].replace('+', ''),
    length: tag.length,
  };
}

function temporary(id: number, suffix: string): string {
  return `__with_${id}_${suffix}`;
}

function setTags(assignments: readonly string[], openMark: string, closeMark: string, id: number): string {
  const statements = assignments.length === 0 ? [`${temporary(id, 'held')} = None`] : assignments;
  return statements
    .map((statement, position) => {
      const first = position === 0 ? openMark : '{%';
      const last = position === statements.length - 1 ? closeMark : '%}';
      return `${first} set ${statement} ${last}`;
    })
    .join('');
}

/**
 * nunjucks has no `with` tag. Jinja2 evaluates every right-hand side against the outer scope
 * before it installs any of the new bindings, and restores the outer bindings at `endwith`, so the
 * rewrite reads each value into a temporary, saves whatever the names held, and assigns. The
 * `endwith` puts the saved values back.
 */
function openWith(body: string, openMark: string, closeMark: string, id: number, stack: WithBlock[]): string {
  const assignments = splitTopLevel(body.replace(/^\s*with\s*/, ''));
  const names: string[] = [];
  const values: string[] = [];
  for (const assignment of assignments) {
    const parsed = /^([A-Za-z_]\w*)\s*=\s*([\s\S]+)$/.exec(assignment);
    if (parsed === null) unsupported(`the \`with\` binding \`${assignment}\``, body);
    names.push(parsed[1]);
    values.push(rewriteExpression(parsed[2], false));
  }
  stack.push({ id, names });
  return setTags(
    [
      ...values.map((value, position) => `${temporary(id, String(position))} = ${value}`),
      ...names.map((name, position) => `${temporary(id, `s${position}`)} = ${name}`),
      ...names.map((name, position) => `${name} = ${temporary(id, String(position))}`),
    ],
    openMark,
    closeMark,
    id,
  );
}

/**
 * Jinja2's `newline_sequence='\n'` and `keep_trailing_newline=False` defaults: the source is
 * normalized to LF and a single newline at the end of a template file is stripped *before*
 * rendering, so a value interpolated at the end keeps its own trailing newline. nunjucks has
 * neither option, so the loader does both, along with the Python-syntax rewrite that has to happen
 * before the parser sees the source.
 */
function pythonCompatibleLoader(templatesDir: string | string[]): nunjucks.FileSystemLoader {
  const loader = new nunjucks.FileSystemLoader(templatesDir, { watch: false });
  const getSource = loader.getSource.bind(loader);
  loader.getSource = (name: string) => {
    const source = getSource(name);
    if (source === null || typeof source.src !== 'string') return source;
    return { ...source, src: normalizePythonSyntax(source.src.replace(/\r\n?/g, '\n').replace(/\n$/, '')) };
  };
  return loader;
}

/** `templatesDir` may be several directories, searched in order, as a Jinja loader list is. */
export function jinjaEnv(templatesDir: string | string[]): nunjucks.Environment {
  const env = new nunjucks.Environment(pythonCompatibleLoader(templatesDir), {
    autoescape: false,
    throwOnUndefined: false,
    trimBlocks: false,
    lstripBlocks: false,
  });
  // Python literals used as bare names in the templates (`default=False`, `{'x': True}`).
  env.addGlobal('True', true);
  env.addGlobal('False', false);
  env.addGlobal('None', null);
  // nunjucks' `lower`/`upper` are string-only; Python renders a bool as 'true'/'TRUE', `None` as
  // 'none'/'NONE', and an undefined name as ''.
  env.addFilter('lower', (value: unknown) => pythonText(value, 'lower').toLowerCase());
  env.addFilter('upper', (value: unknown) => pythonText(value, 'upper').toUpperCase());
  env.addFilter('indent', jinjaIndent);
  env.addFilter('tojson', toJson);
  env.addFilter('plus', pythonPlus);
  env.addFilter('pystr', pythonStr);
  env.addFilter('pyindex', pyIndex);
  return env;
}

export function renderTemplate(env: nunjucks.Environment, name: string, ctx: object): string {
  return env.render(name, ctx as Record<string, unknown>);
}
