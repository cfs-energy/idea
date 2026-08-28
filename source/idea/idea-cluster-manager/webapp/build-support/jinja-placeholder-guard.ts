import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Plugin } from 'vite'

// cluster-manager's web_portal.py renders build/index.html as a Jinja2 template and
// substitutes this one variable. The whole portal bootstrap depends on Vite leaving the
// inline classic script byte-intact, so the build asserts it rather than trusting it.
export const JINJA_APP_INIT_PLACEHOLDER = '{{ app_init_data }}'

const JINJA_TAG_OPENERS = ['{%', '{#']

function countOccurrences(haystack: string, needle: string): number {
    return haystack.split(needle).length - 1
}

/** Returns a description of the problem, or null when index.html is still a safe Jinja2 template:
 * exactly one expression, and that expression is the app_init_data placeholder. */
export function checkJinjaPlaceholder(html: string): string | null {
    const placeholders = countOccurrences(html, JINJA_APP_INIT_PLACEHOLDER)
    if (placeholders !== 1) {
        return `expected exactly 1 "${JINJA_APP_INIT_PLACEHOLDER}", found ${placeholders}`
    }
    const expressions = countOccurrences(html, '{{')
    if (expressions !== 1) {
        return `expected exactly 1 Jinja expression ("{{"), found ${expressions}`
    }
    for (const opener of JINJA_TAG_OPENERS) {
        const count = countOccurrences(html, opener)
        if (count !== 0) {
            return `expected no Jinja "${opener}" sequences, found ${count}`
        }
    }
    return null
}

/** Fails `yarn build` if the emitted index.html stops being renderable by web_portal.py, e.g. a
 * future Vite version starts minifying or externalizing the inline script. */
export function jinjaPlaceholderGuard(): Plugin {
    let indexHtml = ''
    return {
        name: 'idea:jinja-placeholder-guard',
        apply: 'build',
        configResolved(config) {
            indexHtml = resolve(config.root, config.build.outDir, 'index.html')
        },
        closeBundle() {
            const problem = checkJinjaPlaceholder(readFileSync(indexHtml, 'utf-8'))
            if (problem !== null) {
                throw new Error(
                    `${indexHtml} is no longer a valid Jinja2 template for cluster-manager web_portal.py: ${problem}`
                )
            }
        }
    }
}
