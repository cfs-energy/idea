import Utils from './utils'

describe('wayland desktops', () => {
    it.each(['rhel10', 'rocky10', 'ubuntu2604'])('%s offers virtual sessions only', (baseOs) => {
        expect(Utils.isWaylandDesktop(baseOs)).toBe(true)
        expect(Utils.getOsTitle(baseOs)).not.toBe('Unknown')
    })

    it.each(['amazonlinux2023', 'rhel9', 'rocky9', 'ubuntu2404', 'windows2022', undefined])('%s keeps console sessions', (baseOs) => {
        expect(Utils.isWaylandDesktop(baseOs)).toBe(false)
    })
})
