/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

// Structural fingerprint of a rendered subtree, for snapshot diffing across
// Cloudscape and React upgrades. Values that change on every build or every run
// are normalized away; tags, attributes and nesting are kept verbatim.

// awsui_<name>_<file-hash>_<build-hash>_<line>. Only <name> survives a release.
// <name> can itself contain underscores, because Cloudscape uses BEM names.
const HASHED_CLASS_NAME = /\bawsui_([a-zA-Z0-9_-]+)_[a-z0-9]+_[a-z0-9]+_\d+\b/g;

// React useId output: "_r_1f_" on 19, ":r1f:" on 18. Appears inside id, for,
// aria-labelledby, aria-controls and Cloudscape's data-analytics-* attributes.
const GENERATED_ID = /_r_[0-9a-z]+_|:r[0-9a-z]+:/g;

const VOLATILE_ATTRIBUTES = new Set([
    // wall-clock timestamps, different on every run
    'data-analytics-performance-mark',
    'data-analytics-task-interaction-id',
    // presentational, and carries build-hashed --awsui-* custom property names
    'style'
]);

// SVG path geometry: kept as a digest so an icon swap still shows up in the
// diff without embedding kilobytes of path data in the snapshot.
const DIGESTED_ATTRIBUTES = new Set(['d']);

function digest(value: string): string {
    let h = 0x811c9dc5;
    for (let i = 0; i < value.length; i++) {
        h ^= value.charCodeAt(i);
        h = Math.imul(h, 0x01000193);
    }
    return (h >>> 0).toString(16).padStart(8, '0');
}

/** Render a DOM subtree as an indented tag/attribute skeleton. Text content is excluded: the suite
 * already asserts on text, and this is here to see the structure text assertions cannot. */
export function fingerprint(root: Element): string {
    // React's useId counter depends on how much rendered before this subtree, so
    // map each distinct token to its first-seen ordinal instead of its value.
    const generatedIds = new Map<string, string>();
    const stableId = (token: string): string => {
        if (!generatedIds.has(token)) {
            generatedIds.set(token, `_r${generatedIds.size}_`);
        }
        return generatedIds.get(token)!;
    };

    const normalize = (value: string): string => value.replace(HASHED_CLASS_NAME, 'awsui_$1').replace(GENERATED_ID, stableId);

    const normalizeClass = (value: string): string =>
        normalize(value)
            .split(/\s+/)
            .filter(Boolean)
            // token order is composition order and carries no meaning
            .sort()
            .join(' ');

    const lines: string[] = [];

    const walk = (element: Element, depth: number) => {
        const attributes = Array.from(element.attributes)
            .filter((attribute) => !VOLATILE_ATTRIBUTES.has(attribute.name))
            // sorted before normalizing, so id ordinals do not depend on the order
            // React happened to set the attributes in
            .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
            .map((attribute) => {
                if (DIGESTED_ATTRIBUTES.has(attribute.name)) {
                    return `${attribute.name}="#${digest(attribute.value)}"`;
                }
                if (attribute.name === 'class') {
                    return `class="${normalizeClass(attribute.value)}"`;
                }
                return `${attribute.name}="${normalize(attribute.value)}"`;
            });
        const indent = '  '.repeat(depth);
        lines.push(`${indent}${element.tagName.toLowerCase()}${attributes.length > 0 ? ` ${attributes.join(' ')}` : ''}`);
        for (const child of Array.from(element.children)) {
            walk(child, depth + 1);
        }
    };

    walk(root, 0);
    return lines.join('\n');
}
