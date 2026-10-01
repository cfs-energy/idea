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

import {VirtualDesktopClient} from "../../../client";
import {VirtualDesktopSchedule, VirtualDesktopSession, VirtualDesktopWeekSchedule} from "../../../client/data-model";
import React, {Component} from "react";
import Utils from "../../../common/utils";
import 'moment-timezone';
import moment from 'moment';
import {AppContext} from "../../../common";
import {Badge, Box, Button, ButtonDropdown, CopyToClipboard, KeyValuePairs, Link, Popover, SpaceBetween, StatusIndicator} from "@cloudscape-design/components";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import {
    faClock,
    faDesktop,
    faGear,
    faImage,
    faInfo,
    faPlay,
    faStop,
    faStopCircle,
    faTrash,
    faPowerOff,
    faShareFromSquare
} from "@fortawesome/free-solid-svg-icons";
import VirtualDesktopSessionStatusIndicator from "./virtual-desktop-session-status-indicator";
import {ButtonDropdownProps} from "@cloudscape-design/components/button-dropdown";

interface VirtualDesktopSessionCardProps {
    virtualDesktopClient: VirtualDesktopClient
    isSharedSession?: boolean
    isActiveDirectory: boolean
    session: VirtualDesktopSession
    projectAiAccessPending?: boolean
    idleAutoStopDelayMax?: number
    workingHours?: WorkingHours
    screenshot?: string
    onDeleteSession?: (session: VirtualDesktopSession) => Promise<boolean>
    onStartSession?: (session: VirtualDesktopSession) => Promise<boolean>
    onStopSession?: (session: VirtualDesktopSession) => Promise<boolean>
    onRebootSession?: (session: VirtualDesktopSession) => Promise<boolean>
    onDownloadDcvSessionFile: (session: VirtualDesktopSession) => Promise<boolean>
    onLaunchSession: (session: VirtualDesktopSession) => Promise<boolean>
    onUpdateSession?: (session: VirtualDesktopSession) => Promise<boolean>
    onUpdateSessionPermission?: (session: VirtualDesktopSession) => Promise<boolean>
    onConnectHelp: (session: VirtualDesktopSession) => Promise<boolean>
    onShowSchedule?: (session: VirtualDesktopSession) => Promise<boolean>
}

interface VirtualDesktopSessionCardState {
    view: string
}

const DAYS: (keyof VirtualDesktopWeekSchedule)[] = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday']

function todaySchedule(session: VirtualDesktopSession): VirtualDesktopSchedule | undefined {
    const day = moment().tz(AppContext.get().getClusterSettingsService().getClusterTimeZone()).day()
    return session.schedule?.[DAYS[day]]
}

export interface WorkingHours {
    start_up_time?: string
    shut_down_time?: string
}

/**
 * What today's schedule will do to the desktop, in plain words. A scheduled stop only happens once
 * the desktop is idle, so it is never described as a fixed stop time.
 */
/** the cluster's timezone abbreviation: schedules run on cluster time, not the viewer's */
function clusterZone(timezone?: string): string {
    return timezone ? ` ${moment.tz(timezone).format('z')}` : ''
}

export function describeSchedule(schedule: VirtualDesktopSchedule | undefined, idleMinutes: number, workingHours?: WorkingHours, timezone?: string): string {
    const whenIdle = idleMinutes > 0 ? `after ${idleMinutes} min idle` : 'when idle'
    switch (schedule?.schedule_type) {
        case 'START_ALL_DAY':
            return 'Always on'
        case 'STOP_ON_IDLE':
            return `Stops ${whenIdle}`
        case 'WORKING_HOURS':
        case 'CUSTOM_SCHEDULE': {
            const hours = schedule.schedule_type === 'WORKING_HOURS' && workingHours?.start_up_time ? workingHours : schedule
            if (!hours.start_up_time || !hours.shut_down_time) {
                return `Stops ${whenIdle} outside working hours`
            }
            return `Runs ${hours.start_up_time}–${hours.shut_down_time}${clusterZone(timezone)}, then stops ${whenIdle}`
        }
    }
    return 'No schedule today'
}

function describeDay(schedule: VirtualDesktopSchedule | undefined, workingHours?: WorkingHours): string {
    switch (schedule?.schedule_type) {
        case 'WORKING_HOURS':
            return workingHours?.start_up_time ? `Working hours (${workingHours.start_up_time}–${workingHours.shut_down_time})` : 'Working hours'
        case 'START_ALL_DAY':
            return 'Always on'
        case 'STOP_ON_IDLE':
            return 'Stops when idle'
        case 'CUSTOM_SCHEDULE':
            return `${schedule.start_up_time}–${schedule.shut_down_time}`
    }
    return 'No schedule'
}

export interface PrimaryAction {
    label: string
    action?: 'connect' | 'start' | 'info'
}

/** The one button a card leads with: what the user can do next, or what is happening right now. */
export function primaryAction(session: VirtualDesktopSession, canStart: boolean, infoShown: boolean): PrimaryAction {
    switch (session.state) {
        case 'READY':
            return {label: 'Connect', action: 'connect'}
        case 'STOPPED':
            return canStart ? {label: 'Start', action: 'start'} : {label: session.hibernation_enabled ? 'Hibernated' : 'Stopped'}
        case 'ERROR':
            return {label: infoShown ? 'Show preview' : 'Show info', action: 'info'}
        case 'PROVISIONING':
        case 'CREATING':
        case 'INITIALIZING':
            return {label: 'Setting up'}
        case 'RESUMING':
            return {label: 'Starting'}
        case 'STOPPING':
            return {label: session.hibernation_enabled ? 'Hibernating' : 'Stopping'}
        case 'DELETING':
        case 'DELETED':
            return {label: 'Terminating'}
    }
    return {label: 'Unavailable'}
}

class VirtualDesktopSessionCard extends Component<VirtualDesktopSessionCardProps, VirtualDesktopSessionCardState> {

    constructor(props: VirtualDesktopSessionCardProps) {
        super(props);
        this.state = {
            view: 'preview'
        }
    }

    getSession(): VirtualDesktopSession {
        return this.props.session
    }

    canConnect = (): boolean => this.props.session.state === 'READY'

    canDownloadDcvSessionFile = () => this.getSession().state === 'READY'

    canUpdateSession = () => this.getSession().state === 'STOPPED'

    canUpdateSessionPermission = () => {
        if (this.getSession().base_os?.includes('windows') && !this.props.isActiveDirectory) {
            return false
        }
        return !(this.getSession().state === 'DELETING' || this.getSession().state === 'DELETED')
    }

    canReboot = () => this.getSession().state === 'READY' || this.getSession().state === 'ERROR'

    canStop = () => this.getSession().state === 'READY' || this.getSession().state === 'STOPPING' || this.getSession().state === 'RESUMING'

    canDelete = () => {
        const status = this.getSession().state
        return !!status;
    }

    canStart = () => this.getSession().state === 'STOPPED'

    // the controller ignores the per-session override while the admin cap is not positive
    getIdleAutoStopOverride = (): number => {
        if (Utils.asNumber(this.props.idleAutoStopDelayMax, 0) <= 0) {
            return 0
        }
        return Math.min(Utils.asNumber(this.props.session.idle_autostop_delay, 0), Utils.asNumber(this.props.idleAutoStopDelayMax, 0))
    }

    hasSchedule = (): boolean => {
        const schedule = this.props.session.schedule
        return Utils.isNotEmpty(schedule?.monday)
            || Utils.isNotEmpty(schedule?.tuesday)
            || Utils.isNotEmpty(schedule?.wednesday)
            || Utils.isNotEmpty(schedule?.thursday)
            || Utils.isNotEmpty(schedule?.friday)
            || Utils.isNotEmpty(schedule?.saturday)
            || Utils.isNotEmpty(schedule?.sunday);
    }

    buildScheduleLine() {
        const session = this.props.session
        const days: [string, keyof VirtualDesktopWeekSchedule][] = [['Monday', 'monday'], ['Tuesday', 'tuesday'], ['Wednesday', 'wednesday'], ['Thursday', 'thursday'], ['Friday', 'friday'], ['Saturday', 'saturday'], ['Sunday', 'sunday']]
        return <Box variant="small" color="text-body-secondary">
            <Popover
                dismissAriaLabel="Close"
                header="Weekly schedule"
                content={
                    <KeyValuePairs columns={2} items={days.map(([label, key]) => ({label, value: describeDay(session.schedule?.[key], this.props.workingHours)}))}/>
                }
            >
                {describeSchedule(todaySchedule(session), this.getIdleAutoStopOverride(), this.props.workingHours, AppContext.get().getClusterSettingsService().getClusterTimeZone())}
            </Popover>
        </Box>
    }

    onPrimaryAction = (action: PrimaryAction['action']) => {
        if (action === 'connect') {
            this.props.onLaunchSession(this.getSession()).finally()
        } else if (action === 'start') {
            this.props.onStartSession?.(this.getSession()).finally()
        } else if (action === 'info') {
            this.toggleInfo()
        }
    }

    toggleInfo = () => this.setState({view: this.state.view === 'info' ? 'preview' : 'info'})

    buildHeader() {
        const session = this.props.session
        const primary = primaryAction(session, !this.props.isSharedSession && !!this.props.onStartSession, this.state.view === 'info')
        const details = [Utils.getOsTitle(session.software_stack?.base_os), session.server?.instance_type].filter(Utils.isNotEmpty).join(' · ')
        return <SpaceBetween size="xs" direction="vertical">
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px'}}>
                <Box variant="h3">
                    {session.name}{this.props.isSharedSession && `: ${session.owner}`}
                </Box>
                <Button
                    variant={primary.action === 'info' ? 'normal' : 'primary'}
                    disabled={primary.action === undefined}
                    onClick={() => this.onPrimaryAction(primary.action)}>{primary.label}</Button>
            </div>
            <SpaceBetween size="s" direction="horizontal" alignItems="center">
                <VirtualDesktopSessionStatusIndicator state={session.state!} hibernation_enabled={session.hibernation_enabled!} updated_on={session.updated_on}/>
                {details && <Box variant="small" color="text-body-secondary">{details}</Box>}
                {Utils.isNotEmpty(session.project?.title) && <Badge>{session.project?.title}</Badge>}
            </SpaceBetween>
            {this.hasSchedule() && this.buildScheduleLine()}
            {this.props.projectAiAccessPending &&
                <StatusIndicator type="warning">This desktop cannot use the project's AI models yet. {this.canStart()
                    ? 'Start it and it will come up with them.'
                    : 'It is moved onto them automatically, usually within 30 minutes.'}</StatusIndicator>}
            {/* the state badge only says "Error", which is not enough to act on */}
            {session.state === 'ERROR' && Utils.isNotEmpty(session.failure_reason) &&
                <StatusIndicator type="error">{session.failure_reason}</StatusIndicator>}
        </SpaceBetween>
    }

    getScreenshotImageUrl(): string {
        if (!this.props.screenshot) {
            return ''
        }
        return `data:image/jpeg;base64,${this.props.screenshot}`
    }

    buildScreenShotImage() {
        const imageUrl = this.getScreenshotImageUrl()
        if (Utils.isEmpty(imageUrl)) {
            const session = this.getSession()
            const placeholders: { [state: string]: string } = {
                CREATING: 'Creating your desktop',
                INITIALIZING: 'Initializing your desktop',
                PROVISIONING: 'Provisioning your desktop',
                RESUMING: 'Starting',
                READY: 'Preview not captured yet',
                STOPPING: session.hibernation_enabled ? 'Hibernating' : 'Stopping',
                STOPPED: session.hibernation_enabled ? 'Hibernated' : 'Stopped',
                DELETING: 'Terminating',
                DELETED: 'Terminating'
            }
            return <div className="virtual-desktop-placeholder-image">
                {placeholders[session.state ?? ''] ?? 'No preview'}
            </div>
        }
        return <div onClick={() => {
            if (this.canConnect()) {
                this.props.onLaunchSession(this.getSession()).finally()
            }
        }} style={{
            cursor: 'pointer',
            backgroundImage: `url('${imageUrl}')`,
            backgroundSize: 'cover',
            width: '100%',
            height: '300px'
        }}/>
    }

    buildSessionInfo() {
        const session = this.getSession()
        const copyable = (label: string, value?: string) => ({
            label,
            value: Utils.isEmpty(value) ? '-' : <CopyToClipboard variant="inline" textToCopy={value!} copyButtonAriaLabel={`Copy ${label}`} copySuccessText={`${label} copied`} copyErrorText={`Could not copy ${label}`}/>
        })
        const text = (label: string, value?: string) => ({label, value: Utils.isEmpty(value) ? '-' : value})
        const created = session.created_on ? new Date(session.created_on) : undefined
        return (
            <Box padding={{vertical: 'm'}}>
                <KeyValuePairs columns={2} items={[
                    copyable('Desktop ID', session.idea_session_id),
                    copyable('DCV session ID', session.dcv_session_id),
                    text('Project', session.project?.title),
                    text('State', session.state),
                    text('Operating system', Utils.getOsTitle(session.software_stack?.base_os)),
                    text('Instance type', session.server?.instance_type),
                    copyable('Instance ID', session.server?.instance_id),
                    copyable('Private IP', session.server?.private_ip),
                    text('AMI ID', session.software_stack?.ami_id),
                    text('Tenancy', session.software_stack?.launch_tenancy),
                    text('Created', created && !isNaN(created.getTime()) ? created.toLocaleString() : undefined)
                ]}/>
            </Box>
        )
    }

    buildActionDropDownItems(): ButtonDropdownProps.ItemOrGroup[] {
        let dropDownItems: ButtonDropdownProps.ItemOrGroup[] = [
            {
                id: "connect",
                text: "Connect",
                disabled: !this.canConnect(),
                iconSvg: <FontAwesomeIcon icon={faDesktop} size="xs"/>
            }]

        if (!this.props.isSharedSession) {
            dropDownItems.push(
                {
                    id: "session-permissions",
                    text: "Session permissions",
                    disabled: !this.canUpdateSessionPermission(),
                    disabledReason: "Windows sessions support session sharing for active directory only",
                    iconSvg: <FontAwesomeIcon icon={faShareFromSquare} size="xs"/>
                })
        }

        dropDownItems.push({
            id: "toggle-info",
            text: (this.state.view === 'info') ? 'Show preview' : 'Show info',
            iconSvg: <FontAwesomeIcon icon={(this.state.view === 'info') ? faImage : faInfo} size="xs"/>
        })

        if (!this.props.isSharedSession) {
            dropDownItems.push({
                id: "schedule",
                text: 'Schedule',
                iconSvg: <FontAwesomeIcon icon={faClock} size="xs"/>
            })
            dropDownItems.push({
                id: "update-session",
                text: "Update session",
                disabled: !this.canUpdateSession(),
                disabledReason: "Stop the desktop to update it.",
                iconSvg: <FontAwesomeIcon icon={faGear} size="xs"/>
            })
            dropDownItems.push({
                id: "states",
                text: "Desktop state",
                items: [
                    {
                        id: "start",
                        text: "Start",
                        disabled: !this.canStart(),
                        iconSvg: <FontAwesomeIcon icon={faPlay} size="xs"/>
                    },
                    {
                        id: "stop",
                        text: this.getSession().state === 'STOPPING' ? 'Force stop' : (this.getSession().hibernation_enabled) ? 'Hibernate' : 'Stop',
                        disabled: !this.canStop(),
                        iconSvg: (this.getSession().hibernation_enabled) ? <FontAwesomeIcon icon={faStopCircle} size="xs"/> : <FontAwesomeIcon icon={faStop} size="xs"/>
                    },
                    {
                        id: "reboot",
                        text: "Reboot",
                        disabled: !this.canReboot(),
                        iconSvg: <FontAwesomeIcon icon={faPowerOff} size="xs"/>
                    },
                    {
                        id: "terminate",
                        text: "Terminate",
                        disabled: !this.canDelete(),
                        iconSvg: <FontAwesomeIcon icon={faTrash} size="xs"/>
                    }
                ]
            })
        }
        return dropDownItems
    }

    buildActions() {

        return <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px'}}>
            <SpaceBetween size="xs" direction="horizontal" alignItems="center">
                <Button iconName="download"
                        disabled={!this.canDownloadDcvSessionFile()}
                        onClick={() => this.props.onDownloadDcvSessionFile(this.getSession()).finally()}>
                    DCV client file</Button>
                <Popover
                    dismissAriaLabel="Close"
                    header="DCV client file"
                    content={<SpaceBetween size="xs">
                        <Box>Opens this desktop in the Amazon DCV client app instead of the browser. Download the file, then open it with the client.</Box>
                        <Link onFollow={() => this.props.onConnectHelp(this.getSession())}>Get the client and setup steps</Link>
                    </SpaceBetween>}
                >
                    <Link variant="info" ariaLabel="About the DCV client file">Info</Link>
                </Popover>
            </SpaceBetween>
            <Box>
                    <ButtonDropdown
                        onItemClick={(event) => {
                            if (event.detail.id === 'terminate') {
                                if (this.props.onDeleteSession) {
                                    this.props.onDeleteSession(this.getSession()).finally()
                                }
                            } else if (event.detail.id === 'connect') {
                                this.props.onLaunchSession(this.getSession()).finally()
                            } else if (event.detail.id === 'stop') {
                                if (this.props.onStopSession) {
                                    this.props.onStopSession(this.getSession()).finally()
                                }
                            } else if (event.detail.id === 'start') {
                                if (this.props.onStartSession) {
                                    this.props.onStartSession(this.getSession()).finally()
                                }
                            } else if (event.detail.id === 'reboot') {
                                if (this.props.onRebootSession) {
                                    this.props.onRebootSession(this.getSession()).finally()
                                }
                            } else if (event.detail.id === 'toggle-info') {
                                this.toggleInfo()
                            } else if (event.detail.id === 'schedule') {
                                if (this.props.onShowSchedule) {
                                    this.props.onShowSchedule(this.getSession()).finally()
                                }
                            } else if (event.detail.id === 'update-session') {
                                if (this.props.onUpdateSession) {
                                    this.props.onUpdateSession(this.getSession()).finally()
                                }
                            } else if (event.detail.id === 'session-permissions') {
                                if (this.props.onUpdateSessionPermission) {
                                    this.props.onUpdateSessionPermission(this.getSession()).finally()
                                }
                            }
                        }}
                        items={this.buildActionDropDownItems()}
                        expandableGroups
                    >
                        Actions
                    </ButtonDropdown>
            </Box>
        </div>
    }

    render() {
        return <SpaceBetween direction="vertical" size="xs">
            {this.buildHeader()}
            {this.state.view === 'preview' && this.buildScreenShotImage()}
            {this.state.view === 'info' && this.buildSessionInfo()}
            {this.buildActions()}
        </SpaceBetween>
    }
}

export default VirtualDesktopSessionCard
