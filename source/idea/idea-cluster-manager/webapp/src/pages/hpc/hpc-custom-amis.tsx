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

import {PathRouteProps} from "react-router-dom";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {AppContext} from "../../common";
import {Constants} from "../../common/constants";
import Utils from "../../common/utils";
import {withRouter} from "../../navigation/navigation-utils";
import {ImageBuildRecord, ImageInventoryRow} from "../../client/data-model";
import {
    Box,
    Button,
    Checkbox,
    Container,
    FormField,
    Header,
    Input,
    Modal,
    Popover,
    Select,
    SpaceBetween,
    StatusIndicator,
    Table
} from "@cloudscape-design/components";

export interface HpcCustomAmisProps extends PathRouteProps, IdeaAppLayoutProps, IdeaSideNavigationProps {

}

type ImageKind = 'compute' | 'desktop'

export interface HpcCustomAmisState {
    compute: ImageInventoryRow[]
    desktop: ImageInventoryRow[]
    computeError?: string
    desktopError?: string
    loading: boolean
    vdcDeployed: boolean
    // No row means Add image mode, where the base OS comes from the addOs picker.
    buildDialog?: { kind: ImageKind, row?: ImageInventoryRow }
    baseAmi: string
    instanceType: string
    efa: boolean
    fsxLustre: boolean
    updateStack: boolean
    submitting: boolean
    defaultDialog?: ImageInventoryRow
    buildAllOpen: boolean
    supportedBaseOs: string[]
    addOs: string
    addArchitecture: string
    computeNodeOs?: string
    adoptDialog?: { kind: ImageKind, row: ImageInventoryRow }
    adoptError?: string
}

const POLL_INTERVAL_MS = 30000
const SCHEDULER_DEFAULT_REFERENCE = 'scheduler default'
const QUEUE_PROFILE_REFERENCE = 'queue profile: '

/** The queue profiles named in a row's Referenced by column, as the service wrote them. */
const referencedQueueProfiles = (row: ImageInventoryRow): string[] =>
    (row.referenced_by ?? [])
        .filter(reference => reference.startsWith(QUEUE_PROFILE_REFERENCE))
        .map(reference => reference.slice(QUEUE_PROFILE_REFERENCE.length))

/** The image a completed build produced, when the row does not use it yet. */
const adoptableImage = (row: ImageInventoryRow): string | undefined => {
    const build = row.last_build
    if (build?.status === 'complete' && build.image_id && build.image_id !== row.image_id) {
        return build.image_id
    }
    return undefined
}
const DEFAULT_INSTANCE_TYPE: Record<ImageKind, string> = {
    compute: 'c5.large',
    desktop: 'm6i.large'
}
/** Both modules default an arm64 build to the same builder size, whatever the kind. */
const DEFAULT_ARM64_INSTANCE_TYPE = 'm6g.large'
const DEFAULT_ARCHITECTURE = 'x86_64'
const COMPUTE_ARCHITECTURES = ['x86_64', 'arm64']

const formatDate = (value?: string | Date): string => {
    if (!value) {
        return '-'
    }
    const date = new Date(value)
    return isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

/** The date without the time, for a build date shown beside other text. */
const formatDay = (value?: string | Date): string => {
    const date = value ? new Date(value) : undefined
    return date && !isNaN(date.getTime()) ? date.toLocaleDateString() : ''
}

/** Clips rather than wraps, so a long image or vendor name cannot grow a row past two lines. */
const oneLine: React.CSSProperties = {overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'}

/** Joins the parts of a cell that do not fit, for display in a popover. */
const joinDetail = (parts: (string | undefined)[]): string => parts.filter(part => !!part).join('. ')

class HpcCustomAmis extends Component<HpcCustomAmisProps, HpcCustomAmisState> {

    private pollTimer?: ReturnType<typeof setInterval>

    constructor(props: HpcCustomAmisProps) {
        super(props);
        this.state = {
            compute: [],
            desktop: [],
            loading: true,
            vdcDeployed: AppContext.get().getClusterSettingsService().isVirtualDesktopDeployed(),
            baseAmi: '',
            instanceType: '',
            efa: true,
            fsxLustre: true,
            updateStack: true,
            submitting: false,
            buildAllOpen: false,
            supportedBaseOs: [],
            addOs: '',
            addArchitecture: DEFAULT_ARCHITECTURE
        }
    }

    /** The compute rows worth showing: a combination with no image and no build stays hidden. */
    visibleComputeRows(): ImageInventoryRow[] {
        return this.state.compute.filter(row => row.state !== 'none' || !!row.last_build)
    }

    /**
     * The supported (base OS, architecture) combinations with no visible row, which is what
     * Add image offers. An OS with no image at all comes first: that is the gap the button
     * exists to close, and a second architecture for an OS that has one is the rarer case.
     */
    missingComputeCombinations(): { base_os: string, architecture: string }[] {
        const rows = this.visibleComputeRows()
        const shown = new Set(rows.map(row => `${row.base_os}/${row.architecture ?? DEFAULT_ARCHITECTURE}`))
        const listed = new Set(rows.map(row => row.base_os))
        const missing = this.state.supportedBaseOs.flatMap(base_os =>
            COMPUTE_ARCHITECTURES
                .filter(architecture => !shown.has(`${base_os}/${architecture}`))
                .map(architecture => ({base_os, architecture})))
        return [...missing.filter(row => !listed.has(row.base_os)), ...missing.filter(row => listed.has(row.base_os))]
    }

    /** The architectures Add image can still start a first image for, within one base OS. */
    missingArchitectures(base_os: string): string[] {
        return this.missingComputeCombinations().filter(row => row.base_os === base_os).map(row => row.architecture)
    }

    componentDidMount() {
        this.load()
    }

    componentWillUnmount() {
        this.stopPolling()
    }

    setFlashMessage(content: React.ReactNode, type: 'success' | 'info' | 'warning' | 'error') {
        this.props.onFlashbarChange({
            items: [{
                type: type,
                content: content,
                dismissible: true
            }]
        })
    }

    load = () => {
        this.setState({loading: true})
        const clients = AppContext.get().client()
        // Each table fetches and fails on its own, so a controller error does not blank the compute rows.
        const compute = clients.schedulerAdmin().listComputeImages({}).then(result => {
            this.setState({compute: result.listing ?? [], supportedBaseOs: result.supported_base_os ?? [], computeNodeOs: result.compute_node_os, computeError: undefined})
        }).catch(error => {
            this.setState({computeError: `Failed to list compute images: ${error.message}`})
        })
        const desktop = this.state.vdcDeployed
            ? clients.virtualDesktopAdmin().listDesktopImages({}).then(result => {
                this.setState({desktop: result.listing ?? [], desktopError: undefined})
            }).catch(error => {
                this.setState({desktopError: `Failed to list desktop images: ${error.message}`})
            })
            : Promise.resolve()
        Promise.all([compute, desktop]).then(() => {
            this.setState({loading: false}, this.syncPolling)
        })
    }

    private syncPolling = () => {
        const building = [...this.state.compute, ...this.state.desktop].some(row => row.state === 'building')
        if (building && !this.pollTimer) {
            this.pollTimer = setInterval(this.load, POLL_INTERVAL_MS)
        } else if (!building) {
            this.stopPolling()
        }
    }

    private stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer)
            this.pollTimer = undefined
        }
    }

    // Build

    openBuild = (kind: ImageKind, row: ImageInventoryRow) => {
        this.setState({
            buildDialog: {kind, row},
            baseAmi: '',
            instanceType: '',
            efa: true,
            fsxLustre: true,
            updateStack: true,
            submitting: false
        })
    }

    openAddImage = () => {
        const first = this.missingComputeCombinations()[0]
        this.setState({
            buildDialog: {kind: 'compute'},
            addOs: first?.base_os ?? '',
            addArchitecture: first?.architecture ?? DEFAULT_ARCHITECTURE,
            baseAmi: '',
            instanceType: '',
            efa: true,
            fsxLustre: true,
            updateStack: true,
            submitting: false
        })
    }

    closeBuild = () => {
        this.setState({buildDialog: undefined, submitting: false})
    }

    submitBuild = () => {
        const dialog = this.state.buildDialog
        if (!dialog) {
            return
        }
        const baseOs = dialog.row?.base_os ?? this.state.addOs
        if (!baseOs) {
            return
        }
        // a row rebuild keeps the architecture it runs; Add image takes the one just picked
        const architecture = dialog.row?.architecture ?? this.state.addArchitecture
        const clients = AppContext.get().client()
        const baseAmi = this.state.baseAmi.trim() || undefined
        const instanceType = this.state.instanceType.trim() || undefined
        this.setState({submitting: true})
        const started = dialog.kind === 'compute'
            ? clients.schedulerAdmin().buildComputeImage({
                base_os: baseOs,
                architecture: architecture,
                base_ami: baseAmi,
                instance_type: instanceType,
                enable_drivers: [
                    ...(this.state.efa ? ['efa'] : []),
                    ...(this.state.fsxLustre ? ['fsx_lustre'] : [])
                ]
            }).then(result => result.record)
            : clients.virtualDesktopAdmin().buildDesktopImage({
                base_os: baseOs,
                architecture: architecture,
                base_ami: baseAmi,
                instance_type: instanceType,
                update_stack: this.state.updateStack
            }).then(result => result.record)
        started.then((record?: ImageBuildRecord) => {
            this.closeBuild()
            this.setFlashMessage(`Build started for ${baseOs} (${architecture}): ${record?.ami_name ?? ''} from ${record?.base_ami ?? 'the stock image'}. This takes about 20 minutes; the row refreshes on its own.`, 'success')
            this.load()
        }).catch(error => {
            this.setState({submitting: false})
            this.setFlashMessage(`Build failed to start: ${error.message}`, 'error')
        })
    }

    openBuildAll = () => {
        this.setState({buildAllOpen: true, submitting: false})
    }

    closeBuildAll = () => {
        this.setState({buildAllOpen: false, submitting: false})
    }

    submitBuildAll = () => {
        this.setState({submitting: true})
        AppContext.get().client().virtualDesktopAdmin().buildAllDesktopImages({}).then(response => {
            const results = response.results ?? []
            const started = results.filter(r => r.status === 'started').length
            const skipped = results.filter(r => r.status === 'skipped').length
            const failed = results.filter(r => r.status === 'error').length
            this.closeBuildAll()
            this.setFlashMessage(`Desktop image builds: ${started} started, ${skipped} skipped (already building), ${failed} failed to start.`, failed > 0 ? 'warning' : 'success')
            this.load()
        }).catch(error => {
            this.setState({submitting: false})
            this.setFlashMessage(`Build all failed to start: ${error.message}`, 'error')
        })
    }

    // Use built image: repoints whatever uses the row's current image at the completed build.

    openAdopt = (kind: ImageKind, row: ImageInventoryRow) => {
        this.setState({adoptDialog: {kind, row}, adoptError: undefined, submitting: false})
    }

    closeAdopt = () => {
        this.setState({adoptDialog: undefined, adoptError: undefined, submitting: false})
    }

    adoptChanges(row: ImageInventoryRow): { setDefault: boolean, queueProfiles: string[] } {
        return {
            setDefault: row.base_os === this.state.computeNodeOs && (row.referenced_by ?? []).includes(SCHEDULER_DEFAULT_REFERENCE),
            queueProfiles: referencedQueueProfiles(row)
        }
    }

    submitAdopt = async () => {
        const dialog = this.state.adoptDialog
        const row = dialog?.row
        const built = row ? adoptableImage(row) : undefined
        if (!dialog || !row || !built) {
            return
        }
        if (dialog.kind === 'desktop') {
            this.setState({submitting: true, adoptError: undefined})
            AppContext.get().client().virtualDesktopAdmin().useBuiltDesktopImages({stack_ids: [row.stack_id!]}).then(response => {
                const outcome = (response.results ?? [])[0]
                if (outcome?.status === 'updated') {
                    this.closeAdopt()
                    this.setFlashMessage(`${row.stack_id} now launches from ${built}.`, 'success')
                    this.load()
                } else {
                    this.setState({submitting: false, adoptError: outcome?.message ?? 'the stack was not repointed'})
                }
            }).catch(error => {
                this.setState({submitting: false, adoptError: error.message})
            })
            return
        }
        const changes = this.adoptChanges(row)
        const clients = AppContext.get().client()
        const clusterSettingsService = AppContext.get().getClusterSettingsService()
        this.setState({submitting: true, adoptError: undefined})
        const done: string[] = []
        try {
            if (changes.setDefault) {
                const moduleId = Utils.asString(clusterSettingsService.getModuleId(Constants.MODULE_SCHEDULER), Constants.MODULE_SCHEDULER)
                const result = await clients.clusterSettings().updateModuleSettings({
                    module_id: moduleId,
                    settings: {compute_node_ami: built}
                })
                if (!Utils.asBoolean(result.success, false)) {
                    throw new Error('scheduler.compute_node_ami was not updated')
                }
                done.push('scheduler default')
            }
            if (changes.queueProfiles.length > 0) {
                const listing = (await clients.schedulerAdmin().listQueueProfiles({})).listing ?? []
                for (const name of changes.queueProfiles) {
                    const profile = listing.find(candidate => candidate.name === name)
                    if (!profile) {
                        throw new Error(`queue profile ${name} was not found`)
                    }
                    await clients.schedulerAdmin().updateQueueProfile({
                        queue_profile: {
                            ...profile,
                            default_job_params: {...(profile.default_job_params ?? {}), instance_ami: built}
                        }
                    })
                    done.push(`queue profile ${name}`)
                }
            }
            this.closeAdopt()
            this.setFlashMessage(`${row.base_os} now uses ${built}: ${done.join(', ')}.`, 'success')
            this.load()
        } catch (error: any) {
            this.setState({
                submitting: false,
                adoptError: `${error.message}${done.length > 0 ? ` (already applied: ${done.join(', ')})` : ''}`
            })
        }
    }

    // Set as scheduler default

    openSetDefault = (row: ImageInventoryRow) => {
        this.setState({defaultDialog: row, submitting: false})
    }

    closeSetDefault = () => {
        this.setState({defaultDialog: undefined, submitting: false})
    }

    submitSetDefault = () => {
        const row = this.state.defaultDialog
        if (!row || !row.image_id) {
            return
        }
        const clusterSettingsService = AppContext.get().getClusterSettingsService()
        const moduleId = Utils.asString(clusterSettingsService.getModuleId(Constants.MODULE_SCHEDULER), Constants.MODULE_SCHEDULER)
        this.setState({submitting: true})
        AppContext.get().client().clusterSettings().updateModuleSettings({
            module_id: moduleId,
            settings: {
                compute_node_ami: row.image_id
            }
        }).then(result => {
            this.closeSetDefault()
            if (Utils.asBoolean(result.success, false)) {
                this.setFlashMessage(`scheduler.compute_node_ami is now ${row.image_id} (${row.base_os}). Jobs without an explicit AMI use it from the next launch.`, 'success')
            } else {
                this.setFlashMessage('Failed to update the scheduler default image', 'error')
            }
            this.load()
        }).catch(error => {
            this.setState({submitting: false})
            this.setFlashMessage(`Failed to update the scheduler default image: ${error.message}`, 'error')
        })
    }

    // Rendering

    renderBaseOs(row: ImageInventoryRow) {
        return (
            <SpaceBetween size="xxs">
                <Box>{row.base_os}</Box>
                <Box fontSize="body-s" color="text-body-secondary">{row.architecture ?? '-'}</Box>
            </SpaceBetween>
        )
    }

    /** The state as an indicator, before any note the column wraps around it. */
    stateIndicator(row: ImageInventoryRow) {
        switch (row.state) {
            case 'built':
                return <StatusIndicator type="success">Built</StatusIndicator>
            case 'built_outdated':
                return <StatusIndicator type="warning">Built (base outdated)</StatusIndicator>
            case 'stock':
                return <StatusIndicator type="info">Stock</StatusIndicator>
            case 'building':
                return <StatusIndicator type="in-progress">Building</StatusIndicator>
            case 'missing':
                return <StatusIndicator type="error">Missing</StatusIndicator>
            default:
                // A row with no image but a completed build has an image to adopt, so it is idle rather than absent.
                return <StatusIndicator type="stopped">{row.last_build?.status === 'complete' ? 'Not in use' : 'None'}</StatusIndicator>
        }
    }

    /** The architecture the scheduler default runs: its single AMI can hold no other. */
    schedulerDefaultArchitecture(): string {
        const row = this.state.compute.find(candidate => (candidate.referenced_by ?? []).includes(SCHEDULER_DEFAULT_REFERENCE))
        return row?.architecture ?? DEFAULT_ARCHITECTURE
    }

    /** True when a compute row runs an architecture the scheduler default cannot hold. */
    architectureMismatch(kind: ImageKind, row: ImageInventoryRow): boolean {
        return kind === 'compute' && (row.architecture ?? DEFAULT_ARCHITECTURE) !== this.schedulerDefaultArchitecture()
    }

    renderState(kind: ImageKind, row: ImageInventoryRow) {
        const indicator = this.stateIndicator(row)
        // A row of another architecture that no queue profile names has nowhere to put its image.
        const note = this.architectureMismatch(kind, row) && referencedQueueProfiles(row).length === 0
            ? `Scheduler default runs ${this.schedulerDefaultArchitecture()}. Assign this image to a queue profile with ${row.architecture ?? DEFAULT_ARCHITECTURE} instance types.`
            : undefined
        if (!note) {
            return indicator
        }
        // The note is wider than the column, so it rides a popover instead of growing the row.
        return (
            <Popover dismissButton={false} position="top" size="small" triggerType="text" content={note}>
                {indicator}
            </Popover>
        )
    }

    renderLastBuild(row: ImageInventoryRow) {
        const build = row.last_build
        if (!build) {
            return '-'
        }
        const type = build.status === 'complete' ? 'success' : build.status === 'failed' ? 'error' : build.status === 'skipped' ? 'stopped' : 'in-progress'
        const label = build.status === 'complete' ? 'Complete' : build.status === 'failed' ? 'Failed' : build.status === 'skipped' ? 'Skipped' : 'Building'
        const when = build.status === 'building' ? `started ${formatDate(build.started_on)}` : formatDate(build.finished_on)
        // Who asked and which instance ran it, behind the status rather than beside the date.
        const detail = joinDetail([
            build.instance_id ? `builder ${build.instance_id}` : undefined,
            build.requested_by ? `requested by ${build.requested_by}` : undefined
        ])
        const indicator = <StatusIndicator type={type}>{label}</StatusIndicator>
        const built = adoptableImage(row)
        return (
            <SpaceBetween size="xxs">
                <div style={oneLine} title={detail ? `${when}. ${detail}` : when}>
                    {detail
                        ? <Popover dismissButton={false} position="top" size="small" triggerType="custom" content={detail}>{indicator}</Popover>
                        : indicator}
                    {' '}
                    <Box variant="span" fontSize="body-s" color="text-body-secondary">{when}</Box>
                </div>
                {build.error
                    ? <Box fontSize="body-s" color="text-status-error">{build.error}</Box>
                    : built && <Box fontSize="body-s" color="text-body-secondary">{built}</Box>}
            </SpaceBetween>
        )
    }

    renderImage(row: ImageInventoryRow) {
        if (!row.image_id) {
            return '-'
        }
        // A built image is named for its base OS and build stamp, both already on the row, so the
        // date replaces the name. A stock image carries the vendor name, which stays on the line.
        const secondary = row.build_date ? `built ${formatDay(row.build_date)}` : (row.image_name ?? '')
        // The stock base a desktop stack would rebuild from, plus anything the service flagged.
        const detail = joinDetail([
            row.base_ami_id ? `base: ${row.base_ami_id}` : undefined,
            row.build_date ? row.image_name : undefined,
            row.notes
        ])
        return (
            <SpaceBetween size="xxs">
                <Box>
                    {detail
                        ? <Popover dismissButton={false} position="top" size="small" triggerType="text" content={detail}>{row.image_id}</Popover>
                        : row.image_id}
                </Box>
                {secondary !== '' && (
                    <div style={oneLine} title={secondary}>
                        <Box variant="span" fontSize="body-s" color="text-body-secondary">{secondary}</Box>
                    </div>
                )}
            </SpaceBetween>
        )
    }

    renderActions(kind: ImageKind, row: ImageInventoryRow) {
        const building = row.state === 'building'
        // An image of another architecture belongs on a queue profile with matching instance types,
        // never on the single scheduler default AMI.
        const mismatched = this.architectureMismatch(kind, row)
        // The scheduler default never crosses operating systems: only a row of the cluster's
        // compute OS can become it, and only when it is not already the default.
        const canSetDefault = kind === 'compute'
            && !mismatched
            && !!row.image_id
            && !!this.state.computeNodeOs
            && row.base_os === this.state.computeNodeOs
            && !(row.referenced_by ?? []).includes(SCHEDULER_DEFAULT_REFERENCE)
        // Adopting on a mismatched row repoints the queue profiles that name it, never the default.
        const canAdopt = (!mismatched || referencedQueueProfiles(row).length > 0) && !!adoptableImage(row)
        return (
            <div style={{display: 'flex', flexWrap: 'wrap', gap: '4px'}}>
                <Button variant="link" disabled={building} onClick={() => this.openBuild(kind, row)}>Build</Button>
                {canAdopt && <Button variant="link" onClick={() => this.openAdopt(kind, row)}>Use built image</Button>}
                {canSetDefault && <Button variant="link" onClick={() => this.openSetDefault(row)}>Set as default</Button>}
            </div>
        )
    }

    renderReferencedBy(row: ImageInventoryRow) {
        const refs = row.referenced_by ?? []
        if (refs.length === 0) {
            return '-'
        }
        return (
            <SpaceBetween size="xxs">
                {refs.map(ref => <Box key={ref} fontSize="body-s">{ref}</Box>)}
            </SpaceBetween>
        )
    }

    // Six columns, each with a minimum width. The minimums add up to 890px, below which the table
    // scrolls inside its own container.
    renderTable(kind: ImageKind, rows: ImageInventoryRow[], title: string, description: string, error?: string, headerActions?: React.ReactNode) {
        return (
            <Table
                variant="container"
                resizableColumns={true}
                wrapLines={true}
                loading={this.state.loading}
                items={rows}
                empty={error ? <StatusIndicator type="error">{error}</StatusIndicator> : <Box textAlign="center">No images</Box>}
                header={
                    <SpaceBetween size="xs">
                        <Header variant="h2" description={description} actions={headerActions}>{title}</Header>
                        {error && rows.length > 0 && <StatusIndicator type="error">{error} (showing the last successful listing)</StatusIndicator>}
                    </SpaceBetween>
                }
                columnDefinitions={[
                    {id: 'base_os', header: 'Base OS', cell: row => this.renderBaseOs(row), width: 200, minWidth: 180},
                    {id: 'image', header: 'Image', cell: row => this.renderImage(row), minWidth: 180},
                    {id: 'state', header: 'State', cell: row => this.renderState(kind, row), width: 165, minWidth: 150},
                    {id: 'referenced_by', header: 'Referenced by', cell: row => this.renderReferencedBy(row), minWidth: 150},
                    {id: 'last_build', header: 'Last build', cell: row => this.renderLastBuild(row), minWidth: 190},
                    {id: 'actions', header: 'Actions', cell: row => this.renderActions(kind, row), width: 165, minWidth: 150}
                ]}
            />
        )
    }

    renderBuildDialog() {
        const dialog = this.state.buildDialog
        if (!dialog) {
            return null
        }
        const isCompute = dialog.kind === 'compute'
        const baseOs = dialog.row?.base_os ?? this.state.addOs
        const architecture = dialog.row?.architecture ?? this.state.addArchitecture
        const defaultInstanceType = architecture === 'arm64' ? DEFAULT_ARM64_INSTANCE_TYPE : DEFAULT_INSTANCE_TYPE[dialog.kind]
        return (
            <Modal
                visible={true}
                onDismiss={this.closeBuild}
                header={dialog.row
                    ? `Build ${isCompute ? 'compute' : 'desktop'} image: ${dialog.row.base_os} (${dialog.row.architecture})`
                    : 'Add compute image'}
                footer={
                    <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" onClick={this.closeBuild} disabled={this.state.submitting}>Cancel</Button>
                            <Button variant="primary" onClick={this.submitBuild} loading={this.state.submitting}>Build</Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                <SpaceBetween size="m">
                    {!dialog.row && (
                        <SpaceBetween size="m">
                            <FormField label="Base OS" description="Operating systems that can still take another compute image.">
                                <Select
                                    selectedOption={baseOs ? {label: baseOs, value: baseOs} : null}
                                    options={Array.from(new Set(this.missingComputeCombinations().map(row => row.base_os))).map(os => ({label: os, value: os}))}
                                    onChange={event => {
                                        const selected = event.detail.selectedOption.value ?? ''
                                        this.setState({addOs: selected, addArchitecture: this.missingArchitectures(selected)[0] ?? DEFAULT_ARCHITECTURE})
                                    }}
                                />
                            </FormField>
                            <FormField label="Architecture" description="Only the architectures this operating system has no compute image for.">
                                <Select
                                    selectedOption={{label: architecture, value: architecture}}
                                    options={this.missingArchitectures(baseOs).map(value => ({label: value, value: value}))}
                                    onChange={event => this.setState({addArchitecture: event.detail.selectedOption.value ?? DEFAULT_ARCHITECTURE})}
                                />
                            </FormField>
                        </SpaceBetween>
                    )}
                    <Box>
                        This launches a builder instance from a stock {baseOs} {architecture} image, installs everything a {isCompute ? 'compute node' : 'desktop'} needs, snapshots it and terminates the builder. About 20 minutes and one instance hour.
                        {isCompute
                            ? ' The new image is not used until you click Set as default or point a queue profile at it.'
                            : ' Running desktops are unaffected; new desktops from the base stack use the new image.'}
                    </Box>
                    <FormField label="Base AMI" description="Leave empty to use the newest stock image the vendor publishes for this OS. A previous build is never used as the base.">
                        <Input value={this.state.baseAmi} placeholder="ami-..." onChange={event => this.setState({baseAmi: event.detail.value})}/>
                    </FormField>
                    <FormField label="Builder instance type" description={`Default ${defaultInstanceType}. Pick a GPU type to build GPU drivers in.`}>
                        <Input value={this.state.instanceType} placeholder={defaultInstanceType} onChange={event => this.setState({instanceType: event.detail.value})}/>
                    </FormField>
                    {isCompute && (
                        <FormField label="Drivers" description="EFA and Lustre drivers should always be built into compute images; uncheck only for an image that will never touch them.">
                            <SpaceBetween size="xs">
                                <Checkbox checked={this.state.efa} onChange={event => this.setState({efa: event.detail.checked})}>EFA</Checkbox>
                                <Checkbox checked={this.state.fsxLustre} onChange={event => this.setState({fsxLustre: event.detail.checked})}>FSx for Lustre client</Checkbox>
                            </SpaceBetween>
                        </FormField>
                    )}
                    {!isCompute && (
                        <Checkbox checked={this.state.updateStack} onChange={event => this.setState({updateStack: event.detail.checked})}>
                            Point {dialog.row?.stack_id} at the new image when the build finishes
                        </Checkbox>
                    )}
                </SpaceBetween>
            </Modal>
        )
    }

    renderAddImageButton() {
        const missing = this.missingComputeCombinations()
        const button = (
            <Button data-testid="add-image" disabled={missing.length === 0} onClick={this.openAddImage}>
                Add image
            </Button>
        )
        if (missing.length === 0) {
            return <span title="Every supported base OS and architecture already has a compute image.">{button}</span>
        }
        return button
    }

    renderBuildAllDialog() {
        if (!this.state.buildAllOpen) {
            return null
        }
        const eligible = this.state.desktop.filter(row => row.state !== 'building')
        const alreadyBuilding = this.state.desktop.length - eligible.length
        return (
            <Modal
                visible={true}
                onDismiss={this.closeBuildAll}
                header="Build all desktop images"
                footer={
                    <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" onClick={this.closeBuildAll} disabled={this.state.submitting}>Cancel</Button>
                            <Button variant="primary" onClick={this.submitBuildAll} loading={this.state.submitting}>Build all</Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                <SpaceBetween size="s">
                    <Box>
                        This starts one build for every base OS listed below: {eligible.length} build{eligible.length === 1 ? '' : 's'}{alreadyBuilding > 0 ? ` (${alreadyBuilding} already building will be skipped)` : ''}, each on its own builder instance, running in parallel, about 20 minutes each.
                    </Box>
                    <Box>
                        Each base stack is repointed at its new image only after that build succeeds; a failed build changes nothing for its OS. Running desktops are unaffected. New desktops start on the new images as each one completes.
                    </Box>
                </SpaceBetween>
            </Modal>
        )
    }

    renderAdoptDialog() {
        const dialog = this.state.adoptDialog
        const row = dialog?.row
        const built = row ? adoptableImage(row) : undefined
        if (!dialog || !row || !built) {
            return null
        }
        if (dialog.kind === 'desktop') {
            return (
                <Modal
                    visible={true}
                    onDismiss={this.closeAdopt}
                    header={`Use built image: ${row.stack_id}`}
                    footer={
                        <Box float="right">
                            <SpaceBetween direction="horizontal" size="xs">
                                <Button variant="link" onClick={this.closeAdopt} disabled={this.state.submitting}>Cancel</Button>
                                <Button variant="primary" onClick={this.submitAdopt} loading={this.state.submitting}>Use built image</Button>
                            </SpaceBetween>
                        </Box>
                    }
                >
                    <SpaceBetween size="s">
                        <Box>Points <strong>{row.stack_id}</strong> at its last completed build <strong>{built}</strong> instead of <strong>{row.image_id}</strong>. Nothing else changes; running desktops are unaffected and new desktops from this stack launch from the built image.</Box>
                        {this.state.adoptError && <StatusIndicator type="error">{this.state.adoptError}</StatusIndicator>}
                    </SpaceBetween>
                </Modal>
            )
        }
        const changes = this.adoptChanges(row)
        const nothing = !changes.setDefault && changes.queueProfiles.length === 0
        return (
            <Modal
                visible={true}
                onDismiss={this.closeAdopt}
                header={`Use built image: ${row.base_os}`}
                footer={
                    <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" onClick={this.closeAdopt} disabled={this.state.submitting}>Cancel</Button>
                            <Button variant="primary" onClick={this.submitAdopt} loading={this.state.submitting} disabled={nothing}>Use built image</Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                <SpaceBetween size="s">
                    <Box>Replaces <strong>{row.image_id}</strong> with the completed build <strong>{built}</strong> everywhere this row is referenced. Exactly this changes:</Box>
                    <ul>
                        {changes.setDefault && <li><strong>scheduler.compute_node_ami</strong> becomes {built} (the compute OS stays {this.state.computeNodeOs})</li>}
                        {changes.queueProfiles.map(name => <li key={name}>queue profile <strong>{name}</strong>: instance_ami becomes {built}</li>)}
                        {nothing && <li>nothing references {row.image_id}; there is nothing to repoint</li>}
                    </ul>
                    <Box>Running jobs are unaffected; new launches use the built image.</Box>
                    {this.state.adoptError && <StatusIndicator type="error">{this.state.adoptError}</StatusIndicator>}
                </SpaceBetween>
            </Modal>
        )
    }

    renderSetDefaultDialog() {
        const row = this.state.defaultDialog
        if (!row) {
            return null
        }
        return (
            <Modal
                visible={true}
                onDismiss={this.closeSetDefault}
                header="Set as scheduler default"
                footer={
                    <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" onClick={this.closeSetDefault} disabled={this.state.submitting}>Cancel</Button>
                            <Button variant="primary" onClick={this.submitSetDefault} loading={this.state.submitting}>Set as default</Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                This sets <strong>scheduler.compute_node_ami</strong> to {row.image_id}, a {row.base_os} image like the current default. Jobs that do not name an AMI use it from their next launch. Queue profiles with their own instance_ami are not changed.
            </Modal>
        )
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
                        text: 'Scale-Out Computing',
                        href: '#/soca/active-jobs'
                    },
                    {
                        text: 'Custom AMIs',
                        href: ''
                    }
                ]}
                content={
                    <SpaceBetween size="l">
                        <Container header={
                            <Header
                                variant="h1"
                                description="What this cluster launches from today, per base OS and architecture, and whether it has been built. A build pre-installs the node software, so a launch takes minutes instead of about 15."
                                actions={<Button iconName="refresh" onClick={this.load} loading={this.state.loading}>Refresh</Button>}
                            >
                                Custom AMIs
                            </Header>
                        }>
                            <Box>Builds started here run on the module hosts and keep going if you leave the page. Rows that are building refresh every 30 seconds.</Box>
                        </Container>
                        {this.renderTable('compute', this.visibleComputeRows(), 'Compute images', 'Images jobs run on: the scheduler default and the queue profiles that name an image, one row per base OS and architecture. Combinations with neither an image nor a build are hidden; Add image starts one.', this.state.computeError, this.renderAddImageButton())}
                        {this.state.vdcDeployed && this.renderTable('desktop', this.state.desktop, 'Desktop images', 'Images the ss-base-* software stacks launch desktops from.', this.state.desktopError, <Button data-testid="build-all" onClick={this.openBuildAll}>Build all desktop images</Button>)}
                        {this.renderBuildDialog()}
                        {this.renderBuildAllDialog()}
                        {this.renderAdoptDialog()}
                        {this.renderSetDefaultDialog()}
                    </SpaceBetween>
                }/>
        )
    }
}

export default withRouter(HpcCustomAmis)
