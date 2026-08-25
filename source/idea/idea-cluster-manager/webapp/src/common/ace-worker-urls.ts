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

// ace resolves syntax-worker scripts through config.moduleUrl(), which only reads the table
// populated by setModuleUrl(); ace-builds/esm-resolver registers workers with setModuleLoader()
// instead, so without these mappings `new Worker()` gets a bare filename and 404s.

import workerJsonUrl from 'ace-builds/src-noconflict/worker-json.js?url'
import workerYamlUrl from 'ace-builds/src-noconflict/worker-yaml.js?url'
import workerXmlUrl from 'ace-builds/src-noconflict/worker-xml.js?url'

// Worker-backed modes the portal's code editors can select. json is what the form
// builder's "Advanced Mode" uses; yaml/xml cover the language-parameterized file
// editor in file-browser. The other languages selected today (sh, text) have no worker.
export const ACE_WORKER_MODULE_URLS: Readonly<Record<string, string>> = {
    'ace/mode/json_worker': workerJsonUrl,
    'ace/mode/yaml_worker': workerYamlUrl,
    'ace/mode/xml_worker': workerXmlUrl
}

interface AceModuleUrlConfig {
    config: {
        setModuleUrl(name: string, url: string): unknown
    }
}

/** Point ace at the hashed worker assets Vite emitted. Idempotent - ace.config is a module singleton,
 * so the first editor to mount registers for the whole app. */
export function registerAceWorkerUrls(ace: AceModuleUrlConfig): void {
    for (const [moduleName, url] of Object.entries(ACE_WORKER_MODULE_URLS)) {
        ace.config.setModuleUrl(moduleName, url)
    }
}
