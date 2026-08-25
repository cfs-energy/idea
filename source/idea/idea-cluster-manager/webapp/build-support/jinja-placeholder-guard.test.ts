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

import indexHtml from '../index.html?raw'
import { JINJA_APP_INIT_PLACEHOLDER, checkJinjaPlaceholder } from './jinja-placeholder-guard'

describe('jinja placeholder guard', () => {
    // the source template is what Vite copies into build/; if it ever loses the
    // placeholder the built file cannot have it either
    it('accepts the index.html this repo ships', () => {
        expect(checkJinjaPlaceholder(indexHtml)).toBeNull()
    })

    it('rejects a build that dropped the placeholder', () => {
        const minified = indexHtml.replace(JINJA_APP_INIT_PLACEHOLDER, '')
        expect(checkJinjaPlaceholder(minified)).toContain('found 0')
    })

    it('rejects a build that duplicated the placeholder', () => {
        const doubled = indexHtml.replace(
            JINJA_APP_INIT_PLACEHOLDER,
            `${JINJA_APP_INIT_PLACEHOLDER}${JINJA_APP_INIT_PLACEHOLDER}`
        )
        expect(checkJinjaPlaceholder(doubled)).toContain('found 2')
    })

    // anything else Jinja would evaluate is a render-time failure or an injection point
    it('rejects a stray Jinja expression', () => {
        expect(checkJinjaPlaceholder(`<p>{{ other }}</p>${JINJA_APP_INIT_PLACEHOLDER}`)).toContain(
            'expected exactly 1 Jinja expression'
        )
    })

    it('rejects Jinja statement and comment tags', () => {
        expect(checkJinjaPlaceholder(`{% if x %}${JINJA_APP_INIT_PLACEHOLDER}`)).toContain('"{%"')
        expect(checkJinjaPlaceholder(`{# note #}${JINJA_APP_INIT_PLACEHOLDER}`)).toContain('"{#"')
    })
})
