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
