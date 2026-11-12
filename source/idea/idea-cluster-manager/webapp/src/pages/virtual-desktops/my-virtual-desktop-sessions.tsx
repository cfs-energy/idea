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

import {
    Alert,
    Box,
    Button,
    Cards,
    SegmentedControl,
    SpaceBetween,
} from "@cloudscape-design/components";
import {AppContext} from "../../common";
import {
    Project,
    SocaUserInputChoice, VirtualDesktopBaseOS,
    VirtualDesktopSession, VirtualDesktopSessionPermission, VirtualDesktopSessionScreenshot,
    VirtualDesktopSoftwareStack
} from '../../client/data-model'
import {ProjectsClient, VirtualDesktopClient} from '../../client'
import IdeaForm from "../../components/form";
import Utils from "../../common/utils";
import IdeaConfirm from "../../components/modals";
import {withRouter} from "../../navigation/navigation-utils";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import 'moment-timezone';
import VirtualDesktopScheduleModal from "./components/virtual-desktop-schedule-modal";
import VirtualDesktopSessionCard from "./components/virtual-desktop-session-card";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {DcvClientHelpModal} from "./components/dcv-client-help-modal";
import moment from "moment";
import UpdateSessionPermissionModal from "./forms/virtual-desktop-update-session-permissions-form";
import dot from "dot-object";
import VirtualDesktopCreateSessionForm from "./forms/virtual-desktop-create-session-form";
import VirtualDesktopUtilsClient from "../../client/virtual-desktop-utils-client";
import {IdeaFormValidate} from "../../components/form-field/form-field";
import {downloadDcvFile, getConnectionInfo, openInNewTab, POPUP_BLOCKED_MESSAGE} from "./desktop-connection";


const OS_FILTER_LINUX_ID = 'linux'
const OS_FILTER_WINDOWS_ID = 'windows'
const OS_FILTER_ALL_ID = '$all'

/** The row a list filter shows. The listing can hold rows from another filter: see mergeSessions. */
export function matchesOsFilter(session: VirtualDesktopSession, osFilter: string): boolean {
    if (osFilter === OS_FILTER_ALL_ID) {
        return true
    }
    const isWindows = (session.base_os ?? session.software_stack?.base_os ?? '').includes('windows')
    return osFilter === OS_FILTER_WINDOWS_ID ? isWindows : !isWindows
}

export interface MyVirtualDesktopSessionsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface MyVirtualDesktopConfirmModalActionProps {
    actionTitle: string
    onConfirm: () => void
    actionText: string | React.ReactNode
    onCancel: () => void
}

interface ScreenShotState {
    data: string
    time: moment.Moment
}

export interface MyVirtualDesktopSessionsState {
    selectedItems: any[]
    directoryServiceProvider: string
    sessions: Map<string, VirtualDesktopSession>
    loading: boolean
    screenshots: { [k: string]: ScreenShotState }
    // desktops whose last preview request came back with nothing, by remote session id
    previewFailed: { [k: string]: boolean }
    selectedSession: VirtualDesktopSession | undefined
    selectedSessionApplicableInstanceTypes: SocaUserInputChoice[]
    osFilter: string,
    // the last listing request failed: the rows on screen, if any, are the last known state
    listError: boolean
    showDcvClientHelpModal: boolean
    showCreateSessionForm: boolean
    showUpdateSessionForm: boolean
    showUpdateSessionPermissionForm: boolean
    confirmAction: MyVirtualDesktopConfirmModalActionProps
    activeConnectionConfirmModalOnConfirm: () => void
    userProjects: Project[]
    userProjectsFailed: boolean
    softwareStacks: { [k: string]: VirtualDesktopSoftwareStack }
}

// the listing is read from the search index, which trails the session table by a few seconds
const INDEX_LAG_GRACE_MS = 2 * 60 * 1000

/**
 * The next table contents. A complete listing replaces the table, a partial one updates the rows it
 * names; in both, a local copy newer than the server's copy is kept. Because the listing trails the
 * session table, a complete listing keeps a session it does not list yet if that session changed
 * within the grace window (a desktop just created), and drops one the session table already
 * deleted even while the listing still returns it.
 */
const QUICK_REFRESH_MS = 5000
const FIRST_PREVIEW_WINDOW_MS = 60 * 1000
const CHANGING_STATES = new Set(['CREATING', 'PROVISIONING', 'INITIALIZING', 'RESUMING', 'STOPPING'])

/**
 * True while a desktop is changing state, or is ready without a first preview for up to a minute.
 * `readySince` remembers when each preview-less ready desktop was first seen.
 */
export function needsQuickRefresh(sessions: Map<string, VirtualDesktopSession>, screenshots: { [k: string]: unknown }, readySince: { [k: string]: number }, now: number = Date.now()): boolean {
    let quick = false
    sessions.forEach((session) => {
        const id = session.dcv_session_id
        if (CHANGING_STATES.has(session.state ?? '')) {
            quick = true
        } else if (session.state === 'READY' && id && screenshots[id] === undefined) {
            readySince[id] = readySince[id] ?? now
            quick = quick || now - readySince[id] < FIRST_PREVIEW_WINDOW_MS
        }
    })
    return quick
}

export function mergeSessions(current: Map<string, VirtualDesktopSession>, incoming: VirtualDesktopSession[], complete: boolean, now: number = Date.now()): Map<string, VirtualDesktopSession> {
    const next = new Map<string, VirtualDesktopSession>(complete ? [] : current.entries())
    incoming.forEach(session => {
        const id = session.idea_session_id!
        const local = current.get(id)
        const localIsNewer = local !== undefined && local.updated_on !== undefined && session.updated_on !== undefined && local.updated_on > session.updated_on
        if (complete && localIsNewer && local.state === 'DELETED') {
            return
        }
        next.set(id, localIsNewer ? local : session)
    })
    if (complete) {
        current.forEach((local, id) => {
            const changed = Date.parse(local.updated_on ?? local.created_on ?? '')
            const leaving = local.state === 'DELETING' || local.state === 'DELETED'
            if (!next.has(id) && !leaving && now - changed < INDEX_LAG_GRACE_MS) {
                next.set(id, local)
            }
        })
    }
    return next
}

class MyVirtualDesktopSessions extends Component<MyVirtualDesktopSessionsProps, MyVirtualDesktopSessionsState> {

    createSessionForm: RefObject<VirtualDesktopCreateSessionForm | null>
    updateSessionForm: RefObject<IdeaForm | null>
    updateSessionPermissionForm: RefObject<UpdateSessionPermissionModal | null>
    sessionActionConfirmModal: RefObject<IdeaConfirm | null>
    activeConnectionConfirmModal: RefObject<IdeaConfirm | null>
    scheduleModal: RefObject<VirtualDesktopScheduleModal | null>
    refreshInterval: any
    quickRefresh: any
    readySince: { [k: string]: number } = {}
    listRequest = 0
    virtualDesktopSettings: any
    componentMounted: boolean

    constructor(props: MyVirtualDesktopSessionsProps) {
        super(props);
        this.createSessionForm = React.createRef()
        this.updateSessionForm = React.createRef()
        this.updateSessionPermissionForm = React.createRef()
        this.sessionActionConfirmModal = React.createRef()
        this.activeConnectionConfirmModal = React.createRef()
        this.scheduleModal = React.createRef()
        this.componentMounted = true
        this.virtualDesktopSettings = undefined

        this.state = {
            selectedItems: [],
            directoryServiceProvider: '',
            sessions: new Map<string, VirtualDesktopSession>(),
            loading: true,
            screenshots: {},
            previewFailed: {},
            selectedSession: undefined,
            selectedSessionApplicableInstanceTypes: [],
            osFilter: OS_FILTER_ALL_ID,
            listError: false,
            showDcvClientHelpModal: false,
            showCreateSessionForm: false,
            showUpdateSessionForm: false,
            showUpdateSessionPermissionForm: false,
            softwareStacks: {},
            userProjects: [],
            userProjectsFailed: false,
            confirmAction: {
                actionTitle: '',
                actionText: '',
                onConfirm: () => {
                },
                onCancel: () => {
                }
            },
            activeConnectionConfirmModalOnConfirm: () => {
            }
        }
    }

    componentDidMount() {
        this.componentMounted = true

        AppContext.get().getClusterSettingsService().getDirectoryServiceSettings().then(settings => {
            this.setState({
                directoryServiceProvider: dot.pick('provider', settings)
            })
        })

        AppContext.get().getClusterSettingsService().getVirtualDesktopSettings().then(settings => {
            this.virtualDesktopSettings = settings
        })

        this.loadUserProjects().finally()

        const refresh = () => {
            if (!this.componentMounted) {
                return Promise.resolve(true)
            }

            return this.fetchSessions().then(() => {
                this.dropStalePreviews()
                this.fetchSessionScreenshots().finally(() => this.scheduleQuickRefresh(refresh))
            })
        }

        refresh().then(() => this.setState({
            loading: false
        }))
        this.refreshInterval = setInterval(refresh, 30000)
    }

    loadUserProjects(): Promise<void> {
        return this.getProjectsClient().getUserProjects({
            username: AppContext.get().auth().getUsername()
        }).then(result => {
            this.setState({
                userProjects: result.projects!,
                userProjectsFailed: false
            })
        }).catch(() => {
            this.setState({
                userProjectsFailed: true
            })
        })
    }

    componentWillUnmount() {
        this.componentMounted = false
        clearInterval(this.refreshInterval)
        clearTimeout(this.quickRefresh)
    }

    // A desktop that left READY gets a fresh preview when it comes back, never its old screen.
    dropStalePreviews() {
        const screenshots = this.state.screenshots
        let changed = false
        this.state.sessions.forEach((session) => {
            const id = session.dcv_session_id
            if (id && session.state !== 'READY' && screenshots[id] !== undefined) {
                delete screenshots[id]
                delete this.readySince[id]
                changed = true
            }
        })
        if (changed) {
            this.setState({screenshots})
        }
    }

    scheduleQuickRefresh(refresh: () => Promise<unknown>) {
        if (this.quickRefresh || !this.componentMounted || !needsQuickRefresh(this.state.sessions, this.state.screenshots, this.readySince)) {
            return
        }
        this.quickRefresh = setTimeout(() => {
            this.quickRefresh = undefined
            refresh().finally()
        }, QUICK_REFRESH_MS)
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getVirtualDesktopUtilsClient(): VirtualDesktopUtilsClient {
        return AppContext.get().client().virtualDesktopUtils()
    }

    getVirtualDesktopClient(): VirtualDesktopClient {
        return AppContext.get().client().virtualDesktop()
    }

    getUpdateSessionForm(): IdeaForm {
        return this.updateSessionForm.current!
    }

    getCreateSessionForm(): VirtualDesktopCreateSessionForm {
        return this.createSessionForm.current!
    }

    getUpdateSessionPermissionForm(): UpdateSessionPermissionModal {
        return this.updateSessionPermissionForm.current!
    }

    getSessionActionConfirmModal(): IdeaConfirm {
        return this.sessionActionConfirmModal.current!
    }

    getActiveConnectionConfirmModal(): IdeaConfirm {
        return this.activeConnectionConfirmModal.current!
    }

    getScheduleModal(): VirtualDesktopScheduleModal {
        return this.scheduleModal.current!
    }

    // a desktop launched before its project's instance profile was provisioned runs under the
    // shared one, so its models aren't reachable; an unrecorded profile is not treated as a match.
    isProjectAiAccessPending = (session: VirtualDesktopSession): boolean => {
        const project = this.state.userProjects?.find((candidate) => candidate.project_id === session.project?.project_id)
        const projectInstanceProfileArn = project?.bedrock?.instance_profile_arn
        if (!project?.bedrock?.enabled || Utils.isEmpty(projectInstanceProfileArn)) {
            return false
        }
        return session.server?.instance_profile_arn !== projectInstanceProfileArn
    }

    fetchSessions(): Promise<boolean> {
        const request = ++this.listRequest
        return AppContext.get().client().virtualDesktop().listSessions({
            filters: [{
                key: 'base_os',
                value: this.state.osFilter
            }],
            paginator: {
                page_size: 100
            }
        }).then(result => {
            // an answer for an older filter or an earlier poll must not replace a newer one
            if (request !== this.listRequest) {
                return false
            }
            this.setState({listError: false})
            return this.setSessions(result?.listing, true)
        }).catch(() => {
            if (request === this.listRequest) {
                this.setState({listError: true})
            }
            return false
        })
    }

    retryList = () => {
        this.setState({loading: true}, () => {
            this.fetchSessions().finally(() => this.setState({loading: false}))
        })
    }

    async fetchSessionScreenshots() {
        const sessions = this.state.sessions
        if (sessions.size === 0) {
            return
        }

        let screenshots: VirtualDesktopSessionScreenshot[] = []
        sessions.forEach((session) => {
            if (Utils.isEmpty(session.dcv_session_id) || session.dcv_session_id === undefined) {
                return true
            } else if (session.state !== 'READY') {
                // no screenshots required unless the session is ready.
                return true
            } else if (this.state.screenshots[session.dcv_session_id] !== undefined && moment().subtract(5, 'minutes').isBefore(this.state.screenshots[session.dcv_session_id].time)) {
                // do not ask for screenshots if we already have a screenshot that is less than 5 minutes old
                return true
            }

            screenshots.push({
                idea_session_owner: AppContext.get().auth().getUsername(),
                idea_session_id: session.idea_session_id,
                dcv_session_id: session.dcv_session_id,
                create_time: session.created_on
            })
        })

        if (Utils.isEmpty(screenshots)) {
            // not calling the screenshots API when we know that there are no sessions that are active.
            return
        }

        screenshots = screenshots.sort((a: VirtualDesktopSessionScreenshot, b: VirtualDesktopSessionScreenshot) => {
            if (a === undefined && b === undefined) {
                return 0
            }

            if (a === undefined || a.dcv_session_id === undefined) {
                return 1
            }

            if (b === undefined || b.dcv_session_id === undefined) {
                return -1
            }

            if (this.state.screenshots[a.dcv_session_id] === undefined && this.state.screenshots[b.dcv_session_id] === undefined) {
                return moment(a.create_time).diff(b.create_time)
            }

            if (this.state.screenshots[a.dcv_session_id] === undefined) {
                return 1
            }

            if (this.state.screenshots[a.dcv_session_id] === undefined) {
                return -1
            }

            return this.state.screenshots[a.dcv_session_id].time.diff(this.state.screenshots[b.dcv_session_id].time)
        })

        const delay = async (interval: number) => {
            return new Promise((resolve) => {
                setTimeout(() => resolve(true), interval)
            })
        }

        const batchSize = 3
        const screenshotState = this.state.screenshots
        for (let i = 0; i < screenshots.length; i += batchSize) {
            if (!this.componentMounted) {
                // No need to go fetching for screenshots if the component is not mounted.
                continue
            }
            const batch = screenshots.slice(i, i + batchSize)
            // one failed batch must not cost the desktops in the next one their preview
            const result = await this.getVirtualDesktopClient().getSessionScreenshot({
                screenshots: batch
            }).catch(() => undefined)
            result?.success?.forEach((screenshot => {
                screenshotState[screenshot.dcv_session_id!] = {
                    data: screenshot.image_data!,
                    time: moment()
                }
            }))
            const previewFailed = {...this.state.previewFailed}
            batch.forEach(({dcv_session_id}) => {
                if (result?.success?.some(screenshot => screenshot.dcv_session_id === dcv_session_id)) {
                    delete previewFailed[dcv_session_id!]
                } else {
                    previewFailed[dcv_session_id!] = true
                }
            })
            this.setState({
                screenshots: screenshotState,
                previewFailed
            })
            if (i + batchSize < screenshots.length) {
                await delay(1000)
            }
        }
    }

    /**
     * `complete` means `sessions` is the server's whole listing: anything it no longer lists is gone
     * (terminated, or filtered out) and leaves the table. An action result is a partial update and
     * only replaces the sessions it names.
     */
    setSessions = (sessions: VirtualDesktopSession[] | undefined, complete: boolean = false): Promise<boolean> => {
        if (sessions === undefined) {
            return new Promise((resolve) => {
                resolve(true)
            })
        }
        return new Promise<boolean>((resolve) => {
            this.setState({
                sessions: mergeSessions(this.state.sessions, sessions, complete)
            }, () => {
                resolve(true)
            })
        })
    }

    setSession = (session: VirtualDesktopSession): Promise<boolean> => {
        return this.setSessions([session])
    }

    setFlashMessage = (content: React.ReactNode, type: 'success' | 'info' | 'error') => {
        this.props.onFlashbarChange({
            items: [
                {
                    content: content,
                    dismissible: true,
                    type: type
                }
            ]
        })
    }

    buildSessionFailureMessage(session: VirtualDesktopSession, action: string): string {
        const name = Utils.isNotEmpty(session.name) ? `virtual desktop ${session.name}` : 'this desktop'
        if (Utils.isNotEmpty(session.failure_reason)) {
            return `Could not ${action} ${name}: ${session.failure_reason}`
        }
        return `Could not ${action} ${name}. No reason was returned, contact your administrator.`
    }

    // session actions answer with a successful envelope and report per-session failures in
    // result.failed. nothing reaches the user unless that list is read here.
    displayFailedSessions = (failed: VirtualDesktopSession[] | undefined, action: string) => {
        if (!failed || failed.length === 0) {
            return
        }
        this.setFlashMessage(
            <div>
                {failed.map((session, index) => <p key={index}>{this.buildSessionFailureMessage(session, action)}</p>)}
            </div>,
            'error'
        )
    }

    // IdeaException.message is optional, so fall back to a next step rather than an empty banner or a bare code.
    displayActionError = (error: any, action: string) => {
        const message = Utils.isNotEmpty(error?.message) ? error.message : 'Try again, or ask your administrator.'
        this.setFlashMessage(`Could not ${action} virtual desktop: ${message}`, 'error')
    }

    // Open connections are the one refusal a person can override: they confirm, and it is sent again
    // with force. Anything else is reported as it is, because force would skip the check that refused it.
    confirmOrReportFailed = (failed: VirtualDesktopSession[] | undefined, action: string, force: (session: VirtualDesktopSession) => Promise<{ success?: VirtualDesktopSession[], failed?: VirtualDesktopSession[] }>) => {
        const connected = (failed ?? []).filter(entry => (entry.connection_count ?? 0) > 0)
        this.displayFailedSessions((failed ?? []).filter(entry => !connected.includes(entry)), action)
        connected.forEach(entry => {
            this.setState({
                selectedSession: entry,
                activeConnectionConfirmModalOnConfirm: () => {
                    force(entry).then(result => {
                        this.displayFailedSessions(result.failed, action)
                        this.setSessions(result.success).finally()
                    }).catch(error => {
                        this.displayActionError(error, action)
                    })
                }
            }, () => {
                this.getActiveConnectionConfirmModal().show()
            })
        })
    }

    onDeleteSession = (session: VirtualDesktopSession): Promise<boolean> => {
        return new Promise((resolve) => {
            this.setState({
                selectedSession: session,
                confirmAction: {
                    actionTitle: "Terminate Virtual Desktop",
                    actionText: (
                        <SpaceBetween size="s">
                            <Box>Are you sure you want to terminate virtual desktop: {session.name}?</Box>
                            <Box>This will <strong>permanently delete</strong> the instance and its local data.</Box>
                            <Box><strong>Note:</strong> Shared storage filesystems will <strong>not</strong> be affected.</Box>
                        </SpaceBetween>
                    ),
                    onConfirm: () => {
                        this.getVirtualDesktopClient().deleteSessions({
                            sessions: [
                                {
                                    idea_session_id: this.state.selectedSession?.idea_session_id,
                                    dcv_session_id: this.state.selectedSession?.dcv_session_id
                                }
                            ]
                        }).then(result => {
                            this.confirmOrReportFailed(result.failed, 'terminate', (entry) => this.getVirtualDesktopClient().deleteSessions({
                                sessions: [{
                                    idea_session_id: entry.idea_session_id,
                                    dcv_session_id: entry.dcv_session_id,
                                    force: true
                                }]
                            }))
                            this.setSessions(result.success).finally()
                        }).catch(error => {
                            this.displayActionError(error, 'terminate')
                        })
                    },
                    onCancel: () => {
                        this.setState({
                            selectedSession: undefined
                        })
                    }
                }
            }, () => {
                this.getSessionActionConfirmModal().show()
                resolve(true)
            })
        })
    }

    onRebootSession = (session: VirtualDesktopSession): Promise<boolean> => {
        return new Promise<boolean>((resolve) => {
            this.setState({
                selectedSession: session,
                confirmAction: {
                    actionTitle: "Reboot Virtual Desktop",
                    actionText: "Are you sure you want to reboot virtual desktop: " + session.name + "?",
                    onConfirm: () => {
                        this.getVirtualDesktopClient().rebootSessions({
                            sessions: [
                                {
                                    idea_session_id: this.state.selectedSession?.idea_session_id,
                                    dcv_session_id: this.state.selectedSession?.dcv_session_id
                                }
                            ]
                        }).then(result => {
                            this.confirmOrReportFailed(result.failed, 'reboot', (entry) => this.getVirtualDesktopClient().rebootSessions({
                                sessions: [{
                                    idea_session_id: entry.idea_session_id,
                                    dcv_session_id: entry.dcv_session_id,
                                    force: true
                                }]
                            }))
                            this.setSessions(result.success).finally()
                        }).catch(error => {
                            this.displayActionError(error, 'reboot')
                        })
                    },
                    onCancel: () => {
                        this.setState({
                            selectedSession: undefined
                        })
                    }
                }
            }, () => {
                this.getSessionActionConfirmModal().show()
                resolve(true)
            })
        })
    }

    onStopSession = (session: VirtualDesktopSession): Promise<boolean> => {
        let actionTitle = "Stop Virtual Desktop"
        let actionText = "Are you sure you want to stop virtual desktop: " + session.name + "?"
        let isForceStop = session.state === 'STOPPING'

        if (isForceStop) {
            actionTitle = "Force Stop Virtual Desktop"
            actionText = "Are you sure you want to force stop virtual desktop: " + session.name + "? This will forcefully terminate the session."
        } else if (session.hibernation_enabled) {
            actionTitle = "Hibernate Virtual Desktop"
            actionText = "Are you sure you want to hibernate virtual desktop: " + session.name + "?"
        }

        return new Promise((resolve) => {
            this.setState({
                selectedSession: session,
                confirmAction: {
                    actionTitle: actionTitle,
                    actionText: actionText,
                    onConfirm: () => {
                        this.getVirtualDesktopClient().stopSessions({
                            sessions: [
                                {
                                    idea_session_id: this.state.selectedSession?.idea_session_id,
                                    dcv_session_id: this.state.selectedSession?.dcv_session_id,
                                    force: isForceStop
                                }
                            ]
                        }).then(result => {
                            this.confirmOrReportFailed(result.failed, 'stop', (entry) => this.getVirtualDesktopClient().stopSessions({
                                sessions: [{
                                    idea_session_id: entry.idea_session_id,
                                    dcv_session_id: entry.dcv_session_id,
                                    force: true
                                }]
                            }))
                            this.setSessions(result.success).finally()
                        }).catch(error => {
                            this.displayActionError(error, 'stop')
                        })
                    },
                    onCancel: () => {
                        this.setState({
                            selectedSession: undefined
                        })
                    }
                }
            }, () => {
                this.getSessionActionConfirmModal().show()
                resolve(true)
            })
        })
    }

    onStartSession = (session: VirtualDesktopSession): Promise<boolean> => {
        return this.getVirtualDesktopClient().resumeSessions({
            sessions: [{
                idea_session_id: session.idea_session_id,
                dcv_session_id: session.dcv_session_id
            }]
        }).then((result) => {
            this.displayFailedSessions(result.failed, 'start')
            return this.setSessions(result.success)
        }).catch(error => {
            this.displayActionError(error, 'start')
            return false
        })
    }

    onDownloadDcvSessionFile = (session: VirtualDesktopSession): Promise<boolean> => {
        if (session.recovering) {
            this.setFlashMessage('Restarting. Try again in a few minutes.', 'info')
            return Promise.resolve(false)
        }
        return getConnectionInfo(this.getVirtualDesktopClient(), {
            idea_session_id: session.idea_session_id,
            idea_session_owner: session.owner,
            dcv_session_id: session.dcv_session_id
        }).then(info => {
            downloadDcvFile(info, `${session.name}`, session.dcv_session_id)
            return true
        }).catch(error => {
            this.displayActionError(error, 'download the DCV client file for')
            return false
        })
    }

    onLaunchSession = (session: VirtualDesktopSession): Promise<boolean> => {
        if (session.recovering) {
            this.setFlashMessage('Restarting. Try again in a few minutes.', 'info')
            return Promise.resolve(false)
        }
        return getConnectionInfo(this.getVirtualDesktopClient(), {
            idea_session_id: session.idea_session_id,
            idea_session_owner: session.owner,
            dcv_session_id: session.dcv_session_id
        }).then(info => {
            // the request above took long enough that the browser no longer counts this as the click
            if (!openInNewTab(info)) {
                this.setFlashMessage(POPUP_BLOCKED_MESSAGE, 'error')
                return false
            }
            return true
        }).catch(error => {
            this.displayActionError(error, 'connect to')
            return false
        })
    }

    onShareSession = (session: VirtualDesktopSession): Promise<boolean> => {
        this.setState({
            selectedSession: session
        }, () => {
            this.showUpdateSessionPermissionForm()
        })

        return Promise.resolve(true)
    }

    onShowSchedule = (session: VirtualDesktopSession): Promise<boolean> => {
        this.getScheduleModal().showSchedule(session)
        return Promise.resolve(true)
    }

    showUpdateSessionForm() {
        this.setState({
            showUpdateSessionForm: true,
        }, () => {
            this.getUpdateSessionForm().showModal()
        })
    }

    showCreateSessionForm() {
        const open = () => this.setState({
            showCreateSessionForm: true
        }, () => {
            this.getCreateSessionForm().showModal()
        })
        // the form is where the projects are needed, so a failed page load gets another try here
        if (this.state.userProjectsFailed) {
            this.loadUserProjects().finally(open)
        } else {
            open()
        }
    }


    showUpdateSessionPermissionForm() {
        this.setState({
            showUpdateSessionPermissionForm: true
        }, () => {
            this.getUpdateSessionPermissionForm()?.showModal()
        })
    }

    hideUpdateSessionPermissionForm() {
        this.setState({
            showUpdateSessionPermissionForm: false
        })
    }

    hideCreateSessionForm() {
        this.setState({
            showCreateSessionForm: false
        })
    }

    hideUpdateSessionForm() {
        this.setState({
            showUpdateSessionForm: false
        })
    }

    buildUpdateSessionForm() {
        return (
            <IdeaForm
                ref={this.updateSessionForm}
                name={"update-session"}
                modal={true}
                title={"Rename or resize " + this.state.selectedSession?.name}
                modalSize={"medium"}
                onCancel={() => {
                    this.hideUpdateSessionForm()
                }}
                onSubmit={() => {
                    this.getUpdateSessionForm().clearError()
                    if (!this.getUpdateSessionForm().validate()) {
                        return
                    }

                    if (this.state.selectedSession === undefined || this.state.selectedSession?.server === undefined) {
                        return
                    }

                    const values = this.getUpdateSessionForm().getValues()
                    let session: VirtualDesktopSession = {
                        idea_session_id: this.state.selectedSession.idea_session_id,
                        owner: this.state.selectedSession.owner,
                        name: values.session_name,
                        server: {
                            instance_id: this.state.selectedSession.server.instance_id,
                            instance_type: values.instance_type
                        }
                    }
                    return this.getVirtualDesktopClient().updateSession({
                        session: session
                    }).then(result => {
                        this.hideUpdateSessionForm()
                        return this.setSession(result.session!)
                    }).catch(error => {
                        this.getUpdateSessionForm().setError(error.errorCode, error.message)
                        return false
                    })
                }}
                params={[
                    {
                        name: 'session_name',
                        title: 'Desktop name',
                        data_type: 'str',
                        param_type: 'text',
                        help_text: 'Use 3 to 24 characters.',
                        default: this.state.selectedSession?.name,
                        validate: {
                            required: true,
                            regex: '^.{3,24}$',
                            message: 'Use 3 to 24 characters.'
                        } as IdeaFormValidate
                    },
                    {
                        name: 'instance_type',
                        title: 'Desktop size',
                        help_text: this.state.selectedSession?.hibernation_enabled ? "A desktop that hibernates can't change size." : '',
                        data_type: 'str',
                        readonly: this.state.selectedSession?.hibernation_enabled,
                        default: this.state.selectedSession?.server?.instance_type,
                        param_type: 'select_or_text',
                        validate: {
                            required: true
                        },
                        choices: this.state.selectedSessionApplicableInstanceTypes
                    }
                ]}/>
        )
    }

    buildUpdateSessionPermissionForm() {
        return (
            <UpdateSessionPermissionModal
                ref={this.updateSessionPermissionForm}
                modalSize={"large"}
                onCancel={() => {
                    this.hideUpdateSessionPermissionForm()
                }}
                onSubmit={(createdPermissions: VirtualDesktopSessionPermission[], updatedPermissions: VirtualDesktopSessionPermission[], deletedPermissions: VirtualDesktopSessionPermission[]) => {
                    return this.getVirtualDesktopClient().updateSessionPermissions({
                        create: createdPermissions,
                        update: updatedPermissions,
                        delete: deletedPermissions
                    }).then(_ => {
                        this.getUpdateSessionPermissionForm().hideModal()
                        this.setState({
                            showUpdateSessionPermissionForm: false
                        })
                        return Promise.resolve(true)
                    }).catch(error => {
                        // Extract specific failure reasons from the payload if available
                        let detailedMessage = error.message
                        if (error.payload && error.payload.permissions) {
                            const failureReasons = error.payload.permissions
                                .filter((permission: any) => permission.failure_reason)
                                .map((permission: any) => `${permission.failure_reason}`)

                            if (failureReasons.length > 0) {
                                detailedMessage = failureReasons.join('\n')
                            }
                        }

                        this.getUpdateSessionPermissionForm()?.setError(error.errorCode, detailedMessage)
                        return Promise.resolve(false)
                    })
                }}
                session={this.state.selectedSession!}/>
        )
    }

    buildCreateSessionForm() {
        return (
            <VirtualDesktopCreateSessionForm
                ref={this.createSessionForm}
                defaultName={`MyDesktop${(this.state.sessions.size) + 1}`}
                maxRootVolumeMemory={this.virtualDesktopSettings?.dcv_session.max_root_volume_memory}
                projects={this.state.userProjects}
                projectsLoadFailed={this.state.userProjectsFailed}
                onSubmit={(session_name, username, project_id, base_os, software_stack_id, session_type, instance_type, storage_size, hibernation_enabled, vpc_subnet_id) => {
                    return this.getVirtualDesktopClient().createSession({
                        session: {
                            name: session_name,
                            owner: username,
                            hibernation_enabled: hibernation_enabled,
                            software_stack: {
                                stack_id: software_stack_id,
                                base_os: base_os
                            },
                            server: {
                                instance_type: instance_type,
                                root_volume_size: {
                                    value: storage_size,
                                    unit: "gb"
                                },
                                subnet_id: vpc_subnet_id
                            },
                            project: {
                                project_id: project_id
                            },
                            type: session_type
                        }
                    }).then(result => {
                        this.getCreateSessionForm().hideForm()
                        this.setSession(result.session!).finally()
                        return Promise.resolve(true)
                    }).catch(error => {
                        this.getCreateSessionForm()?.setError(error.errorCode, error.message)
                        return Promise.resolve(false)
                    })
                }}
                onDismiss={() => {
                    this.hideCreateSessionForm()
                }}
            />
        )
    }

    buildActiveConnectionConfirmModal() {
        return (
            <IdeaConfirm
                ref={this.activeConnectionConfirmModal}
                title="This desktop is in use"
                onConfirm={this.state.activeConnectionConfirmModalOnConfirm}
                onCancel={() => {
                    this.setState({
                        selectedSession: undefined
                    })
                }}
            >
                {this.state.selectedSession?.connection_count === 1 ? 'There is 1 open connection.' : `There are ${this.state.selectedSession?.connection_count} open connections.`} Anyone connected will be disconnected. Continue?
            </IdeaConfirm>
        )
    }

    buildSessionActionConfirmModal() {
        return (
            <IdeaConfirm
                ref={this.sessionActionConfirmModal}
                title={this.state.confirmAction.actionTitle}
                onConfirm={this.state.confirmAction.onConfirm}
                onCancel={this.state.confirmAction.onCancel}
                dangerConfirm={this.state.confirmAction.actionTitle === "Terminate Virtual Desktop"}
            >
                {this.state.confirmAction.actionText}
            </IdeaConfirm>)
    }

    buildDcvClientHelpModal() {
        return (this.state.selectedSession &&
            <DcvClientHelpModal
                session={this.state.selectedSession}
                onDismiss={() => {
                    this.setState({
                        showDcvClientHelpModal: false,
                        selectedSession: undefined
                    })
                }}
                onDownloadDcvSessionFile={this.onDownloadDcvSessionFile}
                onLaunchSession={this.onLaunchSession}
                visible={this.state.showDcvClientHelpModal}
            />
        )
    }

    buildScheduleModal() {
        return <VirtualDesktopScheduleModal
            ref={this.scheduleModal}
            onScheduleChange={(session) => {
                return this.getVirtualDesktopClient().updateSession({
                    session: session
                }).then(response => {
                    return this.setSession(response.session!)
                }).catch(error => {
                    this.getScheduleModal().setErrorMessage(error.message)
                    return false
                })
            }}
        />
    }

    isActiveDirectory(): boolean {
        return this.state.directoryServiceProvider === 'activedirectory' || this.state.directoryServiceProvider === 'aws_managed_activedirectory'
    }

    /** Empty only means no desktops once the listing has answered, and only for the filter in use. */
    buildEmpty() {
        let message = 'No desktops yet'
        let action = <Button onClick={() => this.showCreateSessionForm()}>Launch desktop</Button>
        if (this.state.listError) {
            message = 'Your desktops could not be loaded. They are not affected.'
            action = <Button onClick={this.retryList}>Try again</Button>
        } else if (this.state.osFilter !== OS_FILTER_ALL_ID) {
            message = `No ${this.state.osFilter === OS_FILTER_WINDOWS_ID ? 'Windows' : 'Linux'} desktops.`
            action = <Button onClick={() => this.setState({osFilter: OS_FILTER_ALL_ID}, this.retryList)}>Show all desktops</Button>
        }
        return <Box textAlign="center" color="inherit" padding={{top: 'xxxl', bottom: "s"}}>
            <SpaceBetween size="m">
                <Box variant="strong">{message}</Box>
                {action}
            </SpaceBetween>
        </Box>
    }

    buildListing() {

        const getSessions = (): VirtualDesktopSession[] => {
            let sessions: VirtualDesktopSession[] = []
            this.state.sessions.forEach((session) => {
                if (matchesOsFilter(session, this.state.osFilter)) {
                    sessions.push(session)
                }
            })
            sessions.sort((session_a, session_b) => {
                if (session_a.created_on === undefined && session_b.created_on === undefined) {
                    return 0
                }

                if (session_a.created_on === undefined) {
                    return -1
                }

                if (session_b.created_on === undefined) {
                    return 1
                }

                return session_a.created_on > session_b.created_on ? 1 : -1
            })
            return sessions
        }

        return <Cards
            stickyHeader={true}
            // the page header already says "My desktops" and the tabs say Owned, so this bar only carries actions
            header={
                <div style={{display: 'flex', justifyContent: 'flex-end'}}>
                    <SpaceBetween direction="horizontal" size="l">
                        <Button variant="normal" iconName="refresh" ariaLabel="Refresh desktops" onClick={this.retryList}/>
                        <SegmentedControl
                            selectedId={this.state.osFilter}
                            onChange={({detail}) => {
                                this.setState({
                                    osFilter: detail.selectedId
                                }, () => this.fetchSessions())
                            }}
                            options={[
                                {text: "All", id: OS_FILTER_ALL_ID},
                                {text: "Windows", id: OS_FILTER_WINDOWS_ID},
                                {text: "Linux", id: OS_FILTER_LINUX_ID}
                            ]}/>
                        <Button key="launch-new-virtual-desktop" variant="primary" onClick={() => {
                            this.showCreateSessionForm()
                        }}>
                            Launch desktop
                        </Button>
                    </SpaceBetween>
                </div>
            }
            trackBy="idea_session_id"
            ariaLabels={{
                itemSelectionLabel: (e, t) => `select ${t.name}`,
                selectionGroupLabel: "Item selection"
            }}
            loading={this.state.loading}
            loadingText="Loading your desktops"
            variant="full-page"
            cardDefinition={{
                sections: [
                    {
                        id: "card",
                        content: (session: VirtualDesktopSession) => {
                            return <VirtualDesktopSessionCard
                                isActiveDirectory={this.isActiveDirectory()}
                                virtualDesktopClient={this.getVirtualDesktopClient()}
                                session={session}
                                projectAiAccessPending={this.isProjectAiAccessPending(session)}
                                idleAutoStopDelayMax={this.virtualDesktopSettings?.dcv_session.idle_autostop_delay_max}
                                workingHours={this.virtualDesktopSettings?.dcv_session.working_hours}
                                onDeleteSession={this.onDeleteSession}
                                onStartSession={this.onStartSession}
                                onStopSession={this.onStopSession}
                                onRebootSession={this.onRebootSession}
                                onDownloadDcvSessionFile={this.onDownloadDcvSessionFile}
                                onLaunchSession={this.onLaunchSession}
                                onShowSchedule={this.onShowSchedule}
                                onUpdateSessionPermission={this.onShareSession}
                                onUpdateSession={(session) => {
                                    return this.getVirtualDesktopUtilsClient().listAllowedInstanceTypesForSession({
                                        session: session
                                    }).then(result => new Promise<boolean>((resolve) => {
                                        this.setState({
                                            selectedSession: session,
                                            selectedSessionApplicableInstanceTypes: Utils.generateInstanceTypeListing(result.listing)
                                        }, () => {
                                            this.showUpdateSessionForm()
                                            resolve(true)
                                        })
                                    })).catch(error => {
                                        this.displayActionError(error, 'update')
                                        return false
                                    })
                                }}
                                onConnectHelp={(session) => {
                                    return new Promise<boolean>((resolve) => {
                                        this.setState({
                                            showDcvClientHelpModal: true,
                                            selectedSession: session
                                        }, () => {
                                            resolve(true)
                                        })
                                    })
                                }}
                                screenshot={this.state.screenshots[session?.dcv_session_id!]?.data}
                                previewFailed={this.state.previewFailed[session?.dcv_session_id!]}
                            />
                        }
                    }
                ]
            }}
            cardsPerRow={[
                {
                    cards: 1
                },
                {
                    minWidth: 800,
                    cards: 2
                },
                {
                    minWidth: 1400,
                    cards: 3
                },
                {
                    minWidth: 1920,
                    cards: 4
                }
            ]}
            empty={this.buildEmpty()}
            items={getSessions()}
        />
    }

    render() {
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
                        text: 'My desktops',
                        href: ''
                    }
                ]}
                contentType={"cards"}
                content={
                    <div>
                        {this.state.listError && this.state.sessions.size > 0 &&
                            <Alert type="warning" action={<Button onClick={this.retryList}>Try again</Button>}>Could not refresh. Showing the last known state.</Alert>}
                        {this.buildListing()}
                        {this.state.showCreateSessionForm && this.buildCreateSessionForm()}
                        {this.state.showUpdateSessionForm && this.buildUpdateSessionForm()}
                        {this.state.showUpdateSessionPermissionForm && this.buildUpdateSessionPermissionForm()}
                        {this.buildSessionActionConfirmModal()}
                        {this.buildActiveConnectionConfirmModal()}
                        {this.buildDcvClientHelpModal()}
                        {this.buildScheduleModal()}
                    </div>
                }/>
        )
    }
}

export default withRouter(MyVirtualDesktopSessions)
