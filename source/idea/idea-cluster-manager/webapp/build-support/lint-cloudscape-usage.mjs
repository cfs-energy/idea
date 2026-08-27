// Two Cloudscape coupling rules that nothing else in the toolchain catches. Runs over the file paths
// given as arguments (pre-commit), or over src/ when called with none (yarn lint).

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

// CommonJS resolution, so typescript is found in webapp/node_modules or, when pre-commit runs this
// before any yarn install, in the hook's own node env via NODE_PATH. An ESM bare import ignores that.
const ts = createRequire(import.meta.url)('typescript');

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
// flattenChildren's consumers in the installed package: column-layout, grid, space-between.
const FLATTEN_CONSUMERS = new Set(['ColumnLayout', 'Grid', 'SpaceBetween']);

const FRAGMENT_MARKER = 'cloudscape-fragment-child-allowed';

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

function tagName(node) {
    const tag = node.tagName ?? node.openingElement?.tagName;
    return tag == null ? '' : tag.getText();
}

function isFragmentElement(node) {
    if (ts.isJsxFragment(node)) {
        return true;
    }
    if (!ts.isJsxElement(node)) {
        return false;
    }
    const name = tagName(node);
    return name === 'React.Fragment' || name === 'Fragment';
}

// A fragment reaches the parent component as a child unless some other JSX
// element encloses it first, so stop descending at the first JSX element.
function findLooseFragments(node, found = []) {
    if (isFragmentElement(node)) {
        found.push(node);
        return found;
    }
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
        return found;
    }
    // forEachChild stops on the first truthy return, so the callback returns nothing.
    node.forEachChild((child) => {
        findLooseFragments(child, found);
    });
    return found;
}

function scanFragments(file, text) {
    const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const lines = text.split('\n');
    const violations = [];

    const report = (node, parent) => {
        const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line;
        if (line > 0 && lines[line - 1].includes(FRAGMENT_MARKER)) {
            return;
        }
        violations.push({ line: line + 1, parent });
    };

    const walk = (node) => {
        if (ts.isJsxElement(node) && FLATTEN_CONSUMERS.has(tagName(node))) {
            const parent = tagName(node);
            for (const child of node.children) {
                if (isFragmentElement(child)) {
                    report(child, parent);
                } else if (ts.isJsxExpression(child) && child.expression != null) {
                    for (const fragment of findLooseFragments(child.expression)) {
                        report(fragment, parent);
                    }
                }
            }
        }
        node.forEachChild(walk);
    };

    walk(source);
    return violations;
}

const args = process.argv.slice(2);
const files =
    args.length > 0
        ? args.map((path) => resolve(process.cwd(), path)).filter((path) => EXTENSIONS.some((extension) => path.endsWith(extension)))
        : collect(join(WEBAPP_ROOT, 'src'));

const hashedClassNames = [];
const fragmentChildren = [];

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
        for (const violation of scanFragments(file, text)) {
            fragmentChildren.push(`${name}:${violation.line}: fragment child of <${violation.parent}>`);
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

if (failed) {
    process.exit(1);
}
