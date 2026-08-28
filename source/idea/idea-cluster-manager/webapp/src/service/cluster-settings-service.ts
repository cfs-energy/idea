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


import {ClusterSettingsClient} from "../client";
import {Constants, ErrorCodes} from "../common/constants";
import IdeaException from "../common/exceptions";
import Utils from "../common/utils";

export interface ClusterSettingsServiceProps {
    clusterSettings: ClusterSettingsClient
}

export interface CustomDashboardSettings {
    enabled: boolean
    title: string
    url: string
}

const DEFAULT_CUSTOM_DASHBOARD_TITLE = 'Dashboard'

const CUSTOM_DASHBOARD_DISABLED: CustomDashboardSettings = {
    enabled: false,
    title: DEFAULT_CUSTOM_DASHBOARD_TITLE,
    url: ''
}

// only http(s) can be framed: a javascript:, data: or blob: URL would run in a document
// that inherits the portal origin and can read its tokens.
export function parseDashboardUrl(url?: string): URL | null {
    if (Utils.isEmpty(url)) {
        return null
    }
    let parsed: URL
    try {
        parsed = new URL(Utils.asString(url), window.location.href)
    } catch (_) {
        return null
    }
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
        return null
    }
    return parsed
}

class ClusterSettingsService {

    private props: ClusterSettingsServiceProps
    clusterModules: any
    globalSettings: any
    moduleSettings: any
    instanceTypes: any[]
    clusterName: string
    clusterLocale: string
    clusterTimezone: string
    clusterHomeDir: string
    customDashboard: CustomDashboardSettings

    constructor(props: ClusterSettingsServiceProps) {
        this.props = props
        this.clusterModules = []
        this.moduleSettings = {}
        this.instanceTypes = []
        this.clusterName = ''
        this.clusterLocale = 'en-US'
        this.clusterTimezone = 'UTC'
        this.clusterHomeDir = ''
        this.customDashboard = CUSTOM_DASHBOARD_DISABLED
    }

    initialize(): Promise<boolean> {
        return this.props.clusterSettings.listClusterModules({}).then(result => {
            this.clusterModules = result.listing
            return this.props.clusterSettings.getModuleSettings({
                module_id: Constants.MODULE_GLOBAL_SETTINGS
            })
        }).then(result => {
            this.globalSettings = result.settings
            return this.getModuleSettings(Constants.MODULE_CLUSTER)
        }).then(clusterSettings => {
            this.clusterLocale = clusterSettings.locale.replace('_', '-')
            this.clusterTimezone = clusterSettings.timezone
            this.clusterName = clusterSettings.cluster_name
            return this.getModuleSettings(Constants.MODULE_SHARED_STORAGE)
        }).then(sharedStorageSettings => {
            this.clusterHomeDir = `${sharedStorageSettings.apps.mount_dir}/${this.clusterName}`
            return this.initializeCustomDashboard()
        }).then(_ => {
            return true
        }).catch(error => {
            console.error(error)
            return false
        })
    }

    private initializeCustomDashboard(): Promise<boolean> {
        return this.getModuleSettings(Constants.MODULE_CLUSTER_MANAGER).then(settings => {
            const customDashboard = settings?.web_portal?.custom_dashboard
            this.customDashboard = {
                enabled: Utils.asBoolean(customDashboard?.enabled),
                title: Utils.asString(customDashboard?.title, DEFAULT_CUSTOM_DASHBOARD_TITLE),
                url: Utils.asString(customDashboard?.url)
            }
            return true
        }).catch(_ => {
            // the embed is optional: leave it disabled rather than failing app initialization
            this.customDashboard = CUSTOM_DASHBOARD_DISABLED
            return false
        })
    }

    getCustomDashboard(): CustomDashboardSettings {
        return this.customDashboard
    }

    getCustomDashboardTitle(): string {
        const title = this.customDashboard.title
        return Utils.isNotEmpty(title) ? title : DEFAULT_CUSTOM_DASHBOARD_TITLE
    }

    isCustomDashboardEnabled(): boolean {
        return this.customDashboard.enabled && parseDashboardUrl(this.customDashboard.url) != null
    }

    fetchInstanceTypes(): Promise<boolean> {
        return this.props.clusterSettings.describeInstanceTypes({}).then(result => {
            this.instanceTypes = result.instance_types
            return true
        })
    }

    getVirtualDesktopSettings(): Promise<any> {
        if (!this.isVirtualDesktopDeployed()) {
            return Promise.resolve({})
        }
        return this.getModuleSettings(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)
    }

    getSchedulerSettings(): Promise<any> {
        if (!this.isSchedulerDeployed()) {
            return Promise.resolve({})
        }
        return this.getModuleSettings(Constants.MODULE_SCHEDULER)
    }

    getModuleSetId(): string {
        return window.idea.app.module_set
    }

    getModuleSet(): any {
        return this.globalSettings.module_sets[this.getModuleSetId()]
    }

    getModuleId(name: string): string | null {
        const moduleSet = this.getModuleSet()
        if (name in moduleSet) {
            return moduleSet[name].module_id
        }
        return null
    }

    getModuleSettings(name: string): Promise<any> {
        if (name in this.moduleSettings) {
            return Promise.resolve(this.moduleSettings[name])
        }
        if (name === Constants.MODULE_GLOBAL_SETTINGS) {
            return Promise.resolve(this.globalSettings)
        }

        const moduleId = this.getModuleId(name)
        if (moduleId != null) {
            const moduleSet = this.getModuleSet()
            let moduleId = moduleSet[name].module_id
            return this.props.clusterSettings.getModuleSettings({
                module_id: moduleId
            }).then(result => {
                this.moduleSettings[name] = result.settings
                return this.moduleSettings[name]
            })
        }

        return Promise.reject(new IdeaException({
            errorCode: 'MODULE_NOT_FOUND',
            message: `ModuleId not found for module name: ${name}`
        }))
    }

    getClusterTimeZone(): string {
        return this.clusterTimezone
    }

    getClusterLocale(): string {
        return this.clusterLocale
    }

    getModuleSetIds(moduleName: string): string[] {
        let result: string[] = []
        const moduleSets = this.globalSettings.module_sets
        for(let moduleSetId in moduleSets) {
            const moduleSet = moduleSets[moduleSetId]
            for(let currentModuleName in moduleSet) {
                if(currentModuleName === moduleName) {
                    result.push(moduleSetId)
                }
            }
        }
        return result
    }

    isModuleEnabled(name: string): boolean {
        if (this.clusterModules) {
            for (let i = 0; i < this.clusterModules.length; i++) {
                let module = this.clusterModules[i]
                if (module.name === name) {
                    return true
                }
            }
        }
        return false
    }

    isModuleDeployed(name: string): boolean {
        if (this.clusterModules) {
            for (let i = 0; i < this.clusterModules.length; i++) {
                let module = this.clusterModules[i]
                if (module.name === name) {
                    return module.status === 'deployed'
                }
            }
        }
        return false
    }

    getModuleInfo(name: string): any {
        let moduleId = this.getModuleId(name)
        if (moduleId === null) {
            throw new IdeaException({
                errorCode: ErrorCodes.MODULE_NOT_FOUND,
                message: `Module not found: ${name}`
            })
        }
        for (let i = 0; i < this.clusterModules.length; i++) {
            let module = this.clusterModules[i]
            if (module.module_id === moduleId) {
                return module
            }
        }
    }

    isVirtualDesktopEnabled(): boolean {
        return this.isModuleEnabled(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)
    }

    isVirtualDesktopDeployed(): boolean {
        return this.isModuleDeployed(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)
    }

    isSchedulerEnabled(): boolean {
        return this.isModuleEnabled(Constants.MODULE_SCHEDULER)
    }

    isSchedulerDeployed(): boolean {
        return this.isModuleDeployed(Constants.MODULE_SCHEDULER)
    }

    isBastionHostEnabled(): boolean {
        return this.isModuleEnabled(Constants.MODULE_BASTION_HOST)
    }

    isBastionHostDeployed(): boolean {
        return this.isModuleDeployed(Constants.MODULE_BASTION_HOST)
    }

    isAnalyticsEnabled(): boolean {
        return this.isModuleEnabled(Constants.MODULE_ANALYTICS)
    }

    isAnalyticsDeployed(): boolean {
        return this.isModuleDeployed(Constants.MODULE_ANALYTICS)
    }

    isMetricsEnabled(): boolean {
        return this.isModuleEnabled(Constants.MODULE_METRICS)
    }

    getInstanceTypes(): Promise<any[]> {
        if (this.instanceTypes.length > 0) {
            return Promise.resolve(this.instanceTypes)
        }
        return this.fetchInstanceTypes().then(_ => {
            return this.instanceTypes
        })
    }

    getClusterSettings(): Promise<any> {
        return this.getModuleSettings(Constants.MODULE_CLUSTER)
    }

    getDirectoryServiceSettings(): Promise<any> {
        return this.getModuleSettings(Constants.MODULE_DIRECTORY_SERVICE)
    }

    getIdentityProviderSettings(): Promise<any> {
        return this.getModuleSettings(Constants.MODULE_IDENTITY_PROVIDER)
    }

    getSharedStorageSettings(): Promise<any> {
        return this.getModuleSettings(Constants.MODULE_SHARED_STORAGE)
    }

    getAnalyticsSettings(): Promise<any> {
        return this.getModuleSettings(Constants.MODULE_ANALYTICS)
    }

    getMetricsSettings(): Promise<any> {
        return this.getModuleSettings(Constants.MODULE_METRICS)
    }

    getClusterHomeDir(): string {
        return this.clusterHomeDir
    }
}

export default ClusterSettingsService
