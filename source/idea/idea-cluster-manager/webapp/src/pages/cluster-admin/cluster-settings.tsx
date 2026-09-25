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

import {SettingsSection, SettingsSource} from './settings-sections';

import AccountReconcileSettings from "./account-reconcile-settings";
import React, {Component} from "react";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {IdeaAppLayoutProps} from "../../components/app-layout";
import {Alert, Box, Button, ColumnLayout, Container, FormField, Header, Input, KeyValuePairs, SpaceBetween, Table, Textarea, Toggle} from "@cloudscape-design/components";
import moment from "moment";
import {KeyValue, KeyValueGroup} from "../../components/key-value";
import {AppContext} from "../../common";
import dot from "dot-object";
import Utils from "../../common/utils";
import {CopyToClipBoard, EnabledDisabledStatusIndicator} from "../../components/common";
import {Constants} from "../../common/constants";
import {SharedStorageFileSystem} from "../../common/shared-storage-utils";
import {withRouter} from "../../navigation/navigation-utils";
import ConfigUtils from "../../common/config-utils";

export interface ClusterSettingsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
    renderSections: (source: SettingsSource) => React.ReactNode
    activeEditor?: string | null
    onEditingChange?: (editor: string | null) => void

}

export interface ClusterSettingsState {
    cluster: any
    identityProvider: any
    directoryservice: any
    sharedStorage: any
    analytics: any
    metrics: any
    clusterManager: any
    desktopSettings: any

    sharedStorageTableItems: any
    selectedFileSystem: SharedStorageFileSystem[]

    maintenanceEditing: boolean
    maintenanceEnabled: boolean
    maintenanceMessage: string
    maintenanceEndsAt: string
    maintenanceUpdating: boolean
    maintenanceError: string | null
    maintenanceSaved: boolean
    settingsErrors: string[]
}

class ClusterSettings extends Component<ClusterSettingsProps, ClusterSettingsState> {


    constructor(props: ClusterSettingsProps) {
        super(props);
        this.state = {
            cluster: {},
            identityProvider: {},
            directoryservice: {},
            sharedStorage: {},
            analytics: {},
            metrics: {},
            clusterManager: {},
            desktopSettings: {},

            sharedStorageTableItems: [],
            selectedFileSystem: [],

            maintenanceEditing: false,
            maintenanceEnabled: false,
            maintenanceMessage: '',
            maintenanceEndsAt: '',
            maintenanceUpdating: false,
            maintenanceError: null,
            maintenanceSaved: false,
            settingsErrors: []
        }
    }

    componentDidUpdate(previous: ClusterSettingsProps) {
        if (previous.activeEditor === 'appearance:Maintenance notice' && this.props.activeEditor !== previous.activeEditor && this.state.maintenanceEditing) {
            this.setState({maintenanceEditing: false, maintenanceError: null,
                maintenanceEnabled: Utils.asBoolean(dot.pick('maintenance.enabled', this.state.clusterManager)),
                maintenanceMessage: Utils.asString(dot.pick('maintenance.message', this.state.clusterManager)),
                maintenanceEndsAt: Utils.asString(dot.pick('maintenance.ends_at', this.state.clusterManager))});
        }
    }

    componentDidMount() {
        let promises: Promise<any>[] = []
        const clusterSettingsService = AppContext.get().getClusterSettingsService()
        // 0
        promises.push(clusterSettingsService.getClusterSettings())
        // 1
        promises.push(clusterSettingsService.getIdentityProviderSettings())
        // 2
        promises.push(clusterSettingsService.getDirectoryServiceSettings())
        // 3
        promises.push(clusterSettingsService.getSharedStorageSettings())
        // 4
        promises.push(clusterSettingsService.getAnalyticsSettings())
        // 5
        promises.push(clusterSettingsService.getModuleSettings(Constants.MODULE_CLUSTER_MANAGER))
        // 6
        // read for the bedrock notice only: a failed read must not blank the page
        promises.push(clusterSettingsService.getVirtualDesktopSettings().catch(() => ({})))
        // 7
        if (clusterSettingsService.isMetricsEnabled()) {
            promises.push(clusterSettingsService.getMetricsSettings())
        }
        // one read that fails must not blank every tab: what did load is rendered and
        // the rest reads as unset.
        Promise.allSettled(promises).then(results => {
            const result = (index: number): any => {
                const settled: any = results[index]
                if (settled == null || settled.status !== 'fulfilled') {
                    return {}
                }
                return (settled.value != null) ? settled.value : {}
            }
            const settingsErrors: string[] = []
            results.forEach((settled, index) => {
                if (settled.status === 'rejected') {
                    console.error('Failed to read cluster settings:', settled.reason)
                    const sources = ['cluster', 'identity provider', 'directory service', 'shared storage', 'analytics', 'cluster manager', 'desktop controller', 'metrics']
                    settingsErrors.push(`Could not read ${sources[index]} settings.`)
                }
            })
            const sharedStorageTableItems = this.getSharedStorageTableItems(result(3))
            const clusterManager = result(5)
            this.setState({
                cluster: result(0),
                identityProvider: result(1),
                directoryservice: result(2),
                sharedStorage: result(3),
                analytics: result(4),
                clusterManager: clusterManager,
                desktopSettings: result(6),
                metrics: (clusterSettingsService.isMetricsEnabled()) ? result(7) : {},
                sharedStorageTableItems: sharedStorageTableItems,
                selectedFileSystem: sharedStorageTableItems.slice(0, 1),
                maintenanceEnabled: Utils.asBoolean(dot.pick('maintenance.enabled', clusterManager), false),
                maintenanceMessage: Utils.asString(dot.pick('maintenance.message', clusterManager)),
                maintenanceEndsAt: Utils.asString(dot.pick('maintenance.ends_at', clusterManager)),
                settingsErrors: settingsErrors
            })
        })
    }

    getSharedStorageTableItems = (sharedStorage: any): SharedStorageFileSystem[] => {
        let result: SharedStorageFileSystem[] = []
        Object.keys(sharedStorage).forEach((key) => {
            const storage = dot.pick(key, sharedStorage)
            const provider = dot.pick('provider', storage)
            if (Utils.isEmpty(provider)) {
                return true
            }
            result.push(new SharedStorageFileSystem(key, storage))
        })
        return result
    }

    // the module settings write is a full replacement of the addressed path, so
    // the catalog is always sent as the complete model id list.
    // All three keys are written together, so the banner can never appear carrying the previous
    // window's message and end time.
    saveMaintenance = () => {
        if (this.state.maintenanceUpdating) return
        const message = this.state.maintenanceMessage.trim()
        const endsAt = this.state.maintenanceEndsAt.trim()

        if (Utils.isNotEmpty(endsAt) && !moment(endsAt, moment.ISO_8601, true).isValid()) {
            this.setState({
                maintenanceError: 'End time must be an ISO 8601 timestamp, for example 2026-09-15T18:00:00Z. Leave it empty for no end time.'
            })
            return
        }

        const clusterSettingsService = AppContext.get().getClusterSettingsService()
        const moduleId = Utils.asString(clusterSettingsService.getModuleId(Constants.MODULE_CLUSTER_MANAGER), Constants.MODULE_CLUSTER_MANAGER)
        this.setState({
            maintenanceUpdating: true,
            maintenanceError: null,
            maintenanceSaved: false
        })
        AppContext.get().client().clusterSettings().updateModuleSettings({
            module_id: moduleId,
            settings: {
                maintenance: {
                    enabled: this.state.maintenanceEnabled,
                    message: message,
                    ends_at: endsAt
                }
            }
        }).then(result => {
            if (!Utils.asBoolean(result.success, false)) {
                this.setState({
                    maintenanceUpdating: false,
                    maintenanceError: 'Failed to update the maintenance settings.'
                })
                return
            }
            this.props.onEditingChange?.(null);
            this.setState({
                maintenanceUpdating: false,
                maintenanceMessage: message,
                maintenanceEndsAt: endsAt,
                maintenanceEditing: false,
                clusterManager: {...this.state.clusterManager, maintenance: {enabled: this.state.maintenanceEnabled, message, ends_at: endsAt}},
                maintenanceSaved: true
            })
        }).catch(error => {
            this.setState({
                maintenanceUpdating: false,
                maintenanceError: error?.message ?? `${error}`
            })
        })
    }

    buildMaintenanceSettings() {
        return (
            <SpaceBetween size={"l"}>
                <Container header={<Header variant={"h2"} actions={this.state.maintenanceEditing ? <SpaceBetween direction="horizontal" size="xs">
                    <Button disabled={this.state.maintenanceUpdating} onClick={() => {this.props.onEditingChange?.(null); this.setState({maintenanceEditing: false, maintenanceError: null,
                        maintenanceEnabled: Utils.asBoolean(dot.pick('maintenance.enabled', this.state.clusterManager)),
                        maintenanceMessage: Utils.asString(dot.pick('maintenance.message', this.state.clusterManager)),
                        maintenanceEndsAt: Utils.asString(dot.pick('maintenance.ends_at', this.state.clusterManager))});}}>Cancel</Button>
                    <Button variant="primary" loading={this.state.maintenanceUpdating} disabled={this.state.maintenanceUpdating} onClick={this.saveMaintenance}>Save</Button>
                </SpaceBetween> : <Button disabled={Boolean(this.props.activeEditor && this.props.activeEditor !== 'appearance:Maintenance notice')} onClick={() => {this.props.onEditingChange?.('appearance:Maintenance notice'); this.setState({maintenanceEditing: true, maintenanceSaved: false});}}>Edit</Button>}
                                           description={"A warning banner on every portal page, including the sign-in page. While it is on, the scheduler also refuses new job submissions with the same message."}>Maintenance notice</Header>}>
                    <SpaceBetween size={"m"}>
                        {this.state.maintenanceError && <Alert type="error" dismissible={true} onDismiss={() => this.setState({maintenanceError: null})}>{this.state.maintenanceError}</Alert>}
                        {this.state.maintenanceSaved && <Alert type="success" dismissible={true} onDismiss={() => this.setState({maintenanceSaved: false})}>
                            Maintenance notice saved.
                        </Alert>}
                        {this.state.maintenanceEditing ? <SpaceBetween size="m"><div id="setting-cluster-manager.maintenance.enabled"><Toggle checked={this.state.maintenanceEnabled}
                                disabled={this.state.maintenanceUpdating}
                                onChange={(event) => this.setState({maintenanceEnabled: event.detail.checked})}>
                            Maintenance notice
                        </Toggle></div>
                        <div id="setting-cluster-manager.maintenance.message"><FormField label="Message"
                                   description="Plain text, shown to every user.">
                            <Textarea value={this.state.maintenanceMessage}
                                      rows={3}
                                      disabled={this.state.maintenanceUpdating}
                                      placeholder="The HPC scheduler is closed for a cluster upgrade. Running desktops are unaffected."
                                      onChange={(event) => this.setState({maintenanceMessage: event.detail.value})}/>
                        </FormField></div>
                        <div id="setting-cluster-manager.maintenance.ends_at"><FormField label="End time - optional"
                                   description="ISO 8601, for example 2026-09-15T18:00:00Z. A value with no offset is read as UTC, and each user sees it in their own timezone. Leave empty to show no end time. This does not automatically reopen submissions.">
                            <Input value={this.state.maintenanceEndsAt}
                                   disabled={this.state.maintenanceUpdating}
                                   placeholder="2026-09-15T18:00:00Z"
                                   onChange={(event) => this.setState({maintenanceEndsAt: event.detail.value})}/>
                        </FormField></div>
                        </SpaceBetween> : <KeyValuePairs columns={1} items={[
                            {label: 'Maintenance notice', value: <span id="setting-cluster-manager.maintenance.enabled">{this.state.maintenanceEnabled ? 'On' : 'Off'}</span>},
                            {label: 'Message', value: <span id="setting-cluster-manager.maintenance.message">{this.state.maintenanceMessage || 'Not set'}</span>},
                            {label: 'End time - optional', value: <span id="setting-cluster-manager.maintenance.ends_at">{this.state.maintenanceEndsAt || 'Not set'}</span>},
                        ]}/>}
                        <p>This does not automatically reopen submissions.</p>
                        <Alert type="info">
                            Turn this notice off when maintenance is complete. The end time is informational.
                        </Alert>
                    </SpaceBetween>
                </Container>
            </SpaceBetween>
        )
    }


    render() {

        const isExternalAlbCertSelfSigned = (): boolean => {
            return !Utils.asBoolean(dot.pick('load_balancers.external_alb.certificates.provided', this.state.cluster), false)
        }

        const isSingleSignOnEnabled = (): boolean => {
            return Utils.asBoolean(dot.pick('cognito.sso_enabled', this.state.identityProvider), false)
        }

        const isDirectoryServiceOpenLDAP = (): boolean => {
            return dot.pick('provider', this.state.directoryservice) === 'openldap'
        }

        const isDirectoryServiceActiveDirectory = (): boolean => {
            let provider = dot.pick('provider', this.state.directoryservice)
            return provider === 'activedirectory' || provider === 'aws_managed_activedirectory'
        }

        const getOpenSearchDashboardUrl = () => {
            let externalAlbUrl = ConfigUtils.getExternalAlbUrl(this.state.cluster)
            return `${externalAlbUrl}/_dashboards`
        }

        const isMetricsEnabled = () => {
            return AppContext.get().getClusterSettingsService().isMetricsEnabled()
        }

        const isMetricsProviderCloudWatch = () => {
            return dot.pick('provider', this.state.metrics) === 'cloudwatch'
        }

        const isMetricsProviderPrometheus = () => {
            return dot.pick('provider', this.state.metrics) === 'prometheus'
        }

        const isMetricsProviderAmazonManagedPrometheus = () => {
            return dot.pick('provider', this.state.metrics) === 'amazon_managed_prometheus'
        }

        const getSelectedFileSystem = () => {
            if(this.state.selectedFileSystem.length === 0){
                return null
            }
            return this.state.selectedFileSystem[0]
        }

        const getSelectedFileSystemTitle = () => {
            const selected = getSelectedFileSystem()
            if(selected == null) {
                return 'File System: -'
            } else {
                return `File System: ${selected.getTitle()}`
            }
        }

        const isBackupEnabled = () => {
            return Utils.asBoolean(dot.pick('backups.enabled', this.state.cluster))
        }

        const sections: SettingsSection[] = [
            {
                label: 'General',
                id: 'general',
                content: (
                    <SpaceBetween size="m">
                        <Container header={<Header variant={"h2"}>General Settings</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Cluster Name" value={dot.pick('cluster_name', this.state.cluster)}/>
                                <KeyValue title="S3 Bucket" value={dot.pick('cluster_s3_bucket', this.state.cluster)}/>
                                <KeyValue title="Administrator Username" value={dot.pick('administrator_username', this.state.cluster)}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Administrator Email" value={dot.pick('administrator_email', this.state.cluster)} clipboard={true}/>
                                <KeyValue title="Cluster Home Directory" value={dot.pick('home_dir', this.state.cluster)} clipboard={true}/>
                                <KeyValue title="Default Encoding" value={dot.pick('encoding', this.state.cluster)}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                    </SpaceBetween>
                )
            },
            {
                label: 'Network',
                id: 'network',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>VPC</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="VPC Id" value={dot.pick('network.vpc_id', this.state.cluster)} clipboard={true}/>
                                <KeyValue title="Private Subnets" value={dot.pick('network.private_subnets', this.state.cluster)} clipboard={true}/>
                                <KeyValue title="Public Subnets" value={dot.pick('network.public_subnets', this.state.cluster)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Cluster Prefix List Id" value={dot.pick('network.cluster_prefix_list_id', this.state.cluster)} clipboard={true}/>
                                <KeyValue title="Existing VPC?" value={dot.pick('network.use_existing_vpc', this.state.cluster)} type={"boolean"}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                        <Container header={<Header variant={"h2"}>Security Groups</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Bastion Host" value={dot.pick('network.security_groups.bastion-host', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                                <KeyValue title="External Load Balancer" value={dot.pick('network.security_groups.external-load-balancer', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Internal Load Balancer" value={dot.pick('network.security_groups.internal-load-balancer', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                                <KeyValue title="Default Security Group" value={dot.pick('network.security_groups.cluster', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                        <Container header={<Header variant={"h2"}>External Load Balancer</Header>}>
                            <SpaceBetween size={"m"}>
                                <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                    <KeyValue title="Load Balancer DNS Name" value={ConfigUtils.getExternalAlbDnsName(this.state.cluster)} clipboard={true}/>
                                    <KeyValue title="Custom DNS Name" value={ConfigUtils.getExternalAlbCustomDnsName(this.state.cluster)} clipboard={true}/>
                                    </SpaceBetween><SpaceBetween size="m"><KeyValue title="Load Balancer ARN" value={ConfigUtils.getExternalAlbArn(this.state.cluster)} clipboard={true}/>
                                    <KeyValue title="Deploy in Public Subnets?" value={dot.pick('load_balancers.external_alb.public', this.state.cluster)} type={"boolean"}/>
                                </SpaceBetween></ColumnLayout>
                                <Box>
                                    <h3>SSL/TLS Settings</h3>
                                    <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                        <KeyValue title="Certificates" value={isExternalAlbCertSelfSigned() ? 'Self-Signed' : 'ACM'}/>
                                        {isExternalAlbCertSelfSigned() && <KeyValue title="Certificate Secret ARN" value={ConfigUtils.getExternalAlbCertificateSecretArn(this.state.cluster)} clipboard={true}/>}
                                        </SpaceBetween><SpaceBetween size="m">{isExternalAlbCertSelfSigned() && <KeyValue title="Certificate Private Key Secret ARN" value={ConfigUtils.getExternalAlbPrivateKeySecretArn(this.state.cluster)} clipboard={true}/>}
                                        <KeyValue title="ACM Certificate ARN" value={ConfigUtils.getExternalAlbAcmCertificateArn(this.state.cluster)} clipboard={true}/>
                                    </SpaceBetween></ColumnLayout>
                                </Box>
                            </SpaceBetween>
                        </Container>
                        <Container header={<Header variant={"h2"}>Internal Load Balancer</Header>}>
                            <SpaceBetween size={"m"}>
                                <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                    <KeyValue title="Load Balancer DNS Name" value={ConfigUtils.getInternalAlbDnsName(this.state.cluster)} clipboard={true}/>
                                    <KeyValue title="Custom DNS Name" value={ConfigUtils.getInternalAlbCustomDnsName(this.state.cluster)} clipboard={true}/>
                                    </SpaceBetween><SpaceBetween size="m"><KeyValue title="Load Balancer ARN" value={ConfigUtils.getInternalAlbArn(this.state.cluster)} clipboard={true}/>
                                </SpaceBetween></ColumnLayout>
                                <Box>
                                    <h3>SSL/TLS Settings</h3>
                                    <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                        <KeyValue title="Certificates" value="Self-Signed"/>
                                        <KeyValue title="Certificate Secret ARN" value={ConfigUtils.getInternalAlbCertificateSecretArn(this.state.cluster)} clipboard={true}/>
                                        </SpaceBetween><SpaceBetween size="m"><KeyValue title="Certificate Private Key Secret ARN" value={ConfigUtils.getInternalAlbPrivateKeySecretArn(this.state.cluster)} clipboard={true}/>
                                        <KeyValue title="ACM Certificate ARN" value={ConfigUtils.getInternalAlbAcmCertificateArn(this.state.cluster)} clipboard={true}/>
                                    </SpaceBetween></ColumnLayout>
                                </Box>
                            </SpaceBetween>
                        </Container>
                    </SpaceBetween>
                )
            },
            {
                label: 'Shared Storage',
                id: 'shared-storage',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>General</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={3}>
                                <KeyValue title="Security Group (Applicable only for new File Systems)" value={dot.pick('security_group_id', this.state.sharedStorage)} clipboard={true} type={"ec2:security-group-id"}/>
                            </ColumnLayout>
                        </Container>
                        <Table header={<Header variant={"h2"}>File Systems</Header>}
                               items={this.state.sharedStorageTableItems}
                               empty={<Box textAlign="center">No file systems configured.</Box>}
                               selectionType={"single"}
                               selectedItems={this.state.selectedFileSystem}
                               onSelectionChange={(event) => {
                                   this.setState({
                                       selectedFileSystem: event.detail.selectedItems
                                   })
                               }}
                               columnDefinitions={[
                                   {
                                       header: 'Title',
                                       id: 'title',
                                       cell: e => {
                                           return e.getTitle()
                                       }
                                   },
                                   {
                                       header: 'Name',
                                       id: 'name',
                                       cell: e => {
                                           return e.getName()
                                       }
                                   },
                                   {
                                       header: 'Mount Target',
                                       id: 'mount_dir',
                                       cell: e => e.getMountTarget()
                                   },
                                   {
                                       id: 'scope',
                                       header: 'Scope',
                                       cell: e => {
                                           return (
                                               <div>
                                                   {
                                                       e.getScope().map((name, index) => {
                                                           return <li key={index}>{name}</li>
                                                       })
                                                   }
                                               </div>
                                           )
                                       }
                                   },
                                   {
                                       header: 'Provider',
                                       id: 'provider',
                                       cell: e => e.getProviderTitle()
                                   },
                                   {
                                       header: 'File System ID',
                                       id: 'file_system_id',
                                       cell: e => <span><CopyToClipBoard text={e.getFileSystemId()} feedback={`${e.getName()} - File System Id copied`}/> {e.getFileSystemId()}</span>
                                   },
                                   {
                                       header: 'Existing?',
                                       id: 'existing_fs',
                                       cell: e => (e.isExistingFileSystem()) ? 'Yes' : 'No'
                                   }
                               ]}
                        />
                        <Container header={<Header variant={"h2"}>{getSelectedFileSystemTitle()}</Header>}>
                            {this.state.selectedFileSystem.length === 0 && <ColumnLayout columns={1}>
                                <p>Select a file system above to view additional details.</p>
                            </ColumnLayout>}

                            {this.state.selectedFileSystem.length > 0 && <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValueGroup title={"General"}>
                                    <KeyValue title="Name" value={getSelectedFileSystem()?.getName()} clipboard={true}/>
                                    <KeyValue title="Title" value={getSelectedFileSystem()?.getTitle()} clipboard={true}/>
                                    <KeyValue title="Provider" value={getSelectedFileSystem()?.getProviderTitle()} clipboard={true}/>
                                    <KeyValue title="Is Existing File System?" value={getSelectedFileSystem()?.isExistingFileSystem()} type={"boolean"}/>
                                    <KeyValue title="File System Id" value={getSelectedFileSystem()?.getFileSystemId()} clipboard={true}/>
                                    {!getSelectedFileSystem()?.isFsxNetAppOntap() && <KeyValue title="DNS Name " value={getSelectedFileSystem()?.getFileSystemDns()} clipboard={true}/>}
                                </KeyValueGroup>

                                <KeyValueGroup title={"Mount Settings"}>
                                    {!getSelectedFileSystem()?.isFsxWindowsFileServer() && <KeyValue title="Mount Directory (Linux)" value={getSelectedFileSystem()?.getMountDirectory()} clipboard={true}/>}
                                    {getSelectedFileSystem()?.hasMountDrive() && <KeyValue title="Mount Drive (Windows)" value={getSelectedFileSystem()?.getMountDrive()}/>}
                                    {!getSelectedFileSystem()?.isFsxWindowsFileServer() && <KeyValue title="Mount Options" value={getSelectedFileSystem()?.getMountOptions()} clipboard={true}/>}
                                    <KeyValue title="Scope" value={getSelectedFileSystem()?.getScope()}/>
                                    {getSelectedFileSystem()?.isScopeProjects() && <KeyValue title="Projects" value={getSelectedFileSystem()?.getProjects()}/>}
                                    {getSelectedFileSystem()?.isScopeModule() && <KeyValue title="Modules" value={getSelectedFileSystem()?.getModules()}/>}
                                    {getSelectedFileSystem()?.isScopeQueueProfile() && <KeyValue title="Queue Profiles" value={getSelectedFileSystem()?.getQueueProfiles()}/>}
                                    {getSelectedFileSystem()?.isFsxLustre() && <KeyValue title="FSx for Lustre: Mount Name" value={getSelectedFileSystem()?.getMountName()}/>}
                                    {getSelectedFileSystem()?.isFsxLustre() && <KeyValue title="FSx for Lustre: Version" value={getSelectedFileSystem()?.getLustreVersion()}/>}
                                </KeyValueGroup>

                                </SpaceBetween><SpaceBetween size="m">{getSelectedFileSystem()?.isFsxNetAppOntap() && <KeyValueGroup title={"Storage Virtual Machine"}>
                                    <KeyValue title="Storage Virtual Machine Id" value={getSelectedFileSystem()?.getSvmId()} clipboard={true}/>
                                    <KeyValue title="SMB DNS" value={getSelectedFileSystem()?.getSvmSmbDns()} clipboard={true}/>
                                    <KeyValue title="NFS DNS" value={getSelectedFileSystem()?.getSvmNfsDns()} clipboard={true}/>
                                    <KeyValue title="Management DNS" value={getSelectedFileSystem()?.getSvmManagementDns()} clipboard={true}/>
                                    <KeyValue title="iSCSI DNS" value={getSelectedFileSystem()?.getSvmIscsiDns()} clipboard={true}/>
                                </KeyValueGroup>}

                                {getSelectedFileSystem()?.isVolumeApplicable() && <KeyValueGroup title={"Volume"}>
                                    <KeyValue title="Volume Id" value={getSelectedFileSystem()?.getVolumeId()} clipboard={true}/>
                                    <KeyValue title="Volume Path" value={getSelectedFileSystem()?.getVolumePath()} clipboard={true}/>
                                    {getSelectedFileSystem()?.isFsxNetAppOntap() && <KeyValue title="Security Style" value={getSelectedFileSystem()?.getVolumeSecurityStyle()} clipboard={true}/>}
                                </KeyValueGroup>}

                            </SpaceBetween></ColumnLayout>}
                        </Container>
                    </SpaceBetween>
                )
            },
            {
                label: 'Identity Provider',
                id: 'identity-provider',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>Identity Provider</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Provider Name" value={dot.pick('provider', this.state.identityProvider)}/>
                                <KeyValue title="User Pool Id" value={dot.pick('cognito.user_pool_id', this.state.identityProvider)} clipboard={true} type={"cognito:user-pool-id"}/>
                                <KeyValue title="Administrators Group Name" value={dot.pick('cognito.administrators_group_name', this.state.identityProvider)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Managers Group Name" value={dot.pick('cognito.managers_group_name', this.state.identityProvider)} clipboard={true}/>
                                <KeyValue title="Domain URL" value={dot.pick('cognito.domain_url', this.state.identityProvider)} clipboard={true}/>
                                <KeyValue title="Provider URL" value={dot.pick('cognito.provider_url', this.state.identityProvider)} clipboard={true}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                        <Container header={<Header variant={"h2"}>Single Sign-On</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={3}>
                                <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={isSingleSignOnEnabled()}/>} type={"react-node"}/>
                            </ColumnLayout>
                        </Container>
                    </SpaceBetween>
                )
            },
            {
                label: 'Directory Service',
                id: 'directory-service',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>Directory Service</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Provider" value={Utils.getDirectoryServiceTitle(dot.pick('provider', this.state.directoryservice))}/>
                                <KeyValue title="Automation Directory" value={dot.pick('automation_dir', this.state.directoryservice)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Root Username Secret ARN" value={dot.pick('root_username_secret_arn', this.state.directoryservice)} clipboard={true}/>
                                <KeyValue title="Root Password Secret ARN" value={dot.pick('root_password_secret_arn', this.state.directoryservice)} clipboard={true}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                        {isDirectoryServiceOpenLDAP() && <Container header={<Header variant={"h2"}>OpenLDAP Settings</Header>}>
                            <SpaceBetween size={"m"}>
                                <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                    <KeyValue title="Name" value={dot.pick('name', this.state.directoryservice)} clipboard={true}/>
                                    <KeyValue title="LDAP Base" value={dot.pick('ldap_base', this.state.directoryservice)} clipboard={true}/>
                                    </SpaceBetween><SpaceBetween size="m"><KeyValue title="LDAP Connection URI" value={dot.pick('ldap_connection_uri', this.state.directoryservice)} clipboard={true}/>
                                </SpaceBetween></ColumnLayout>
                                <Box>
                                    <h3>EC2 Instance Details</h3>
                                    <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                        <KeyValue title="Hostname" value={dot.pick('hostname', this.state.directoryservice)} clipboard={true}/>
                                        <KeyValue title="Private IP" value={dot.pick('private_ip', this.state.directoryservice)} clipboard={true}/>
                                        <KeyValue title="Instance Id" value={dot.pick('instance_id', this.state.directoryservice)} clipboard={true} type={"ec2:instance-id"}/>
                                        <KeyValue title="Instance Type" value={dot.pick('instance_type', this.state.directoryservice)}/>
                                        <KeyValue title="Security Group Id" value={dot.pick('security_group_id', this.state.directoryservice)} clipboard={true} type={"ec2:security-group-id"}/>
                                        </SpaceBetween><SpaceBetween size="m"><KeyValue title="Base OS" value={Utils.getOsTitle(dot.pick('base_os', this.state.directoryservice))}/>
                                        <KeyValue title="CloudWatch Logs" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('cloudwatch_logs.enabled', this.state.directoryservice), false)}/>} type={"react-node"}/>
                                        <KeyValue title="Is Public?" value={dot.pick('public', this.state.directoryservice)} type={"boolean"}/>
                                        <KeyValue title="Public IP" value={dot.pick('public_ip', this.state.directoryservice)}/>
                                    </SpaceBetween></ColumnLayout>
                                </Box>
                            </SpaceBetween>
                        </Container>}
                        {isDirectoryServiceActiveDirectory() && <Container header={<Header variant={"h2"}>Microsoft AD Settings</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Directory Id" value={dot.pick('directory_id', this.state.directoryservice)}/>
                                <KeyValue title="Short Name (NETBIOS)" value={dot.pick('ad_short_name', this.state.directoryservice)}/>
                                <KeyValue title="Edition" value={dot.pick('ad_edition', this.state.directoryservice)}/>
                                <KeyValue title="Domain Name" value={dot.pick('name', this.state.directoryservice)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Password Max Age" value={dot.pick('password_max_age', this.state.directoryservice)} suffix={"days"}/>
                                <KeyValue title="AD Automation SQS Queue Url" value={dot.pick('ad_automation.sqs_queue_url', this.state.directoryservice)} clipboard={true}/>
                                <KeyValue title="AD Automation DynamoDB Table Name" value={`${AppContext.get().auth().getClusterName()}.ad-automation`} clipboard={true}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>}
                    </SpaceBetween>
                )
            },
            {
                label: 'Analytics',
                id: 'analytics',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>OpenSearch Settings</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Domain Name" value={dot.pick('opensearch.domain_name', this.state.analytics)} clipboard={true}/>
                                <KeyValue title="Domain ARN" value={dot.pick('opensearch.domain_arn', this.state.analytics)} clipboard={true}/>
                                <KeyValue title="Domain Endpoint" value={dot.pick('opensearch.domain_endpoint', this.state.analytics)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Dashboard URL" value={getOpenSearchDashboardUrl()} type={"external-link"} clipboard={true}/>
                                <KeyValue title="Existing OpenSearch Service Domain?" value={dot.pick('opensearch.use_existing', this.state.analytics)} type={"boolean"}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                        <Container header={<Header variant={"h2"}>Kinesis Settings</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Stream Name" value={dot.pick('kinesis.stream_name', this.state.analytics)} clipboard={true}/>
                                <KeyValue title="Stream ARN" value={dot.pick('kinesis.stream_arn', this.state.analytics)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Stream Mode" value={dot.pick('kinesis.stream_mode', this.state.analytics)}/>
                                <KeyValue title="Shard Count" value={dot.pick('kinesis.shard_count', this.state.analytics)}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                    </SpaceBetween>
                )
            },
            {
                label: 'Metrics',
                id: 'metrics',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>Metrics</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={isMetricsEnabled()}/>} type={"react-node"}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Provider Name" value={dot.pick('provider', this.state.metrics)}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                        {isMetricsProviderCloudWatch() && <Container header={<Header variant={"h2"}>CloudWatch Metrics</Header>}>
                            <SpaceBetween size={"m"}>
                                <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                    <KeyValue title="Metrics Collection Interval" value={dot.pick('cloudwatch.metrics_collection_interval', this.state.metrics)} suffix={"seconds"}/>
                                    </SpaceBetween><SpaceBetween size="m"><KeyValue title="Force Flush Interval" value={dot.pick('cloudwatch.force_flush_interval', this.state.metrics)} suffix={"seconds"}/>
                                </SpaceBetween></ColumnLayout>
                                <Box>
                                    <h4>CloudWatch Dashboard</h4>
                                    <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                        <KeyValue title="Dashboard ARN" value={dot.pick('cloudwatch.dashboard_arn', this.state.metrics)} clipboard={true}/>
                                        </SpaceBetween><SpaceBetween size="m"><KeyValue title="Dashboard Name" value={dot.pick('cloudwatch.dashboard_name', this.state.metrics)} clipboard={true}/>
                                    </SpaceBetween></ColumnLayout>
                                </Box>
                            </SpaceBetween>
                        </Container>}
                        {isMetricsProviderAmazonManagedPrometheus() && <Container header={<Header variant={"h2"}>Amazon Managed Prometheus</Header>}>
                            <SpaceBetween size={"m"}>
                                <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                    <KeyValue title="Workspace Name" value={dot.pick('amazon_managed_prometheus.workspace_name', this.state.metrics)}/>
                                    <KeyValue title="Workspace ID" value={dot.pick('amazon_managed_prometheus.workspace_id', this.state.metrics)}/>
                                    <KeyValue title="Workspace ARN" value={dot.pick('amazon_managed_prometheus.workspace_arn', this.state.metrics)}/>
                                    </SpaceBetween><SpaceBetween size="m"><KeyValue title="Remote Write Url" value={dot.pick('prometheus.remote_write.url', this.state.metrics)}/>
                                    <KeyValue title="Remote Read Url" value={dot.pick('prometheus.remote_read.url', this.state.metrics)}/>
                                </SpaceBetween></ColumnLayout>
                            </SpaceBetween>
                        </Container>}
                        {isMetricsProviderPrometheus() && <Container header={<Header variant={"h2"}>Custom Prometheus</Header>}>
                            <SpaceBetween size={"m"}>
                                <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                    <KeyValue title="Remote Write Url" value={dot.pick('prometheus.remote_write.url', this.state.metrics)}/>
                                    </SpaceBetween><SpaceBetween size="m"><KeyValue title="Remote Read Url" value={dot.pick('prometheus.remote_read.url', this.state.metrics)}/>
                                </SpaceBetween></ColumnLayout>
                            </SpaceBetween>
                        </Container>}
                    </SpaceBetween>
                )
            },
            {
                label: 'Maintenance',
                id: 'maintenance',
                content: this.buildMaintenanceSettings()
            },
            {
                label: 'Account reconciliation',
                id: 'account-reconciliation',
                content: <AccountReconcileSettings active={true} identityProvider={this.state.identityProvider} mode="policy" highlightedKey={this.props.searchParams.get('key')} editDisabled={Boolean(this.props.activeEditor && this.props.activeEditor !== 'sign-in:Account synchronization')} onEditingChange={editing => this.props.onEditingChange?.(editing ? 'sign-in:Account synchronization' : null)}/>
            },
            {
                label: 'CloudWatch Logs',
                id: 'cloudwatch-logs',
                content: (
                    <Container header={<Header variant={"h2"}>CloudWatch Logs</Header>}>
                        <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                            <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('cloudwatch_logs.enabled', this.state.cluster), false)}/>} type={"react-node"}/>
                            <KeyValue title="Force Flush Interval" value={dot.pick('cloudwatch_logs.force_flush_interval', this.state.cluster)} suffix={"seconds"}/>
                            </SpaceBetween><SpaceBetween size="m"><KeyValue title="Log Retention" value={dot.pick('cloudwatch_logs.retention_in_days', this.state.cluster)} suffix={"days"}/>
                        </SpaceBetween></ColumnLayout>
                    </Container>
                )
            },
            {
                label: 'SES',
                id: 'ses',
                content: (
                    <Container header={<Header variant={"h2"}>Simple Email Service (SES)</Header>}>
                        <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                            <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('ses.enabled', this.state.cluster), false)}/>} type={"react-node"}/>
                            <KeyValue title="AWS Account ID" value={dot.pick('ses.account_id', this.state.cluster)} clipboard={true}/>
                            <KeyValue title="AWS Region" value={dot.pick('ses.region', this.state.cluster)}/>
                            </SpaceBetween><SpaceBetween size="m"><KeyValue title="Sender Email" value={dot.pick('ses.sender_email', this.state.cluster)} clipboard={true}/>
                            <KeyValue title="Max Sending Rate" value={dot.pick('ses.max_sending_rate', this.state.cluster)} suffix={" / second"}/>
                        </SpaceBetween></ColumnLayout>
                    </Container>
                )
            },
            {
                label: 'EC2',
                id: 'ec2',
                content: (
                    <Container header={<Header variant={"h2"}>EC2</Header>}>
                        <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                            <KeyValue title="SSH Key Pair" value={dot.pick('network.ssh_key_pair', this.state.cluster)} clipboard={true}/>
                            </SpaceBetween><SpaceBetween size="m"><KeyValue title="Custom EC2 Managed Policy ARNs" value={dot.pick('iam.ec2_managed_policy_arns', this.state.cluster)} clipboard={true}/>
                        </SpaceBetween></ColumnLayout>
                    </Container>
                )
            },
            {
                label: 'Backup',
                id: 'backups',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>AWS Backup</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}>
                                <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={isBackupEnabled()}/>} type={"react-node"}/>
                            </ColumnLayout>
                        </Container>
                        {isBackupEnabled() && <Container header={<Header variant={"h2"}>Backup Vault</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="ARN" value={dot.pick('backups.backup_vault.arn', this.state.cluster)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="KMS Key Id (CMK)" value={dot.pick('backups.backup_vault.kms_key_id', this.state.cluster)} clipboard={true}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>}
                        {isBackupEnabled() && <Container header={<Header variant={"h2"}>Backup Plan</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="ARN" value={dot.pick('backups.backup_plan.arn', this.state.cluster)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Selection" value={dot.pick('backups.backup_plan.selection.tags', this.state.cluster)}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>}
                    </SpaceBetween>
                )
            },
            {
                label: 'Route 53',
                id: 'route-53',
                content: (
                    <SpaceBetween size={"l"}>
                        <Container header={<Header variant={"h2"}>Private Hosted Zone</Header>}>
                            <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                                <KeyValue title="Hosted Zone Name" value={dot.pick('route53.private_hosted_zone_name', this.state.cluster)} clipboard={true}/>
                                <KeyValue title="Hosted Zone ID" value={dot.pick('route53.private_hosted_zone_id', this.state.cluster)} clipboard={true}/>
                                </SpaceBetween><SpaceBetween size="m"><KeyValue title="Hosted Zone ARN" value={dot.pick('route53.private_hosted_zone_arn', this.state.cluster)} clipboard={true}/>
                            </SpaceBetween></ColumnLayout>
                        </Container>
                    </SpaceBetween>
                )
            },
            {
                label: 'AWS Account',
                id: 'aws-account',
                content: (
                    <Container header={<Header variant={"h2"}>AWS Account Settings</Header>}>
                        <ColumnLayout variant={"text-grid"} columns={2} minColumnWidth={280}><SpaceBetween size="m">
                            <KeyValue title="AWS Account ID" value={dot.pick('aws.account_id', this.state.cluster)} clipboard={true}/>
                            <KeyValue title="AWS Region" value={dot.pick('aws.region', this.state.cluster)} clipboard={true}/>
                            <KeyValue title="Pricing API Region" value={dot.pick('aws.pricing_region', this.state.cluster)} clipboard={true}/>
                            </SpaceBetween><SpaceBetween size="m"><KeyValue title="AWS Partition" value={dot.pick('aws.partition', this.state.cluster)}/>
                            <KeyValue title="AWS DNS Suffix" value={dot.pick('aws.dns_suffix', this.state.cluster)}/>
                        </SpaceBetween></ColumnLayout>
                    </Container>
                )
            }
        ];
        return this.props.renderSections({sections, values: {cluster: this.state.cluster, 'cluster-manager': this.state.clusterManager, 'shared-storage': this.state.sharedStorage, 'virtual-desktop-controller': this.state.desktopSettings}, errors: this.state.settingsErrors})
    }
}

export default withRouter(ClusterSettings)
