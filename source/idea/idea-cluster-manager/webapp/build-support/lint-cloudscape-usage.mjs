// Three Cloudscape usage rules that nothing else in the toolchain catches. Runs over the file paths
// given as arguments (hk), or over src/ when called with none (yarn lint).

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

// CommonJS resolution, so oxc-parser is found in webapp/node_modules or, when hk runs this
// before any yarn install, in mise's tool environment via NODE_PATH. An ESM bare import ignores that.
// The parser is not the TypeScript compiler, whose JavaScript API TypeScript 7 no longer exports.
const { parseSync, visitorKeys } = createRequire(import.meta.url)('oxc-parser');

const WEBAPP_ROOT = fileURLToPath(new URL('..', import.meta.url));
const EXTENSIONS = ['.ts', '.tsx', '.js', '.jsx', '.css', '.scss'];

// Rule 1: Cloudscape class names carry a per-release build hash, so source that hardcodes one stops
// matching on the next upgrade with no error and no failing test. In awsui_<name>_<file-hash>_<build-
// hash>_<line> only <name> survives a release, and <name> can itself contain underscores (BEM).
const HASHED_CLASS_NAME = /\bawsui_[a-zA-Z0-9_-]+_[a-z0-9]+_[a-z0-9]+_\d+\b/;

// Opt out on the preceding line, with a reason, e.g. a normalization fixture.
const ALLOW_MARKER = 'awsui-hashed-class-name-allowed';

// Rule 2: these components flatten React.Fragment children on React 18 but hand them straight to
// React.Children.toArray on 19, so a fragment that expanded into N grid cells collapses into one.
// flattenChildren's consumers in the installed package: column-layout, control-group, grid, space-between.
const FLATTEN_CONSUMERS = new Set(['ColumnLayout', 'ControlGroup', 'Grid', 'SpaceBetween']);

const FRAGMENT_MARKER = 'cloudscape-fragment-child-allowed';

// Rule 3: presentational HTML in JSX bypasses the design system: bold and line-break tags, spacer
// entities and inline styles render browser defaults and never follow the theme. Prose belongs in
// TextContent (which styles native tags), spacing in SpaceBetween/Box, emphasis in Box variants.
const RAW_MARKUP_TAGS = new Set(['b', 'i', 'u', 'br', 'font', 'center', 'small', 'big']);
const RAW_MARKUP_ENTITIES = /&nbsp;|&ensp;|&emsp;/;
const RAW_MARKUP_MARKER = 'cloudscape-raw-markup-allowed';
const PROSE_WRAPPER = 'TextContent';

function collect(directory, found = []) {
    for (const entry of readdirSync(directory)) {
        if (entry === 'node_modules') {
            continue;
        }
        const path = join(directory, entry);
        if (statSync(path).isDirectory()) {
            collect(path, found);
        } else if (EXTENSIONS.some((extension) => path.endsWith(extension))) {
            found.push(path);
        }
    }
    return found;
}

// Line numbers follow the TypeScript scanner, which also breaks lines at a lone CR, U+2028 and U+2029.
function lineStarts(text) {
    const starts = [0];
    for (let index = 0; index < text.length; index++) {
        const code = text.charCodeAt(index);
        if (code === 0x0d && text.charCodeAt(index + 1) === 0x0a) {
            index++;
        }
        if (code === 0x0a || code === 0x0d || code === 0x2028 || code === 0x2029) {
            starts.push(index + 1);
        }
    }
    return starts;
}

function lineOf(starts, offset) {
    let low = 0;
    let high = starts.length - 1;
    while (low < high) {
        const middle = (low + high + 1) >> 1;
        if (starts[middle] <= offset) {
            low = middle;
        } else {
            high = middle - 1;
        }
    }
    return low;
}

// Whitespace the TypeScript scanner skips before a JSX text token, so a text node is reported on the
// line of its first visible character, not on the line where the previous tag ended.
function isTrivia(code) {
    return (
        code === 0x20 ||
        (code >= 0x09 && code <= 0x0d) ||
        code === 0xa0 ||
        code === 0x85 ||
        code === 0x1680 ||
        (code >= 0x2000 && code <= 0x200b) ||
        code === 0x2028 ||
        code === 0x2029 ||
        code === 0x202f ||
        code === 0x205f ||
        code === 0x3000 ||
        code === 0xfeff
    );
}

function startOf(node, text) {
    let offset = node.start;
    if (node.type === 'JSXText') {
        while (offset < node.end && isTrivia(text.charCodeAt(offset))) {
            offset++;
        }
    }
    return offset;
}

// Child nodes in source order.
function children(node) {
    const found = [];
    for (const key of visitorKeys[node.type] ?? []) {
        const value = node[key];
        for (const child of Array.isArray(value) ? value : [value]) {
            if (child != null && typeof child.type === 'string') {
                found.push(child);
            }
        }
    }
    return found.sort((left, right) => left.start - right.start);
}

// A file that does not parse yields an empty program, so its errors are reported rather than
// letting it pass with nothing checked.
function parseErrors(errors, starts) {
    return errors
        .filter((error) => error.severity === 'Error')
        .map((error) => ({ line: error.labels?.[0] == null ? 1 : lineOf(starts, error.labels[0].start) + 1, message: error.message }));
}

const isElement = (node) => node.type === 'JSXElement' && !node.openingElement.selfClosing;
const isSelfClosing = (node) => node.type === 'JSXElement' && node.openingElement.selfClosing;

function tagName(node, text) {
    const name = node.openingElement.name;
    return text.slice(name.start, name.end);
}

function isFragmentElement(node, text) {
    if (node.type === 'JSXFragment') {
        return true;
    }
    if (!isElement(node)) {
        return false;
    }
    const name = tagName(node, text);
    return name === 'React.Fragment' || name === 'Fragment';
}

// A fragment reaches the parent component as a child unless some other JSX
// element encloses it first, so stop descending at the first JSX element.
function findLooseFragments(node, text, found = []) {
    if (isFragmentElement(node, text)) {
        found.push(node);
        return found;
    }
    if (node.type === 'JSXElement') {
        return found;
    }
    for (const child of children(node)) {
        findLooseFragments(child, text, found);
    }
    return found;
}

function scanFragments(program, text, starts) {
    const lines = text.split('\n');
    const violations = [];

    const report = (node, parent) => {
        const line = lineOf(starts, startOf(node, text));
        if (line > 0 && lines[line - 1].includes(FRAGMENT_MARKER)) {
            return;
        }
        violations.push({ line: line + 1, parent });
    };

    const walk = (node) => {
        if (isElement(node) && FLATTEN_CONSUMERS.has(tagName(node, text))) {
            const parent = tagName(node, text);
            for (const child of node.children) {
                if (isFragmentElement(child, text)) {
                    report(child, parent);
                } else if (
                    (child.type === 'JSXExpressionContainer' || child.type === 'JSXSpreadChild') &&
                    child.expression.type !== 'JSXEmptyExpression'
                ) {
                    for (const fragment of findLooseFragments(child.expression, text)) {
                        report(fragment, parent);
                    }
                }
            }
        }
        for (const child of children(node)) {
            walk(child);
        }
    };

    walk(program);
    return violations;
}


function scanRawMarkup(program, text, starts) {
    const lines = text.split('\n');
    const violations = [];

    const report = (node, what) => {
        const line = lineOf(starts, startOf(node, text));
        if (line > 0 && lines[line - 1].includes(RAW_MARKUP_MARKER)) {
            return;
        }
        violations.push({ line: line + 1, what });
    };

    // Inside TextContent the native tags are the documented way to write prose.
    const walk = (node, inProse) => {
        let prose = inProse;
        if (isElement(node) || isSelfClosing(node)) {
            const name = tagName(node, text);
            if (name === PROSE_WRAPPER) {
                prose = true;
            } else if (!prose && RAW_MARKUP_TAGS.has(name)) {
                report(node, `<${name}>`);
            }
        }
        if (node.type === 'JSXText' && RAW_MARKUP_ENTITIES.test(node.raw)) {
            report(node, 'spacer entity');
        }
        for (const child of children(node)) {
            walk(child, prose);
        }
    };

    walk(program, false);
    return violations;
}

const args = process.argv.slice(2);
const files =
    args.length > 0
        ? args.map((path) => resolve(process.cwd(), path)).filter((path) => EXTENSIONS.some((extension) => path.endsWith(extension)))
        : collect(join(WEBAPP_ROOT, 'src'));

const hashedClassNames = [];
const fragmentChildren = [];
const rawMarkup = [];
const unparsed = [];

for (const file of files) {
    const text = readFileSync(file, 'utf8');
    const name = relative(WEBAPP_ROOT, file);

    const lines = text.split('\n');
    lines.forEach((line, index) => {
        const match = HASHED_CLASS_NAME.exec(line);
        if (match == null || (index > 0 && lines[index - 1].includes(ALLOW_MARKER))) {
            return;
        }
        hashedClassNames.push(`${name}:${index + 1}: ${match[0]}`);
    });

    if (file.endsWith('.tsx') || file.endsWith('.jsx')) {
        const { program, errors } = parseSync(file, text, { lang: 'tsx', sourceType: 'module' });
        const starts = lineStarts(text);
        const failures = parseErrors(errors, starts);
        if (failures.length > 0) {
            for (const failure of failures) {
                unparsed.push(`${name}:${failure.line}: ${failure.message}`);
            }
            continue;
        }
        for (const violation of scanFragments(program, text, starts)) {
            fragmentChildren.push(`${name}:${violation.line}: fragment child of <${violation.parent}>`);
        }
        if (!/\.test\.[jt]sx$/.test(file)) {
            for (const violation of scanRawMarkup(program, text, starts)) {
                rawMarkup.push(`${name}:${violation.line}: ${violation.what}`);
            }
        }
    }
}

let failed = false;

if (hashedClassNames.length > 0) {
    failed = true;
    console.error('Hardcoded Cloudscape class names (they change on every release):\n');
    for (const violation of hashedClassNames) {
        console.error(`  ${violation}`);
    }
    console.error(`\nUse an app-owned class, id or data attribute. To allow one, put "${ALLOW_MARKER}: <reason>" on the line above.`);
}

if (fragmentChildren.length > 0) {
    failed = true;
    console.error(`${hashedClassNames.length > 0 ? '\n' : ''}React.Fragment children of a Cloudscape layout component (React 19 stops flattening them):\n`);
    for (const violation of fragmentChildren) {
        console.error(`  ${violation}`);
    }
    console.error(`\nReturn a keyed array instead of a fragment. To allow one, put "${FRAGMENT_MARKER}: <reason>" on the line above.`);
}

if (rawMarkup.length > 0) {
    failed = true;
    console.error(`${failed ? '\n' : ''}Presentational HTML outside TextContent (browser defaults, off-theme):\n`);
    for (const violation of rawMarkup) {
        console.error(`  ${violation}`);
    }
    console.error(`\nWrap prose in TextContent, use Box variants for emphasis and SpaceBetween for spacing. To allow one, put "${RAW_MARKUP_MARKER}: <reason>" on the line above.`);
}

if (unparsed.length > 0) {
    console.error(`${failed ? '\n' : ''}Files the parser could not read (nothing in them was checked):\n`);
    failed = true;
    for (const violation of unparsed) {
        console.error(`  ${violation}`);
    }
}

if (failed) {
    process.exit(1);
}
