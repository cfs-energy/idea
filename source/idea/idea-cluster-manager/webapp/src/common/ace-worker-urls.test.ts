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
