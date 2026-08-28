import { ACE_WORKER_MODULE_URLS, registerAceWorkerUrls } from './ace-worker-urls'

describe('ace worker urls', () => {
    it('replaces the bare relative path ace would otherwise hand to new Worker()', async () => {
        // exercise the real ace config, the same way the editors load it
        const ace = await import('ace-builds')

        // ace's fallback: moduleUrl() strips the "_worker" suffix and returns a bare
        // filename, which new Worker() resolves against the page URL and 404s. lua is
        // deliberately unregistered, so it keeps showing the broken shape for contrast.
        expect(ace.config.moduleUrl('ace/mode/json_worker', 'worker')).toBe('worker-json.js')
        expect(ace.config.moduleUrl('ace/mode/lua_worker', 'worker')).toBe('worker-lua.js')

        registerAceWorkerUrls(ace)

        for (const [moduleName, url] of Object.entries(ACE_WORKER_MODULE_URLS)) {
            expect(ace.config.moduleUrl(moduleName, 'worker')).toBe(url)
        }
        // the mode the form builder's Advanced Mode uses
        expect(ace.config.moduleUrl('ace/mode/json_worker', 'worker')).not.toBe('worker-json.js')
        expect(ace.config.moduleUrl('ace/mode/lua_worker', 'worker')).toBe('worker-lua.js')
    })

    it('resolves each worker to a distinct ace worker script', () => {
        const urls = Object.values(ACE_WORKER_MODULE_URLS)
        expect(urls.length).toBeGreaterThan(0)
        for (const url of urls) {
            expect(url).toMatch(/worker-[a-z]+.*\.js$/)
        }
        expect(new Set(urls).size).toBe(urls.length)
    })
})
