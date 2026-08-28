import ClusterSettingsService, {parseDashboardUrl} from './cluster-settings-service'
import {ClusterSettingsClient} from '../client'
import {initTestAppData} from '../test-support'

const GLOBAL_SETTINGS = {
    module_sets: {
        default: {
            'cluster': {module_id: 'cluster'},
            'shared-storage': {module_id: 'shared-storage'},
            'cluster-manager': {module_id: 'cluster-manager'}
        }
    }
}

const MODULE_SETTINGS: any = {
    'cluster': {locale: 'en_US', timezone: 'UTC', cluster_name: 'idea-test'},
    'shared-storage': {apps: {mount_dir: '/apps'}}
}

// clusterManagerSettings: the settings the API returns for cluster-manager, or an
// Error to make that one call reject.
function buildService(clusterManagerSettings: any): ClusterSettingsService {
    const client = {
        listClusterModules: () => Promise.resolve({
            listing: [{name: 'cluster-manager', module_id: 'cluster-manager', status: 'deployed'}]
        }),
        getModuleSettings: (request: any) => {
            if (request.module_id === 'global-settings') {
                return Promise.resolve({settings: GLOBAL_SETTINGS})
            }
            if (request.module_id === 'cluster-manager') {
                if (clusterManagerSettings instanceof Error) {
                    return Promise.reject(clusterManagerSettings)
                }
                return Promise.resolve({settings: clusterManagerSettings})
            }
            return Promise.resolve({settings: MODULE_SETTINGS[request.module_id]})
        }
    }
    return new ClusterSettingsService({clusterSettings: client as unknown as ClusterSettingsClient})
}

function customDashboard(settings: any): any {
    return {web_portal: {custom_dashboard: settings}}
}

describe('parseDashboardUrl', () => {
    it('accepts http and https', () => {
        expect(parseDashboardUrl('https://dashboard.example.com/view')?.href).toBe('https://dashboard.example.com/view')
        expect(parseDashboardUrl('http://dashboard.example.com/view')?.href).toBe('http://dashboard.example.com/view')
    })
    it('resolves a relative url against the portal', () => {
        const parsed = parseDashboardUrl('/grafana/d/cluster')
        expect(parsed).not.toBeNull()
        expect(parsed!.origin).toBe(window.location.origin)
    })
    it('rejects script-bearing schemes', () => {
        expect(parseDashboardUrl('javascript:alert(document.cookie)')).toBeNull()
        expect(parseDashboardUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
        expect(parseDashboardUrl('blob:https://dashboard.example.com/1234')).toBeNull()
    })
    it('rejects empty and unparsable values', () => {
        expect(parseDashboardUrl('')).toBeNull()
        expect(parseDashboardUrl('   ')).toBeNull()
        expect(parseDashboardUrl(undefined)).toBeNull()
        expect(parseDashboardUrl('http://[not-a-host')).toBeNull()
    })
})

describe('custom dashboard settings', () => {

    beforeEach(() => {
        initTestAppData()
    })

    it('is enabled when the flag is set and the url is embeddable', async () => {
        const service = buildService(customDashboard({
            enabled: true,
            title: 'Cluster Dashboard',
            url: 'https://dashboard.example.com/view'
        }))
        expect(await service.initialize()).toBe(true)
        expect(service.isCustomDashboardEnabled()).toBe(true)
        expect(service.getCustomDashboardTitle()).toBe('Cluster Dashboard')
        expect(service.getCustomDashboard().url).toBe('https://dashboard.example.com/view')
    })

    it('stays disabled when the url is empty', async () => {
        const service = buildService(customDashboard({enabled: true, title: 'Cluster Dashboard', url: ''}))
        expect(await service.initialize()).toBe(true)
        expect(service.isCustomDashboardEnabled()).toBe(false)
    })

    it('stays disabled when the url is not http(s)', async () => {
        const service = buildService(customDashboard({
            enabled: true,
            title: 'Cluster Dashboard',
            url: 'javascript:alert(document.cookie)'
        }))
        expect(await service.initialize()).toBe(true)
        expect(service.isCustomDashboardEnabled()).toBe(false)
    })

    it('stays disabled when the flag is off', async () => {
        const service = buildService(customDashboard({
            enabled: false,
            title: 'Cluster Dashboard',
            url: 'https://dashboard.example.com/view'
        }))
        expect(await service.initialize()).toBe(true)
        expect(service.isCustomDashboardEnabled()).toBe(false)
    })

    it('falls back to the default title when the settings block is absent', async () => {
        const service = buildService({})
        expect(await service.initialize()).toBe(true)
        expect(service.isCustomDashboardEnabled()).toBe(false)
        expect(service.getCustomDashboardTitle()).toBe('Dashboard')
    })

    it('leaves the embed disabled and app initialization successful when the settings call fails', async () => {
        const service = buildService(new Error('access denied'))
        expect(await service.initialize()).toBe(true)
        expect(service.isCustomDashboardEnabled()).toBe(false)
        expect(service.getCustomDashboardTitle()).toBe('Dashboard')
    })
})
