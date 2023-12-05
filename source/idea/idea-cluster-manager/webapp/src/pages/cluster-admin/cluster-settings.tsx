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

import React, {Component} from "react";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {Box, Button, ColumnLayout, Container, Header, Link, SpaceBetween, Table, Tabs} from "@cloudscape-design/components";
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

}

export interface ClusterSettingsState {
    cluster: any
    identityProvider: any
    directoryservice: any
    sharedStorage: any
    analytics: any
    metrics: any
    activeTabId: string

    sharedStorageTableItems: any
    selectedFileSystem: SharedStorageFileSystem[]
}

const DEFAULT_ACTIVE_TAB_ID = 'general'

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
            activeTabId: DEFAULT_ACTIVE_TAB_ID,

            sharedStorageTableItems: [],
            selectedFileSystem: []
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
        if (clusterSettingsService.isMetricsEnabled()) {
            promises.push(clusterSettingsService.getMetricsSettings())
        }
        const queryParams = new URLSearchParams(this.props.location.search)
        const activeTabId = Utils.asString(queryParams.get('tab'), DEFAULT_ACTIVE_TAB_ID)
        Promise.all(promises).then(result => {
            const sharedStorageTableItems = this.getSharedStorageTableItems(result[3])
            this.setState({
                cluster: result[0],
                identityProvider: result[1],
                directoryservice: result[2],
                sharedStorage: result[3],
                analytics: result[4],
                metrics: (clusterSettingsService.isMetricsEnabled()) ? result[5] : {},
                activeTabId: activeTabId,
                sharedStorageTableItems: sharedStorageTableItems,
                selectedFileSystem: [sharedStorageTableItems[0]]
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

        const getClusterManagerOpenAPISpecUrl = () => {
            return `${AppContext.get().getHttpEndpoint()}${Utils.getApiContextPath(Constants.MODULE_CLUSTER_MANAGER)}/openapi.yml`
        }

        const getClusterManagerSwaggerEditorUrl = () => {
            return `https://editor-next.swagger.io/?url=${getClusterManagerOpenAPISpecUrl()}`
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

        return (
            <IdeaAppLayout
                ideaPageId={this.props.ideaPageId}
                toolsOpen={this.props.toolsOpen}
                tools={this.props.tools}
                onToolsChange={this.props.onToolsChange}
                onPageChange={this.props.onPageChange}
                sideNavHeader={this.props.sideNavHeader}
                sideNavItems={this.props.sideNavItems}
                onSideNavChange={this.props.onSideNavChange}
                onFlashbarChange={this.props.onFlashbarChange}
                flashbarItems={this.props.flashbarItems}
                breadcrumbItems={[
                    {
                        text: 'IDEA',
                        href: '#/'
                    },
                    {
                        text: 'Cluster Management',
                        href: '#/cluster/status'
                    },
                    {
                        text: 'Settings',
                        href: ''
                    }
                ]}
                header={(
                    <Header variant={"h1"}
                            description={"View and manage cluster settings. (Read-Only, use idea-admin.sh to update cluster settings.)"}
                            actions={(<SpaceBetween size={"s"}>
                                <Button variant={"primary"} onClick={() => this.props.navigate('/cluster/status')}>View Cluster Status</Button>
                            </SpaceBetween>)}>
                        Cluster Settings
                    </Header>
                )}
                contentType={"default"}
                content={
                    <SpaceBetween size={"l"}>
                        <Container>
                            <ColumnLayout variant={"text-grid"} columns={3}>
                                <KeyValue title="Cluster Name" value={dot.pick('cluster_name', this.state.cluster)} clipboard={true}/>
                                <KeyValue title="AWS Region" value={dot.pick('aws.region', this.state.cluster)}/>
                                <KeyValue title="S3 Bucket" value={dot.pick('cluster_s3_bucket', this.state.cluster)} clipboard={true} type={"s3:bucket-name"}/>
                            </ColumnLayout>
                        </Container>
                        <Tabs
                            activeTabId={this.state.activeTabId}
                            onChange={(event) => {
                                this.setState({
                                    activeTabId: event.detail.activeTabId
                                }, () => {
                                    this.props.searchParams.set('tab', event.detail.activeTabId)
                                    this.props.setSearchParams(this.props.searchParams)
                                })
                            }}
                            tabs={[
                                {
                                    label: 'General',
                                    id: 'general',
                                    content: (
                                        <SpaceBetween size="m">
                                            <Container header={<Header variant={"h2"}>General Settings</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Administrator Username" value={dot.pick('administrator_username', this.state.cluster)}/>
                                                    <KeyValue title="Administrator Email" value={dot.pick('administrator_email', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Cluster Home Directory" value={dot.pick('home_dir', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Locale" value={dot.pick('locale', this.state.cluster)}/>
                                                    <KeyValue title="Timezone" value={dot.pick('timezone', this.state.cluster)}/>
                                                    <KeyValue title="Default Encoding" value={dot.pick('encoding', this.state.cluster)}/>
                                                </ColumnLayout>
                                            </Container>
                                            <Container header={<Header variant={"h2"} info={<Link external={true} href={"https://spec.openapis.org/oas/v3.1.0"}>Info</Link>}>OpenAPI Specification</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={1}>
                                                    <KeyValue title="Cluster Manager API Spec" value={getClusterManagerOpenAPISpecUrl()} type={"external-link"} clipboard/>
                                                    <KeyValue title="Swagger Editor" value={getClusterManagerSwaggerEditorUrl()} type={"external-link"} clipboard/>
                                                </ColumnLayout>
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
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="VPC Id" value={dot.pick('network.vpc_id', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Private Subnets" value={dot.pick('network.private_subnets', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Public Subnets" value={dot.pick('network.public_subnets', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Cluster Prefix List Id" value={dot.pick('network.cluster_prefix_list_id', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Existing VPC?" value={dot.pick('network.use_existing_vpc', this.state.cluster)} type={"boolean"}/>
                                                </ColumnLayout>
                                            </Container>
                                            <Container header={<Header variant={"h2"}>Security Groups</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Bastion Host" value={dot.pick('network.security_groups.bastion-host', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                                                    <KeyValue title="External Load Balancer" value={dot.pick('network.security_groups.external-load-balancer', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                                                    <KeyValue title="Internal Load Balancer" value={dot.pick('network.security_groups.internal-load-balancer', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                                                    <KeyValue title="Default Security Group" value={dot.pick('network.security_groups.cluster', this.state.cluster)} clipboard={true} type={"ec2:security-group-id"}/>
                                                </ColumnLayout>
                                            </Container>
                                            <Container header={<Header variant={"h2"}>External Load Balancer</Header>}>
                                                <SpaceBetween size={"m"}>
                                                    <ColumnLayout variant={"text-grid"} columns={2}>
                                                        <KeyValue title="Load Balancer DNS Name" value={ConfigUtils.getExternalAlbDnsName(this.state.cluster)} clipboard={true}/>
                                                        <KeyValue title="Custom DNS Name" value={ConfigUtils.getExternalAlbCustomDnsName(this.state.cluster)} clipboard={true}/>
                                                        <KeyValue title="Load Balancer ARN" value={ConfigUtils.getExternalAlbArn(this.state.cluster)} clipboard={true}/>
                                                        <KeyValue title="Deploy in Public Subnets?" value={dot.pick('load_balancers.external_alb.public', this.state.cluster)} type={"boolean"}/>
                                                    </ColumnLayout>
                                                    <Box>
                                                        <h3>SSL/TLS Settings</h3>
                                                        <ColumnLayout variant={"text-grid"} columns={2}>
                                                            <KeyValue title="Certificates" value={isExternalAlbCertSelfSigned() ? 'Self-Signed' : 'ACM'}/>
                                                            {isExternalAlbCertSelfSigned() && <KeyValue title="Certificate Secret ARN" value={ConfigUtils.getExternalAlbCertificateSecretArn(this.state.cluster)} clipboard={true}/>}
                                                            {isExternalAlbCertSelfSigned() && <KeyValue title="Certificate Private Key Secret ARN" value={ConfigUtils.getExternalAlbPrivateKeySecretArn(this.state.cluster)} clipboard={true}/>}
                                                            <KeyValue title="ACM Certificate ARN" value={ConfigUtils.getExternalAlbAcmCertificateArn(this.state.cluster)} clipboard={true}/>
                                                        </ColumnLayout>
                                                    </Box>
                                                </SpaceBetween>
                                            </Container>
                                            <Container header={<Header variant={"h2"}>Internal Load Balancer</Header>}>
                                                <SpaceBetween size={"m"}>
                                                    <ColumnLayout variant={"text-grid"} columns={2}>
                                                        <KeyValue title="Load Balancer DNS Name" value={ConfigUtils.getInternalAlbDnsName(this.state.cluster)} clipboard={true}/>
                                                        <KeyValue title="Custom DNS Name" value={ConfigUtils.getInternalAlbCustomDnsName(this.state.cluster)} clipboard={true}/>
                                                        <KeyValue title="Load Balancer ARN" value={ConfigUtils.getInternalAlbArn(this.state.cluster)} clipboard={true}/>
                                                    </ColumnLayout>
                                                    <Box>
                                                        <h3>SSL/TLS Settings</h3>
                                                        <ColumnLayout variant={"text-grid"} columns={2}>
                                                            <KeyValue title="Certificates" value="Self-Signed"/>
                                                            <KeyValue title="Certificate Secret ARN" value={ConfigUtils.getInternalAlbCertificateSecretArn(this.state.cluster)} clipboard={true}/>
                                                            <KeyValue title="Certificate Private Key Secret ARN" value={ConfigUtils.getInternalAlbPrivateKeySecretArn(this.state.cluster)} clipboard={true}/>
                                                            <KeyValue title="ACM Certificate ARN" value={ConfigUtils.getInternalAlbAcmCertificateArn(this.state.cluster)} clipboard={true}/>
                                                        </ColumnLayout>
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

                                                {this.state.selectedFileSystem.length > 0 && <ColumnLayout variant={"text-grid"} columns={1}>
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

                                                    {getSelectedFileSystem()?.isFsxNetAppOntap() && <KeyValueGroup title={"Storage Virtual Machine"}>
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

                                                </ColumnLayout>}
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
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Provider Name" value={dot.pick('provider', this.state.identityProvider)}/>
                                                    <KeyValue title="User Pool Id" value={dot.pick('cognito.user_pool_id', this.state.identityProvider)} clipboard={true} type={"cognito:user-pool-id"}/>
                                                    <KeyValue title="Administrators Group Name" value={dot.pick('cognito.administrators_group_name', this.state.identityProvider)} clipboard={true}/>
                                                    <KeyValue title="Managers Group Name" value={dot.pick('cognito.managers_group_name', this.state.identityProvider)} clipboard={true}/>
                                                    <KeyValue title="Domain URL" value={dot.pick('cognito.domain_url', this.state.identityProvider)} clipboard={true}/>
                                                    <KeyValue title="Provider URL" value={dot.pick('cognito.provider_url', this.state.identityProvider)} clipboard={true}/>
                                                </ColumnLayout>
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
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="Provider" value={Utils.getDirectoryServiceTitle(dot.pick('provider', this.state.directoryservice))}/>
                                                    <KeyValue title="Automation Directory" value={dot.pick('automation_dir', this.state.directoryservice)} clipboard={true}/>
                                                    <KeyValue title="Root Username Secret ARN" value={dot.pick('root_username_secret_arn', this.state.directoryservice)} clipboard={true}/>
                                                    <KeyValue title="Root Password Secret ARN" value={dot.pick('root_password_secret_arn', this.state.directoryservice)} clipboard={true}/>
                                                </ColumnLayout>
                                            </Container>
                                            {isDirectoryServiceOpenLDAP() && <Container header={<Header variant={"h2"}>OpenLDAP Settings</Header>}>
                                                <SpaceBetween size={"m"}>
                                                    <ColumnLayout variant={"text-grid"} columns={3}>
                                                        <KeyValue title="Name" value={dot.pick('name', this.state.directoryservice)} clipboard={true}/>
                                                        <KeyValue title="LDAP Base" value={dot.pick('ldap_base', this.state.directoryservice)} clipboard={true}/>
                                                        <KeyValue title="LDAP Connection URI" value={dot.pick('ldap_connection_uri', this.state.directoryservice)} clipboard={true}/>
                                                    </ColumnLayout>
                                                    <Box>
                                                        <h3>EC2 Instance Details</h3>
                                                        <ColumnLayout variant={"text-grid"} columns={3}>
                                                            <KeyValue title="Hostname" value={dot.pick('hostname', this.state.directoryservice)} clipboard={true}/>
                                                            <KeyValue title="Private IP" value={dot.pick('private_ip', this.state.directoryservice)} clipboard={true}/>
                                                            <KeyValue title="Instance Id" value={dot.pick('instance_id', this.state.directoryservice)} clipboard={true} type={"ec2:instance-id"}/>
                                                            <KeyValue title="Instance Type" value={dot.pick('instance_type', this.state.directoryservice)}/>
                                                            <KeyValue title="Security Group Id" value={dot.pick('security_group_id', this.state.directoryservice)} clipboard={true} type={"ec2:security-group-id"}/>
                                                            <KeyValue title="Base OS" value={Utils.getOsTitle(dot.pick('base_os', this.state.directoryservice))}/>
                                                            <KeyValue title="CloudWatch Logs Enabled" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('cloudwatch_logs.enabled', this.state.directoryservice), false)}/>} type={"react-node"}/>
                                                            <KeyValue title="Is Public?" value={dot.pick('public', this.state.directoryservice)} type={"boolean"}/>
                                                            <KeyValue title="Public IP" value={dot.pick('public_ip', this.state.directoryservice)}/>
                                                        </ColumnLayout>
                                                    </Box>
                                                </SpaceBetween>
                                            </Container>}
                                            {isDirectoryServiceActiveDirectory() && <Container header={<Header variant={"h2"}>Microsoft AD Settings</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="Directory Id" value={dot.pick('directory_id', this.state.directoryservice)}/>
                                                    <KeyValue title="Short Name (NETBIOS)" value={dot.pick('ad_short_name', this.state.directoryservice)}/>
                                                    <KeyValue title="Edition" value={dot.pick('ad_edition', this.state.directoryservice)}/>
                                                    <KeyValue title="Domain Name" value={dot.pick('name', this.state.directoryservice)} clipboard={true}/>
                                                    <KeyValue title="Password Max Age" value={dot.pick('password_max_age', this.state.directoryservice)} suffix={"days"}/>
                                                    <KeyValue title="AD Automation SQS Queue Url" value={dot.pick('ad_automation.sqs_queue_url', this.state.directoryservice)} clipboard={true}/>
                                                    <KeyValue title="AD Automation DynamoDB Table Name" value={`${AppContext.get().auth().getClusterName()}.ad-automation`} clipboard={true}/>
                                                </ColumnLayout>
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
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="Domain Name" value={dot.pick('opensearch.domain_name', this.state.analytics)} clipboard={true}/>
                                                    <KeyValue title="Domain ARN" value={dot.pick('opensearch.domain_arn', this.state.analytics)} clipboard={true}/>
                                                    <KeyValue title="Domain Endpoint" value={dot.pick('opensearch.domain_endpoint', this.state.analytics)} clipboard={true}/>
                                                    <KeyValue title="Dashboard URL" value={getOpenSearchDashboardUrl()} type={"external-link"} clipboard={true}/>
                                                    <KeyValue title="Existing OpenSearch Service Domain?" value={dot.pick('opensearch.use_existing', this.state.analytics)} type={"boolean"}/>
                                                </ColumnLayout>
                                            </Container>
                                            <Container header={<Header variant={"h2"}>Kinesis Settings</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="Stream Name" value={dot.pick('kinesis.stream_name', this.state.analytics)} clipboard={true}/>
                                                    <KeyValue title="Stream ARN" value={dot.pick('kinesis.stream_arn', this.state.analytics)} clipboard={true}/>
                                                    <KeyValue title="Stream Mode" value={dot.pick('kinesis.stream_mode', this.state.analytics)}/>
                                                    <KeyValue title="Shard Count" value={dot.pick('kinesis.shard_count', this.state.analytics)}/>
                                                </ColumnLayout>
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
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={isMetricsEnabled()}/>} type={"react-node"}/>
                                                    <KeyValue title="Provider Name" value={dot.pick('provider', this.state.metrics)}/>
                                                </ColumnLayout>
                                            </Container>
                                            {isMetricsProviderCloudWatch() && <Container header={<Header variant={"h2"}>CloudWatch Metrics</Header>}>
                                                <SpaceBetween size={"m"}>
                                                    <ColumnLayout variant={"text-grid"} columns={3}>
                                                        <KeyValue title="Metrics Collection Interval" value={dot.pick('cloudwatch.metrics_collection_interval', this.state.metrics)} suffix={"seconds"}/>
                                                        <KeyValue title="Force Flush Interval" value={dot.pick('cloudwatch.force_flush_interval', this.state.metrics)} suffix={"seconds"}/>
                                                    </ColumnLayout>
                                                    <Box>
                                                        <h4>CloudWatch Dashboard</h4>
                                                        <ColumnLayout variant={"text-grid"} columns={2}>
                                                            <KeyValue title="Dashboard ARN" value={dot.pick('cloudwatch.dashboard_arn', this.state.metrics)} clipboard={true}/>
                                                            <KeyValue title="Dashboard Name" value={dot.pick('cloudwatch.dashboard_name', this.state.metrics)} clipboard={true}/>
                                                        </ColumnLayout>
                                                    </Box>
                                                </SpaceBetween>
                                            </Container>}
                                            {isMetricsProviderAmazonManagedPrometheus() && <Container header={<Header variant={"h2"}>Amazon Managed Prometheus</Header>}>
                                                <SpaceBetween size={"m"}>
                                                    <ColumnLayout variant={"text-grid"} columns={3}>
                                                        <KeyValue title="Workspace Name" value={dot.pick('amazon_managed_prometheus.workspace_name', this.state.metrics)}/>
                                                        <KeyValue title="Workspace ID" value={dot.pick('amazon_managed_prometheus.workspace_id', this.state.metrics)}/>
                                                        <KeyValue title="Workspace ARN" value={dot.pick('amazon_managed_prometheus.workspace_arn', this.state.metrics)}/>
                                                        <KeyValue title="Remote Write Url" value={dot.pick('prometheus.remote_write.url', this.state.metrics)}/>
                                                        <KeyValue title="Remote Read Url" value={dot.pick('prometheus.remote_read.url', this.state.metrics)}/>
                                                    </ColumnLayout>
                                                </SpaceBetween>
                                            </Container>}
                                            {isMetricsProviderPrometheus() && <Container header={<Header variant={"h2"}>Custom Prometheus</Header>}>
                                                <SpaceBetween size={"m"}>
                                                    <ColumnLayout variant={"text-grid"} columns={3}>
                                                        <KeyValue title="Remote Write Url" value={dot.pick('prometheus.remote_write.url', this.state.metrics)}/>
                                                        <KeyValue title="Remote Read Url" value={dot.pick('prometheus.remote_read.url', this.state.metrics)}/>
                                                    </ColumnLayout>
                                                </SpaceBetween>
                                            </Container>}
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'CloudWatch Logs',
                                    id: 'cloudwatch-logs',
                                    content: (
                                        <Container header={<Header variant={"h2"}>CloudWatch Logs</Header>}>
                                            <ColumnLayout variant={"text-grid"} columns={3}>
                                                <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('cloudwatch_logs.enabled', this.state.cluster), false)}/>} type={"react-node"}/>
                                                <KeyValue title="Force Flush Interval" value={dot.pick('cloudwatch_logs.force_flush_interval', this.state.cluster)} suffix={"seconds"}/>
                                                <KeyValue title="Log Retention" value={dot.pick('cloudwatch_logs.retention_in_days', this.state.cluster)} suffix={"days"}/>
                                            </ColumnLayout>
                                        </Container>
                                    )
                                },
                                {
                                    label: 'SES',
                                    id: 'ses',
                                    content: (
                                        <Container header={<Header variant={"h2"}>Simple Email Service (SES)</Header>}>
                                            <ColumnLayout variant={"text-grid"} columns={3}>
                                                <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('ses.enabled', this.state.cluster), false)}/>} type={"react-node"}/>
                                                <KeyValue title="AWS Account ID" value={dot.pick('ses.account_id', this.state.cluster)} clipboard={true}/>
                                                <KeyValue title="AWS Region" value={dot.pick('ses.region', this.state.cluster)}/>
                                                <KeyValue title="Sender Email" value={dot.pick('ses.sender_email', this.state.cluster)} clipboard={true}/>
                                                <KeyValue title="Max Sending Rate" value={dot.pick('ses.max_sending_rate', this.state.cluster)} suffix={" / second"}/>
                                            </ColumnLayout>
                                        </Container>
                                    )
                                },
                                {
                                    label: 'EC2',
                                    id: 'ec2',
                                    content: (
                                        <Container header={<Header variant={"h2"}>EC2</Header>}>
                                            <ColumnLayout variant={"text-grid"} columns={1}>
                                                <KeyValue title="SSH Key Pair" value={dot.pick('network.ssh_key_pair', this.state.cluster)} clipboard={true}/>
                                                <KeyValue title="Custom EC2 Managed Policy ARNs" value={dot.pick('iam.ec2_managed_policy_arns', this.state.cluster)} clipboard={true}/>
                                            </ColumnLayout>
                                        </Container>
                                    )
                                },
                                {
                                    label: 'Backup',
                                    id: 'backups',
                                    content: (
                                        <SpaceBetween size={"l"}>
                                            <Container header={<Header variant={"h2"}>AWS Backup</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="Status" value={<EnabledDisabledStatusIndicator enabled={isBackupEnabled()}/>} type={"react-node"}/>
                                                </ColumnLayout>
                                            </Container>
                                            {isBackupEnabled() && <Container header={<Header variant={"h2"}>Backup Vault</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="ARN" value={dot.pick('backups.backup_vault.arn', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="KMS Key Id (CMK)" value={dot.pick('backups.backup_vault.kms_key_id', this.state.cluster)} clipboard={true}/>
                                                </ColumnLayout>
                                            </Container>}
                                            {isBackupEnabled() && <Container header={<Header variant={"h2"}>Backup Plan</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="ARN" value={dot.pick('backups.backup_plan.arn', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Selection" value={dot.pick('backups.backup_plan.selection.tags', this.state.cluster)}/>
                                                </ColumnLayout>
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
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="Hosted Zone Name" value={dot.pick('route53.private_hosted_zone_name', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Hosted Zone ID" value={dot.pick('route53.private_hosted_zone_id', this.state.cluster)} clipboard={true}/>
                                                    <KeyValue title="Hosted Zone ARN" value={dot.pick('route53.private_hosted_zone_arn', this.state.cluster)} clipboard={true}/>
                                                </ColumnLayout>
                                            </Container>
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'AWS Account',
                                    id: 'aws-account',
                                    content: (
                                        <Container header={<Header variant={"h2"}>AWS Account Settings</Header>}>
                                            <ColumnLayout variant={"text-grid"} columns={3}>
                                                <KeyValue title="AWS Account ID" value={dot.pick('aws.account_id', this.state.cluster)} clipboard={true}/>
                                                <KeyValue title="AWS Region" value={dot.pick('aws.region', this.state.cluster)} clipboard={true}/>
                                                <KeyValue title="Pricing API Region" value={dot.pick('aws.pricing_region', this.state.cluster)} clipboard={true}/>
                                                <KeyValue title="AWS Partition" value={dot.pick('aws.partition', this.state.cluster)}/>
                                                <KeyValue title="AWS DNS Suffix" value={dot.pick('aws.dns_suffix', this.state.cluster)}/>
                                            </ColumnLayout>
                                        </Container>
                                    )
                                }
                            ]}/>
                    </SpaceBetween>
                }/>
        )
    }
}

export default withRouter(ClusterSettings)
