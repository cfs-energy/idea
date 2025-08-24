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

import React, {Component, RefObject} from "react";

import {ColumnLayout, Container, Header, Link, SpaceBetween, Table, Tabs, Button} from "@cloudscape-design/components";
import IdeaForm from "../../components/form";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {KeyValue, KeyValueGroup} from "../../components/key-value";
import {AppContext} from "../../common";
import Utils from "../../common/utils";
import dot from "dot-object";
import {EnabledDisabledStatusIndicator} from "../../components/common";
import {Constants} from "../../common/constants";
import {withRouter} from "../../navigation/navigation-utils";
import ConfigUtils from "../../common/config-utils";
import { SimpleSettingsButton } from "../../components/simple-settings";
import {SocaUserInputParamMetadata, VirtualDesktopWeekSchedule} from "../../client/data-model";
import VirtualDesktopUtilsClient from "../../client/virtual-desktop-utils-client";
import DefaultScheduleModal from "./components/default-schedule-modal";

export interface VirtualDesktopSettingsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface VirtualDesktopSettingsState {
    vdcModuleInfo: any
    vdcSettings: any
    clusterSettings: any
    activeTabId: string
}

const DEFAULT_ACTIVE_TAB_ID = 'general'

class VirtualDesktopSettings extends Component<VirtualDesktopSettingsProps, VirtualDesktopSettingsState> {

    generalSettingsForm: RefObject<IdeaForm>
    defaultScheduleModal: RefObject<DefaultScheduleModal>

    constructor(props: VirtualDesktopSettingsProps) {
        super(props);
        this.generalSettingsForm = React.createRef()
        this.defaultScheduleModal = React.createRef()
        this.state = {
            vdcModuleInfo: {},
            vdcSettings: {},
            clusterSettings: {},
            activeTabId: DEFAULT_ACTIVE_TAB_ID
        }
    }

    getVirtualDesktopUtilsClient(): VirtualDesktopUtilsClient {
        return AppContext.get().client().virtualDesktopUtils()
    }

    componentDidMount() {
        let promises: Promise<any>[] = []
        const clusterSettingsService = AppContext.get().getClusterSettingsService()
        promises.push(clusterSettingsService.getClusterSettings())
        promises.push(clusterSettingsService.getVirtualDesktopSettings())

        Promise.all(promises).then(result => {
            const queryParams = new URLSearchParams(this.props.location.search)
            this.setState({
                vdcModuleInfo: AppContext.get().getClusterSettingsService().getModuleInfo(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER),
                vdcSettings: result[1],
                clusterSettings: result[0],
                activeTabId: Utils.asString(queryParams.get('tab'), DEFAULT_ACTIVE_TAB_ID)
            })
        })
    }

    // Dynamic settings configuration
    getSettingsConfig = (): {[key: string]: {config: SocaUserInputParamMetadata, path: string}} => {
        return {
            'idle_timeout': {
                path: 'dcv_session.idle_timeout',
                config: {
                    name: 'idle_timeout',
                    title: 'Idle Timeout (minutes)',
                    description: 'Time in minutes before idle sessions timeout',
                    param_type: 'text',
                    data_type: 'int',
                    validate: { required: true, min: 1, max: 1440 }
                }
            },
            'idle_timeout_warning': {
                path: 'dcv_session.idle_timeout_warning',
                config: {
                    name: 'idle_timeout_warning',
                    title: 'Idle Timeout Warning (seconds)',
                    description: 'Warning time in seconds before timeout',
                    param_type: 'text',
                    data_type: 'int',
                    validate: { required: true, min: 30, max: 3600 }
                }
            },
            'cpu_utilization_threshold': {
                path: 'dcv_session.cpu_utilization_threshold',
                config: {
                    name: 'cpu_utilization_threshold',
                    title: 'CPU Utilization Threshold (%)',
                    description: 'CPU threshold percentage for idle detection',
                    param_type: 'text',
                    data_type: 'int',
                    validate: { required: true, min: 1, max: 100 }
                }
            },
            'idle_autostop_delay': {
                path: 'dcv_session.idle_autostop_delay',
                config: {
                    name: 'idle_autostop_delay',
                    title: 'Idle AutoStop Delay (minutes)',
                    description: 'Time in minutes before auto-stopping idle instances',
                    param_type: 'text',
                    data_type: 'int',
                    validate: { required: true, min: 5, max: 1440 }
                }
            },
            'additional_security_groups': {
                path: 'dcv_session.additional_security_groups',
                config: {
                    name: 'additional_security_groups',
                    title: 'Allowed Security Groups',
                    description: 'Additional security groups that can be attached to virtual desktops',
                    param_type: 'text',
                    data_type: 'str',
                    multiple: true,
                    help_text: 'Enter one security group ID per line (e.g., sg-12345678)',
                    validate: { required: false }
                }
            },
            'max_root_volume_memory': {
                path: 'dcv_session.max_root_volume_memory',
                config: {
                    name: 'max_root_volume_memory',
                    title: 'Max Root Volume Size (GB)',
                    description: 'Maximum root volume size allowed for virtual desktops',
                    param_type: 'text',
                    data_type: 'int',
                    validate: { required: true, min: 10, max: 1000 }
                }
            },
            'allowed_instance_types': {
                path: 'dcv_session.instance_types.allow',
                config: {
                    name: 'allowed_instance_types',
                    title: 'Allowed Instance Types',
                    description: 'Instance types that users can select. Leave empty to allow all types.',
                    param_type: 'text',
                    data_type: 'str',
                    multiple: true,
                    help_text: 'Enter one instance type or family per line (e.g., t4, t3.xlarge, m5.large)',
                    validate: { required: false }
                }
            },
            'denied_instance_types': {
                path: 'dcv_session.instance_types.deny',
                config: {
                    name: 'denied_instance_types',
                    title: 'Denied Instance Types',
                    description: 'Instance types that users cannot select.',
                    param_type: 'text',
                    data_type: 'str',
                    multiple: true,
                    help_text: 'Enter one instance type or family per line (e.g., t4, t3.xlarge, m5.large)',
                    validate: { required: false }
                }
            },
            'subnet_autoretry': {
                path: 'dcv_session.network.subnet_autoretry',
                config: {
                    name: 'subnet_autoretry',
                    title: 'Subnet AutoRetry',
                    description: 'Automatically retry deployment on other subnets if the selected subnet fails',
                    param_type: 'confirm',
                    data_type: 'bool',
                    validate: { required: true }
                }
            },
            'randomize_subnets': {
                path: 'dcv_session.network.randomize_subnets',
                config: {
                    name: 'randomize_subnets',
                    title: 'Randomize Subnets',
                    description: 'Randomly select subnets for virtual desktop deployment',
                    param_type: 'confirm',
                    data_type: 'bool',
                    validate: { required: true }
                }
            },
            'evdi_subnets': {
                path: 'dcv_session.network.private_subnets',
                config: {
                    name: 'evdi_subnets',
                    title: 'eVDI Subnets',
                    description: 'Specific subnets for virtual desktop deployment. Leave empty to use cluster subnets.',
                    param_type: 'text',
                    data_type: 'str',
                    multiple: true,
                    help_text: 'Enter one subnet ID per line (e.g., subnet-12345678)',
                    validate: { required: false }
                }
            },
            'working_hours_start': {
                path: 'dcv_session.working_hours.start_up_time',
                config: {
                    name: 'working_hours_start',
                    title: 'Working Hours Start Time',
                    description: 'Start time for working hours schedule (24-hour format)',
                    param_type: 'text',
                    data_type: 'str',
                    help_text: 'Enter time in HH:MM format (e.g., 09:00)',
                    validate: { 
                        required: true,
                        regex: '^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$'
                    }
                }
            },
            'working_hours_end': {
                path: 'dcv_session.working_hours.shut_down_time',
                config: {
                    name: 'working_hours_end',
                    title: 'Working Hours End Time',
                    description: 'End time for working hours schedule (24-hour format)',
                    param_type: 'text',
                    data_type: 'str',
                    help_text: 'Enter time in HH:MM format (e.g., 17:00)',
                    validate: { 
                        required: true,
                        regex: '^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$'
                    }
                }
            }
        };
    };

    // Generic save handler
    handleSaveSetting = (settingKey: string) => async (newValue: any): Promise<boolean> => {
        const settingsConfig = this.getSettingsConfig();
        const setting = settingsConfig[settingKey];
        
        if (!setting) {
            console.error(`Unknown setting: ${settingKey}`);
            return false;
        }

        return this.updateSetting(setting.path, newValue);
    };

    // Generic method to get setting config
    getSettingConfig = (settingKey: string): SocaUserInputParamMetadata => {
        const settingsConfig = this.getSettingsConfig();
        const setting = settingsConfig[settingKey];
        
        if (!setting) {
            throw new Error(`Unknown setting: ${settingKey}`);
        }
        
        return setting.config;
    };

    // Generic method to get current setting value
    getCurrentValue = (settingKey: string): any => {
        const settingsConfig = this.getSettingsConfig();
        const setting = settingsConfig[settingKey];
        
        if (!setting) {
            return undefined;
        }
        
        // Check if settings are loaded by verifying the object has keys
        // This prevents showing incorrect default values before settings load
        if (Object.keys(this.state.vdcSettings).length === 0) {
            return undefined;
        }
        
        let value = dot.pick(setting.path, this.state.vdcSettings);
        
        // Handle different data types
        if (setting.config.data_type === 'bool') {
            return Utils.asBoolean(value);
        } else if (setting.config.multiple) {
            return value || [];
        } else {
            return value;
        }
    };

    // Helper method to create editable setting row
    createEditableSetting = (settingKey: string, displayTitle?: string) => {
        const settingsConfig = this.getSettingsConfig();
        const setting = settingsConfig[settingKey];
        
        if (!setting) {
            console.error(`Unknown setting: ${settingKey}`);
            return <div>Error: Unknown setting</div>;
        }

        const title = displayTitle || setting.config.title || settingKey;
        const settingPath = setting.path;
        const value = dot.pick(settingPath, this.state.vdcSettings);
        const suffix = settingPath.includes('timeout') ? (settingPath.includes('warning') ? 'seconds' : 'minutes') :
                      settingPath.includes('threshold') ? '%' :
                      settingPath.includes('memory') || settingPath.includes('volume') ? 'GB' : undefined;

        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <KeyValue title={title} value={value} suffix={suffix}/>
                <SimpleSettingsButton
                    title={`Edit ${title}`}
                    settingConfig={this.getSettingConfig(settingKey)}
                    currentValue={this.getCurrentValue(settingKey)}
                    onSave={this.handleSaveSetting(settingKey)}
                />
            </div>
        );
    };

    // Show default schedule modal
    showDefaultScheduleModal = () => {
        const currentDefaultSchedule: VirtualDesktopWeekSchedule = {
            monday: {
                schedule_type: dot.pick('dcv_session.schedule.monday.type', this.state.vdcSettings) || 'STOP_ON_IDLE',
                start_up_time: dot.pick('dcv_session.schedule.monday.start_up_time', this.state.vdcSettings),
                shut_down_time: dot.pick('dcv_session.schedule.monday.shut_down_time', this.state.vdcSettings)
            },
            tuesday: {
                schedule_type: dot.pick('dcv_session.schedule.tuesday.type', this.state.vdcSettings) || 'STOP_ON_IDLE',
                start_up_time: dot.pick('dcv_session.schedule.tuesday.start_up_time', this.state.vdcSettings),
                shut_down_time: dot.pick('dcv_session.schedule.tuesday.shut_down_time', this.state.vdcSettings)
            },
            wednesday: {
                schedule_type: dot.pick('dcv_session.schedule.wednesday.type', this.state.vdcSettings) || 'STOP_ON_IDLE',
                start_up_time: dot.pick('dcv_session.schedule.wednesday.start_up_time', this.state.vdcSettings),
                shut_down_time: dot.pick('dcv_session.schedule.wednesday.shut_down_time', this.state.vdcSettings)
            },
            thursday: {
                schedule_type: dot.pick('dcv_session.schedule.thursday.type', this.state.vdcSettings) || 'STOP_ON_IDLE',
                start_up_time: dot.pick('dcv_session.schedule.thursday.start_up_time', this.state.vdcSettings),
                shut_down_time: dot.pick('dcv_session.schedule.thursday.shut_down_time', this.state.vdcSettings)
            },
            friday: {
                schedule_type: dot.pick('dcv_session.schedule.friday.type', this.state.vdcSettings) || 'STOP_ON_IDLE',
                start_up_time: dot.pick('dcv_session.schedule.friday.start_up_time', this.state.vdcSettings),
                shut_down_time: dot.pick('dcv_session.schedule.friday.shut_down_time', this.state.vdcSettings)
            },
            saturday: {
                schedule_type: dot.pick('dcv_session.schedule.saturday.type', this.state.vdcSettings) || 'STOP_ON_IDLE',
                start_up_time: dot.pick('dcv_session.schedule.saturday.start_up_time', this.state.vdcSettings),
                shut_down_time: dot.pick('dcv_session.schedule.saturday.shut_down_time', this.state.vdcSettings)
            },
            sunday: {
                schedule_type: dot.pick('dcv_session.schedule.sunday.type', this.state.vdcSettings) || 'STOP_ON_IDLE',
                start_up_time: dot.pick('dcv_session.schedule.sunday.start_up_time', this.state.vdcSettings),
                shut_down_time: dot.pick('dcv_session.schedule.sunday.shut_down_time', this.state.vdcSettings)
            }
        };
        
        this.defaultScheduleModal.current?.showDefaultSchedule(currentDefaultSchedule);
    };

    // Update default schedule settings
    updateDefaultSchedule = async (defaultSchedule: VirtualDesktopWeekSchedule): Promise<boolean> => {
        try {
            // Convert the week schedule to nested settings object for API call
            const settings: any = {
                dcv_session: {
                    schedule: {}
                }
            };
            
            const daysOfWeek = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
            
            daysOfWeek.forEach(day => {
                const daySchedule = defaultSchedule[day as keyof VirtualDesktopWeekSchedule];
                if (daySchedule) {
                    settings.dcv_session.schedule[day] = {
                        type: daySchedule.schedule_type
                    };
                    
                    if (daySchedule.schedule_type === 'CUSTOM_SCHEDULE') {
                        settings.dcv_session.schedule[day].start_up_time = daySchedule.start_up_time;
                        settings.dcv_session.schedule[day].shut_down_time = daySchedule.shut_down_time;
                    } else {
                        // Clear custom times for non-custom schedules
                        settings.dcv_session.schedule[day].start_up_time = null;
                        settings.dcv_session.schedule[day].shut_down_time = null;
                    }
                }
            });

            // Call the cluster settings API to update the settings
            await AppContext.get().client().clusterSettings().updateModuleSettings({
                module_id: 'vdc',
                settings: settings
            });

            // Refresh the settings to reflect the changes
            const updatedSettings = await AppContext.get().getClusterSettingsService().getVirtualDesktopSettings();
            this.setState({
                vdcSettings: updatedSettings
            });

            // Show success message
            this.props.onFlashbarChange({
                items: [{
                    type: 'success',
                    content: 'Default schedule updated successfully. Changes will appear on refresh after a few seconds.',
                    dismissible: true
                }]
            });

            return true;
        } catch (error) {
            console.error('Failed to update default schedule:', error);
            this.defaultScheduleModal.current?.setErrorMessage('Failed to update default schedule. Please try again.');
            return false;
        }
    };

    // Single method to update any setting
    updateSetting = async (settingPath: string, newValue: any): Promise<boolean> => {
        try {
            const settings: any = {};
            const pathParts = settingPath.split('.');
            let current = settings;
            
            // Build nested object structure
            for (let i = 0; i < pathParts.length - 1; i++) {
                current[pathParts[i]] = {};
                current = current[pathParts[i]];
            }
            current[pathParts[pathParts.length - 1]] = newValue;

            const result = await AppContext.get().client().clusterSettings().updateModuleSettings({
                module_id: dot.pick('module_id', this.state.vdcModuleInfo) || Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
                settings: settings
            });

            if (result.success) {
                this.props.onFlashbarChange({
                    items: [{
                        type: 'success',
                        content: 'Setting updated successfully. Changes will appear on refresh after a few seconds.',
                        dismissible: true
                    }]
                });
                return true;
            } else {
                this.props.onFlashbarChange({
                    items: [{
                        type: 'error',
                        content: 'Failed to update setting',
                        dismissible: true
                    }]
                });
                return false;
            }
        } catch (error: any) {
            console.error('Error updating setting:', error);
            this.props.onFlashbarChange({
                items: [{
                    type: 'error',
                    content: `Error updating setting: ${error?.message || error}`,
                    dismissible: true
                }]
            });
            return false;
        }
    };

    buildNotificationSettings() {
        let notifications = dot.pick('dcv_session.notifications', this.state.vdcSettings)
        let items: any[] = []
        if (notifications) {
            Object.keys(notifications).forEach((settingName) => {
                items.push({
                    name: settingName,
                    enabled: Utils.asBoolean(dot.pick(`dcv_session.notifications.${settingName}.enabled`, this.state.vdcSettings)),
                    email_template: dot.pick(`dcv_session.notifications.${settingName}.email_template`, this.state.vdcSettings)
                })
            })
        }
        return <Table items={items} columnDefinitions={[
            {
                header: 'Session State',
                cell: e => e.name
            },
            {
                header: 'Status',
                cell: e => <EnabledDisabledStatusIndicator enabled={e.enabled}/>
            },
            {
                header: 'Email Template Name',
                cell: e => e.email_template
            }
        ]}/>
    }

    buildGeneralSettings() {
        return (
            <Container header={<Header variant={"h2"}>Module Information</Header>}>
                <ColumnLayout variant={"text-grid"} columns={3}>
                    <KeyValue title="Module Name" value={dot.pick('name', this.state.vdcModuleInfo)}/>
                    <KeyValue title="Module ID" value={dot.pick('module_id', this.state.vdcModuleInfo)}/>
                    <KeyValue title="Version" value={dot.pick('version', this.state.vdcModuleInfo)}/>
                </ColumnLayout>
            </Container>
        )
    }

    buildPermissionSettings() {
        return (
            <Container header={<Header variant={"h2"}>Permission Settings</Header>}>
                <ColumnLayout variant={"text-grid"} columns={2}>
                    <KeyValue title="QUIC Support">
                        <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('dcv_session.quic_support', this.state.vdcSettings))}/>
                    </KeyValue>
                    <KeyValue title="Subnet AutoRetry">
                        <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('dcv_session.network.subnet_autoretry', this.state.vdcSettings))}/>
                    </KeyValue>
                    <KeyValue title="Randomize Subnets">
                        <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('dcv_session.network.randomize_subnets', this.state.vdcSettings))}/>
                    </KeyValue>
                </ColumnLayout>
            </Container>
        )
    }

    render() {

        const getVirtualDesktopOpenAPISpecUrl = () => {
            return `${AppContext.get().getHttpEndpoint()}${Utils.getApiContextPath(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)}/openapi.yml`
        }

        const getVirtualDesktopSwaggerEditorUrl = () => {
            return `https://editor-next.swagger.io/?url=${getVirtualDesktopOpenAPISpecUrl()}`
        }

        const getInternalALBUrl = () => {
            return ConfigUtils.getInternalAlbUrl(this.state.clusterSettings)
        }

        const isClusterBackupEnabled = () => {
            return Utils.asBoolean(dot.pick('backups.enabled', this.state.clusterSettings))
        }

        const isVDIBackupEnabled = () => {
            return Utils.asBoolean(dot.pick('vdi_host_backup.enabled', this.state.vdcSettings))
        }

        const isBackupEnabled = () => {
            return isVDIBackupEnabled() && isClusterBackupEnabled()
        }

        const isGatewayCertificateSelfSigned = (): boolean => {
            return !Utils.asBoolean(dot.pick('dcv_connection_gateway.certificate.provided', this.state.vdcSettings), false)
        }

        const buildAutoScalingSettingContainer = (setting: any) => {
            return (
                <Container header={<Header variant={"h2"}>Autoscaling</Header>}>
                    <ColumnLayout variant={"text-grid"} columns={2}>
                        <KeyValue title="Min Instances" value={dot.pick('autoscaling.min_capacity', setting)}/>
                        <KeyValue title="Max Instances" value={dot.pick('autoscaling.max_capacity', setting)}/>
                        <KeyValue title="Scale-In Protection">
                            <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('autoscaling.new_instances_protected_from_scale_in', setting))}/>
                        </KeyValue>
                        <KeyValue title={"ARN"} value={dot.pick('asg_arn', setting)} clipboard={true} type={'ec2:asg-arn'}/>
                    </ColumnLayout>
                    <KeyValueGroup title={"Scaling Policy"}>
                        <KeyValue title="CPU Target Utilization" value={dot.pick('autoscaling.cpu_utilization_scaling_policy.target_utilization_percent', setting)} suffix={"%"}/>
                        <KeyValue title="Warmup time" value={dot.pick('autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes', setting)} suffix={"minutes"}/>
                    </KeyValueGroup>
                </Container>
            )
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
                        text: 'Virtual Desktops',
                        href: '#/virtual-desktop/dashboard'
                    },
                    {
                        text: 'Settings',
                        href: ''
                    }
                ]}
                header={<Header variant={"h1"} description={"Manage virtual desktop settings (Some settings are read-only, use idea-admin.sh to update read-only eVDI settings.)"}>Virtual Desktop Settings</Header>}
                contentType={"default"}
                content={
                    <SpaceBetween size={"l"}>
                        <Container>
                            <ColumnLayout variant={"text-grid"} columns={3}>
                                <KeyValue title="Module Name" value={dot.pick('name', this.state.vdcModuleInfo)}/>
                                <KeyValue title="Module ID" value={dot.pick('module_id', this.state.vdcModuleInfo)}/>
                                <KeyValue title="Version" value={dot.pick('version', this.state.vdcModuleInfo)}/>
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
                                        <SpaceBetween size={"m"}>
                                            <Container header={<Header variant={"h2"}>General</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="QUIC">
                                                        <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('dcv_session.quic_support', this.state.vdcSettings))}/>
                                                    </KeyValue>
                                                    {this.createEditableSetting('evdi_subnets')}
                                                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                                                        <KeyValue title="Subnet AutoRetry">
                                                            <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('dcv_session.network.subnet_autoretry', this.state.vdcSettings))}/>
                                                        </KeyValue>
                                                        <SimpleSettingsButton
                                                            title="Edit Subnet AutoRetry"
                                                            settingConfig={this.getSettingConfig('subnet_autoretry')}
                                                            currentValue={this.getCurrentValue('subnet_autoretry')}
                                                            onSave={this.handleSaveSetting('subnet_autoretry')}
                                                        />
                                                    </div>
                                                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                                                        <KeyValue title="Randomize Subnets">
                                                            <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('dcv_session.network.randomize_subnets', this.state.vdcSettings))}/>
                                                        </KeyValue>
                                                        <SimpleSettingsButton
                                                            title="Edit Randomize Subnets"
                                                            settingConfig={this.getSettingConfig('randomize_subnets')}
                                                            currentValue={this.getCurrentValue('randomize_subnets')}
                                                            onSave={this.handleSaveSetting('randomize_subnets')}
                                                        />
                                                    </div>
                                                </ColumnLayout>
                                            </Container>
                                            <Container header={<Header variant={"h2"} info={<Link external={true} href={"https://spec.openapis.org/oas/v3.1.0"}>Info</Link>}>OpenAPI Specification</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={1}>
                                                    <KeyValue title="eVDI API Spec" value={getVirtualDesktopOpenAPISpecUrl()} type={"external-link"} clipboard/>
                                                    <KeyValue title="Swagger Editor" value={getVirtualDesktopSwaggerEditorUrl()} type={"external-link"} clipboard/>
                                                </ColumnLayout>
                                            </Container>
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'Notifications',
                                    id: 'notifications',
                                    content: (
                                        <Container header={<Header variant={"h2"} description={"Notifications will be sent only if notifications are enabled in Cluster Settings"}>Notifications</Header>}>
                                            {this.buildNotificationSettings()}
                                        </Container>
                                    )
                                },
                                {
                                    label: 'Schedule',
                                    id: 'schedule',
                                    content: (
                                        <SpaceBetween size={"m"}>
                                            <Container header={<Header variant={"h2"} description={"Default schedule applied to all sessions"} actions={
                                                <Button variant="primary" onClick={() => this.showDefaultScheduleModal()}>
                                                    Edit Default Schedules
                                                </Button>
                                            }>Default Schedule</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Monday"
                                                              value={Utils.getScheduleTypeDisplay(dot.pick('dcv_session.schedule.monday.type', this.state.vdcSettings), dot.pick('dcv_session.working_hours.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.working_hours.shut_down_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.monday.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.monday.shut_down_time', this.state.vdcSettings))}/>
                                                    <KeyValue title="Tuesday"
                                                              value={Utils.getScheduleTypeDisplay(dot.pick('dcv_session.schedule.tuesday.type', this.state.vdcSettings), dot.pick('dcv_session.working_hours.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.working_hours.shut_down_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.tuesday.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.tuesday.shut_down_time', this.state.vdcSettings))}/>
                                                    <KeyValue title="Wednesday"
                                                              value={Utils.getScheduleTypeDisplay(dot.pick('dcv_session.schedule.wednesday.type', this.state.vdcSettings), dot.pick('dcv_session.working_hours.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.working_hours.shut_down_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.wednesday.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.wednesday.shut_down_time', this.state.vdcSettings))}/>
                                                    <KeyValue title="Thursday"
                                                              value={Utils.getScheduleTypeDisplay(dot.pick('dcv_session.schedule.thursday.type', this.state.vdcSettings), dot.pick('dcv_session.working_hours.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.working_hours.shut_down_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.thursday.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.thursday.shut_down_time', this.state.vdcSettings))}/>
                                                    <KeyValue title="Friday"
                                                              value={Utils.getScheduleTypeDisplay(dot.pick('dcv_session.schedule.friday.type', this.state.vdcSettings), dot.pick('dcv_session.working_hours.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.working_hours.shut_down_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.friday.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.friday.shut_down_time', this.state.vdcSettings))}/>
                                                    <KeyValue title="Saturday"
                                                              value={Utils.getScheduleTypeDisplay(dot.pick('dcv_session.schedule.saturday.type', this.state.vdcSettings), dot.pick('dcv_session.working_hours.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.working_hours.shut_down_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.saturday.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.saturday.shut_down_time', this.state.vdcSettings))}/>
                                                    <KeyValue title="Sunday"
                                                              value={Utils.getScheduleTypeDisplay(dot.pick('dcv_session.schedule.sunday.type', this.state.vdcSettings), dot.pick('dcv_session.working_hours.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.working_hours.shut_down_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.sunday.start_up_time', this.state.vdcSettings), dot.pick('dcv_session.schedule.sunday.shut_down_time', this.state.vdcSettings))}/>
                                                </ColumnLayout>
                                            </Container>
                                            <Container header={<Header variant={"h2"}>Working Hours</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    {this.createEditableSetting('working_hours_start')}
                                                    {this.createEditableSetting('working_hours_end')}
                                                </ColumnLayout>
                                            </Container>
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'Server',
                                    id: 'server',
                                    content: (
                                        <SpaceBetween size={"m"}>
                                            <Container header={<Header variant={"h2"}>DCV Session</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    {this.createEditableSetting('idle_timeout')}
                                                    {this.createEditableSetting('idle_timeout_warning')}
                                                    {this.createEditableSetting('cpu_utilization_threshold')}
                                                    {this.createEditableSetting('idle_autostop_delay')}
                                                </ColumnLayout>
                                            </Container>
                                            <Container header={<Header variant={"h2"}>DCV Host</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    {this.createEditableSetting('additional_security_groups')}
                                                    {this.createEditableSetting('max_root_volume_memory')}
                                                    {this.createEditableSetting('allowed_instance_types')}
                                                    {this.createEditableSetting('denied_instance_types')}
                                                </ColumnLayout>
                                            </Container>
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'Controller',
                                    id: 'controller',
                                    content: (
                                        <SpaceBetween size={"m"}>
                                            <Container header={<Header variant={"h2"}>VDI Controller</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Base OS" value={Utils.getOsTitle(dot.pick('controller.autoscaling.base_os', this.state.vdcSettings))}/>
                                                    <KeyValue title="Instance Type" value={dot.pick('controller.autoscaling.instance_type', this.state.vdcSettings)}/>
                                                    <KeyValue title="Volume" value={{
                                                        value: dot.pick('controller.autoscaling.volume_size', this.state.vdcSettings),
                                                        unit: 'GB'
                                                    }} type={"memory"}/>
                                                </ColumnLayout>
                                            </Container>
                                            {
                                                buildAutoScalingSettingContainer(dot.pick('controller', this.state.vdcSettings))
                                            }
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'Broker',
                                    id: 'broker',
                                    content: (
                                        <SpaceBetween size={'m'}>
                                            <Container header={<Header variant={"h2"}>Amazon DCV Broker</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Base OS" value={Utils.getOsTitle(dot.pick('dcv_broker.autoscaling.base_os', this.state.vdcSettings))}/>
                                                    <KeyValue title="Instance Type" value={dot.pick('dcv_broker.autoscaling.instance_type', this.state.vdcSettings)}/>
                                                    <KeyValue title="Volume" value={{
                                                        value: dot.pick('dcv_broker.autoscaling.volume_size', this.state.vdcSettings),
                                                        unit: 'GB'
                                                    }} type={"memory"}/>

                                                </ColumnLayout>
                                            </Container>
                                            {
                                                buildAutoScalingSettingContainer(dot.pick('dcv_broker', this.state.vdcSettings))
                                            }
                                            <Container header={<Header variant={"h2"}>Connection Details</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Client Port" value={dot.pick('dcv_broker.client_communication_port', this.state.vdcSettings)}/>
                                                    <KeyValue title="Agent Port" value={dot.pick('dcv_broker.agent_communication_port', this.state.vdcSettings)}/>
                                                    <KeyValue title="Gateway Port" value={dot.pick('dcv_broker.gateway_communication_port', this.state.vdcSettings)}/>
                                                    <KeyValue title="Client URL" value={`${getInternalALBUrl()}:${dot.pick('dcv_broker.client_communication_port', this.state.vdcSettings)}`} clipboard={true}/>
                                                    <KeyValue title="Agent URL" value={`${getInternalALBUrl()}:${dot.pick('dcv_broker.agent_communication_port', this.state.vdcSettings)}`} clipboard={true}/>
                                                    <KeyValue title="Gateway URL" value={`${getInternalALBUrl()}:${dot.pick('dcv_broker.gateway_communication_port', this.state.vdcSettings)}`} clipboard={true}/>
                                                </ColumnLayout>
                                            </Container>
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'Connection Gateway',
                                    id: 'connection-gateway',
                                    content: (
                                        <SpaceBetween size={'m'}>
                                            <Container header={<Header variant={"h2"}>Amazon DCV Connection Gateway</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={3}>
                                                    <KeyValue title="Base OS" value={Utils.getOsTitle(dot.pick('dcv_connection_gateway.autoscaling.base_os', this.state.vdcSettings))}/>
                                                    <KeyValue title="Instance Type" value={dot.pick('dcv_connection_gateway.autoscaling.instance_type', this.state.vdcSettings)}/>
                                                    <KeyValue title="Volume" value={{
                                                        value: dot.pick('dcv_connection_gateway.autoscaling.volume_size', this.state.vdcSettings),
                                                        unit: 'GB'
                                                    }} type={"memory"}/>
                                                </ColumnLayout>
                                            </Container>
                                            {
                                                buildAutoScalingSettingContainer(dot.pick('dcv_connection_gateway', this.state.vdcSettings))
                                            }
                                            <Container header={<Header variant={"h2"}>Certificate</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={isGatewayCertificateSelfSigned() ? 3 : 2}>
                                                    <KeyValue title="Type" value={isGatewayCertificateSelfSigned() ? 'Self-Signed' : 'Custom'}/>
                                                    {!isGatewayCertificateSelfSigned() && <KeyValue title="Domain" value={dot.pick('dcv_connection_gateway.certificate.custom_dns_name', this.state.vdcSettings)}/>}
                                                    <KeyValue title="Secret ARN" value={dot.pick('dcv_connection_gateway.certificate.certificate_secret_arn', this.state.vdcSettings)} clipboard={true}/>
                                                    <KeyValue title="Private Key Secret ARN" value={dot.pick('dcv_connection_gateway.certificate.private_key_secret_arn', this.state.vdcSettings)} clipboard={true}/>
                                                </ColumnLayout>
                                            </Container>
                                        </SpaceBetween>
                                    )
                                },
                                {
                                    label: 'Backup',
                                    id: 'backups',
                                    content: (
                                        <SpaceBetween size={"l"}>
                                            <Container header={<Header variant={"h2"}>AWS Backup</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="Cluster" value={<EnabledDisabledStatusIndicator enabled={isClusterBackupEnabled()}/>} type={"react-node"}/>
                                                    <KeyValue title="VDI Host" value={<EnabledDisabledStatusIndicator enabled={isVDIBackupEnabled()}/>} type={"react-node"}/>
                                                </ColumnLayout>
                                            </Container>
                                            {isBackupEnabled() && <Container header={<Header variant={"h2"}>Backup Plan</Header>}>
                                                <ColumnLayout variant={"text-grid"} columns={2}>
                                                    <KeyValue title="ARN" value={dot.pick('vdi_host_backup.backup_plan.arn', this.state.vdcSettings)} clipboard={true}/>
                                                    <KeyValue title="Selection" value={dot.pick('vdi_host_backup.backup_plan.selection.tags', this.state.vdcSettings)}/>
                                                </ColumnLayout>
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
                                                <KeyValue title="Status">
                                                    <EnabledDisabledStatusIndicator enabled={true}/>
                                                </KeyValue>
                                                <KeyValue title="Force Flush Interval" value={5} suffix={"seconds"}/>
                                                <KeyValue title="Log Retention" value={90} suffix={"days"}/>
                                            </ColumnLayout>
                                        </Container>
                                    )
                                }
                            ]}/>
                        <DefaultScheduleModal
                            ref={this.defaultScheduleModal}
                            onScheduleChange={this.updateDefaultSchedule}
                        />
                    </SpaceBetween>
                }/>
        )
    }
}

export default withRouter(VirtualDesktopSettings)
