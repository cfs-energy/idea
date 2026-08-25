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

import { waitedFor } from './virtual-desktop-session-status-indicator'

const minutesAgo = (minutes: number): string => new Date(Date.now() - minutes * 60000).toISOString()

describe('virtual desktop waiting time', () => {
    it('tells the user how long a desktop has been waiting', () => {
        expect(waitedFor({ state: 'PROVISIONING', hibernation_enabled: false, updated_on: minutesAgo(14) })).toBe(' - 14 min so far')
        expect(waitedFor({ state: 'INITIALIZING', hibernation_enabled: false, updated_on: minutesAgo(3) })).toBe(' - 3 min so far')
        expect(waitedFor({ state: 'RESUMING', hibernation_enabled: false, updated_on: minutesAgo(90) })).toBe(' - 90 min so far')
    })

    it('stays quiet for settled states and for the first minute', () => {
        expect(waitedFor({ state: 'READY', hibernation_enabled: false, updated_on: minutesAgo(14) })).toBe('')
        expect(waitedFor({ state: 'ERROR', hibernation_enabled: false, updated_on: minutesAgo(14) })).toBe('')
        expect(waitedFor({ state: 'PROVISIONING', hibernation_enabled: false, updated_on: minutesAgo(0) })).toBe('')
    })

    it('shows nothing rather than a broken number when the timestamp is missing or unusable', () => {
        expect(waitedFor({ state: 'PROVISIONING', hibernation_enabled: false })).toBe('')
        expect(waitedFor({ state: 'PROVISIONING', hibernation_enabled: false, updated_on: 'not a date' })).toBe('')
        expect(waitedFor({ state: 'PROVISIONING', hibernation_enabled: false, updated_on: '' })).toBe('')
    })
})
