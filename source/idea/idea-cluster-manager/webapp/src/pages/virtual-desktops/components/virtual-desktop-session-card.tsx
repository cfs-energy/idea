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
import {Badge, Box, Button, ButtonDropdown, CopyToClipboard, ExpandableSection, KeyValuePairs, Link, Popover, SpaceBetween, Spinner, StatusIndicator} from "@cloudscape-design/components";
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
import VirtualDesktopSessionStatusIndicator, {desktopStateLabel} from "./virtual-desktop-session-status-indicator";
import {ButtonDropdownProps} from "@cloudscape-design/components/button-dropdown";
import {desktopTerminalReason, shellStore, terminalEnabled} from "../../home/terminal/shell-store";
import {hasAccess} from "../../../navigation/task-navigation";

interface VirtualDesktopSessionCardProps {
    virtualDesktopClient: VirtualDesktopClient
    isSharedSession?: boolean
    isActiveDirectory: boolean
    session: VirtualDesktopSession
    projectAiAccessPending?: boolean
    idleAutoStopDelayMax?: number
    workingHours?: WorkingHours
    screenshot?: string
    previewFailed?: boolean
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
    blackScreenshot?: string
    // the slow request this card is waiting on: start or connect behind the primary button, or the client file
    pending?: 'primary' | 'download'
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

/** The reason as the desktop's owner reads it. The log location at its end is for administrators. */
export function reasonForOwner(reason?: string): string {
    return (reason ?? '').replace(/\s*The host startup log is kept in CloudWatch Logs:[\s\S]*$/, '')
}

export interface PrimaryAction {
    label: string
    action?: 'connect' | 'start' | 'info'
}

/** The one button a card leads with: what the user can do next, or what is happening right now. */
export function primaryAction(session: VirtualDesktopSession, canStart: boolean, infoShown: boolean): PrimaryAction {
    if (session.recovering) return {label: 'Restarting'}
    switch (session.state) {
        case 'READY':
            return {label: 'Connect', action: 'connect'}
        case 'STOPPED':
            return canStart ? {label: 'Start', action: 'start'} : {label: desktopStateLabel(session.state, session.hibernation_enabled)}
        case 'ERROR':
            return {label: infoShown ? 'Show preview' : 'Show info', action: 'info'}
    }
    return {label: session.state ? desktopStateLabel(session.state, session.hibernation_enabled) : 'Unavailable'}
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

    private unsubscribeShell?: () => void

    // Terminal is for the owner's Linux desktops; shared desktops never show it.
    showTerminal = () => !this.props.isSharedSession && terminalEnabled() && hasAccess(AppContext.get(), 'cluster')

    terminalReason = () => desktopTerminalReason(this.getSession(), shellStore.get())

    openTerminal = () => shellStore.open(`desktop:${this.getSession().idea_session_id}`, this.getSession().name || 'My desktop')

    componentDidMount() {
        if (!this.showTerminal()) return
        this.unsubscribeShell = shellStore.subscribe(() => this.forceUpdate())
        void shellStore.loadTargets(AppContext.get().client().terminal())
    }

    componentDidUpdate(previous: VirtualDesktopSessionCardProps) {
        // A desktop that just became ready may not have been a target when the list was read.
        if (this.showTerminal() && previous.session.state !== this.getSession().state && this.getSession().state === 'READY') {
            void shellStore.loadTargets(AppContext.get().client().terminal(), true)
        }
    }

    componentWillUnmount() {
        this.unsubscribeShell?.()
    }

    canConnect = (): boolean => this.props.session.state === 'READY' && !this.props.session.recovering

    canDownloadDcvSessionFile = () => this.canConnect()

    canUpdateSession = () => this.getSession().state === 'STOPPED'

    canUpdateSessionPermission = () => {
        if (this.getSession().base_os?.includes('windows') && !this.props.isActiveDirectory) {
            return false
        }
        return !this.isTerminating()
    }

    isTerminating = () => this.getSession().state === 'DELETING' || this.getSession().state === 'DELETED'

    connectDisabledReason = () => {
        if (this.isTerminating()) return 'This desktop is being terminated.'
        if (this.getSession().recovering) return 'Restarting. Try again in a few minutes.'
        return 'You can connect when the desktop is ready.'
    }

    sharingDisabledReason = () => this.isTerminating()
        ? 'This desktop is being terminated.'
        : 'Windows desktops can only be shared on a cluster that uses Active Directory.'

    updateDisabledReason = () => this.isTerminating()
        ? 'This desktop is being terminated.'
        : 'Stop the desktop to rename or resize it.'

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

    // A second click while the request is out does nothing. The flag is not state, which would only
    // update after the click that needs to see it.
    private requestOut = false

    private runOnce = (pending: 'primary' | 'download', request: () => Promise<boolean> | undefined) => {
        if (this.requestOut) return
        const result = request()
        if (!result) return
        this.requestOut = true
        this.setState({pending})
        result.finally(() => {
            this.requestOut = false
            this.setState({pending: undefined})
        })
    }

    connect = () => this.runOnce('primary', () => this.props.onLaunchSession(this.getSession()))

    start = () => this.runOnce('primary', () => this.props.onStartSession?.(this.getSession()))

    onPrimaryAction = (action: PrimaryAction['action']) => {
        if (action === 'connect') {
            this.connect()
        } else if (action === 'start') {
            this.start()
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
                {/* a long name wraps inside its own column; the buttons never move to a new line */}
                <div style={{minWidth: 0, overflowWrap: 'anywhere'}}>
                    <Box variant="h3">
                        {session.name}{this.props.isSharedSession && `: ${session.owner}`}
                    </Box>
                </div>
                <div style={{display: 'flex', alignItems: 'center', gap: '4px', flexShrink: 0, whiteSpace: 'nowrap'}}>
                    {/* always rendered while Terminal applies, so the card does not shift once access is known */}
                    {this.showTerminal() && <Button variant="icon" iconName="command-prompt"
                            ariaLabel={`Open a shell on ${session.name || 'My desktop'}`}
                            disabled={!!this.terminalReason()}
                            disabledReason={this.terminalReason()}
                            onClick={this.openTerminal}/>}
                    <Button
                        variant={primary.action === 'info' ? 'normal' : 'primary'}
                        disabled={primary.action === undefined}
                        loading={this.state.pending === 'primary'}
                        onClick={() => this.onPrimaryAction(primary.action)}>{primary.label}</Button>
                </div>
            </div>
            <SpaceBetween size="s" direction="horizontal" alignItems="center">
                <VirtualDesktopSessionStatusIndicator state={session.state!} hibernation_enabled={session.hibernation_enabled!} updated_on={session.updated_on} recovering={session.recovering}/>
                {details && <Box variant="small" color="text-body-secondary">{details}</Box>}
                {Utils.isNotEmpty(session.project?.title) && <Badge>{session.project?.title}</Badge>}
            </SpaceBetween>
            {this.hasSchedule() && this.buildScheduleLine()}
            {this.props.projectAiAccessPending &&
                <StatusIndicator type="warning">This desktop cannot use the project's AI models yet. {this.canStart()
                    ? 'Start it and it will come up with them.'
                    : 'It is moved onto them automatically, usually within 30 minutes.'}</StatusIndicator>}
            {/* the state alone is not enough to act on, and many failures come with no reason at all */}
            {session.state === 'ERROR' && <SpaceBetween size="xxs">
                <StatusIndicator type="error">This desktop failed. {this.props.isSharedSession
                    ? `Ask ${session.owner} to reboot it.`
                    : 'Reboot it, or terminate it and launch a new one. Your shared storage is not affected.'}</StatusIndicator>
                {Utils.isNotEmpty(reasonForOwner(session.failure_reason)) && <Box variant="small" color="text-body-secondary">{reasonForOwner(session.failure_reason)}</Box>}
            </SpaceBetween>}
        </SpaceBetween>
    }

    getScreenshotImageUrl(): string {
        // The page caches the last capture per session; only a running desktop's capture is current.
        if (!this.props.screenshot || !this.canConnect()) {
            return ''
        }
        return `data:image/jpeg;base64,${this.props.screenshot}`
    }

    buildScreenShotImage() {
        const imageUrl = this.getScreenshotImageUrl()
        if (imageUrl && this.state.blackScreenshot === imageUrl) {
            return <div className="virtual-desktop-placeholder-image" onClick={() => {
                if (this.canConnect()) {
                    this.connect()
                }
            }} style={{cursor: this.canConnect() ? 'pointer' : 'default'}}>
                <Box textAlign="center" padding="m" color="inherit">
                    <div>Desktop locked for security.</div>
                    <div>Preview unavailable. Connect to unlock.</div>
                </Box>
            </div>
        }
        if (Utils.isEmpty(imageUrl)) {
            const session = this.getSession()
            // In-between states get a spinner; settled states only text. Both stay centered and padded.
            const changing = ['CREATING', 'INITIALIZING', 'PROVISIONING', 'RESUMING', 'STOPPING', 'DELETING', 'DELETED'].includes(session.state ?? '')
            const settled: { [state: string]: string } = {
                READY: this.props.previewFailed ? 'Preview unavailable. You can still connect.' : 'Preview not captured yet',
                STOPPED: desktopStateLabel(session.state, session.hibernation_enabled)
            }
            const transition = session.recovering ? 'Restarting…' : changing ? `${desktopStateLabel(session.state, session.hibernation_enabled)}…` : undefined
            return <div className="virtual-desktop-placeholder-image" onClick={() => {
                if (this.canConnect()) {
                    this.connect()
                }
            }} style={{cursor: this.canConnect() ? 'pointer' : 'default'}}>
                <div className="virtual-desktop-placeholder-status">
                    {transition && <Spinner size="large"/>}
                    <div>{transition ?? settled[session.state ?? ''] ?? 'No preview'}</div>
                    {transition && <div>{session.recovering ? 'Try again in a few minutes.' : 'You can leave this page.'}</div>}
                </div>
            </div>
        }
        return <img alt="Desktop preview" src={imageUrl} onLoad={(event) => {
            try {
                const canvas = document.createElement('canvas')
                canvas.width = canvas.height = 32
                const context = canvas.getContext('2d')
                if (!context) return
                context.drawImage(event.currentTarget, 0, 0, 32, 32)
                const pixels = context.getImageData(0, 0, 32, 32).data
                // Allow JPEG noise, but require every sampled RGB channel to be near black.
                const black = pixels.length > 0 && pixels.every((value, index) => index % 4 === 3 || value <= 8)
                if (black && imageUrl === this.getScreenshotImageUrl()) {
                    this.setState({blackScreenshot: imageUrl})
                }
            } catch {
                // Canvas/decode failures must leave the preview and Connect action usable.
            }
        }} onClick={() => {
            if (this.canConnect()) {
                this.connect()
            }
        }} style={{
            cursor: 'pointer',
            objectFit: 'cover',
            display: 'block',
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
                <SpaceBetween size="m">
                    <KeyValuePairs columns={2} items={[
                        text('Project', session.project?.title),
                        text('State', session.recovering ? 'Restarting' : desktopStateLabel(session.state, session.hibernation_enabled)),
                        text('Operating system', Utils.getOsTitle(session.software_stack?.base_os)),
                        text('Size', session.server?.instance_type),
                        text('Created', created && !isNaN(created.getTime()) ? created.toLocaleString() : undefined)
                    ]}/>
                    <ExpandableSection headerText="Technical details">
                        <KeyValuePairs columns={2} items={[
                            copyable('Desktop ID', session.idea_session_id),
                            copyable('Remote session ID', session.dcv_session_id),
                            copyable('Instance ID', session.server?.instance_id),
                            copyable('Private IP', session.server?.private_ip),
                            text('Image ID', session.software_stack?.ami_id)
                        ]}/>
                    </ExpandableSection>
                </SpaceBetween>
            </Box>
        )
    }

    buildActionDropDownItems(): ButtonDropdownProps.ItemOrGroup[] {
        let dropDownItems: ButtonDropdownProps.ItemOrGroup[] = [
            {
                id: "connect",
                text: "Connect",
                disabled: !this.canConnect(),
                disabledReason: this.connectDisabledReason(),
                iconSvg: <FontAwesomeIcon icon={faDesktop} size="xs"/>
            }]

        if (this.showTerminal()) {
            dropDownItems.push({
                id: "terminal",
                text: "Terminal",
                disabled: !!this.terminalReason(),
                disabledReason: this.terminalReason(),
                iconName: "command-prompt"
            })
        }

        if (!this.props.isSharedSession) {
            dropDownItems.push(
                {
                    id: "session-permissions",
                    text: "Share desktop",
                    disabled: !this.canUpdateSessionPermission(),
                    disabledReason: this.sharingDisabledReason(),
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
                text: "Rename or resize",
                disabled: !this.canUpdateSession(),
                disabledReason: this.updateDisabledReason(),
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
                        loading={this.state.pending === 'download'}
                        onClick={() => this.runOnce('download', () => this.props.onDownloadDcvSessionFile(this.getSession()))}>
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
                            } else if (event.detail.id === 'terminal') {
                                this.openTerminal()
                            } else if (event.detail.id === 'connect') {
                                this.connect()
                            } else if (event.detail.id === 'stop') {
                                if (this.props.onStopSession) {
                                    this.props.onStopSession(this.getSession()).finally()
                                }
                            } else if (event.detail.id === 'start') {
                                this.start()
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
