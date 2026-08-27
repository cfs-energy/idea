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
