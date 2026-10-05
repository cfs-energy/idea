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

import React, {Component, useCallback, useEffect, useMemo, useRef, useState} from "react";

import {PathRouteProps} from "react-router-dom";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {AppContext} from "../../common";
import {Constants} from "../../common/constants";
import Utils from "../../common/utils";
import {withRouter} from "../../navigation/navigation-utils";
import {hasAccess} from "../../navigation/task-navigation";
import {
    GetImageScheduleResponse,
    ImageBuildRecord,
    ImageInventoryRow,
    ImageKind,
    ImageRefreshResult,
    ImageRowFilter,
    ImageRowKey,
    ImageRowStatus,
    RefreshImagesRequest
} from "../../client/data-model";
import {
    Box,
    Button,
    Checkbox,
    ColumnLayout,
    Container,
    FormField,
    Header,
    Input,
    Link,
    Modal,
    Multiselect,
    MultiselectProps,
    Popover,
    Select,
    SpaceBetween,
    StatusIndicator,
    StatusIndicatorProps,
    Table,
    Tabs,
    TextFilter,
    Toggle
} from "@cloudscape-design/components";

export interface HpcCustomAmisProps extends PathRouteProps, IdeaAppLayoutProps, IdeaSideNavigationProps {

}

const POLL_INTERVAL_MS = 30000
// ponytail: fixed estimate per image at the default of 4 bakes at a time; read image_pipeline.max_concurrent_bakes if it drifts
const MINUTES_PER_IMAGE = 45
const CONCURRENT_BAKES = 4

const IN_FLIGHT: ImageRowStatus[] = ['queued', 'resolving', 'building', 'checking', 'test_launching', 'promoting', 'waiting_capacity']
const BAKE_STEPS: ImageRowStatus[] = ['resolving', 'building', 'checking', 'test_launching', 'promoting']
const BAKE_STEP_LABEL: Record<string, string> = {
    resolving: 'finding the newest vendor image',
    building: 'building',
    checking: 'checking',
    test_launching: 'test-launching',
    promoting: 'switching new launches'
}

/** The status filter offers the groups an operator thinks in, each standing for the row statuses it covers. */
const STATUS_GROUPS: Record<string, ImageRowStatus[]> = {
    'Current': ['current'],
    'Baking': ['queued', 'resolving', 'building', 'checking', 'test_launching', 'promoting'],
    'Failed': ['failed'],
    'Waiting for capacity': ['waiting_capacity'],
    'Pinned': ['pinned'],
    'Unsupported in this region': ['unsupported']
}
const VARIANT_LABEL: Record<string, string> = {cpu: 'CPU', nvidia: 'GPU (NVIDIA)', amd: 'GPU (AMD)'}
const KIND_LABEL: Record<ImageKind, string> = {desktop: 'Desktop', compute: 'Compute'}

const variantOf = (row: ImageBuildRecord) => row.variant ?? 'cpu'
const familyOf = (row: ImageBuildRecord) => (row.base_os ?? '').replace(/\d.*$/, '')
const rowKey = (row: ImageBuildRecord) => `${row.kind}/${row.base_os}/${row.architecture}/${variantOf(row)}`
const keyOf = (row: ImageBuildRecord): ImageRowKey => ({kind: row.kind, base_os: row.base_os, architecture: row.architecture, variant: variantOf(row)})
const isInFlight = (row: ImageBuildRecord) => IN_FLIGHT.includes(row.status as ImageRowStatus)
const isPinned = (row: ImageBuildRecord) => !isInFlight(row) && (row.pinned === true || row.status === 'pinned')

const formatDate = (value?: string): string => {
    if (!value) {
        return '-'
    }
    const date = new Date(value)
    return isNaN(date.getTime()) ? value : date.toLocaleString()
}

const formatDay = (value?: string): string => {
    const date = value ? new Date(value) : undefined
    return date && !isNaN(date.getTime()) ? date.toLocaleDateString() : '-'
}

const formatHour = (hour?: number) => `${String(hour ?? 2).padStart(2, '0')}:00`

const capitalize = (text: string) => text.replace(/\b\w/g, letter => letter.toUpperCase())

const statusGroup = (row: ImageBuildRecord): string =>
    isPinned(row) ? 'Pinned' : Object.keys(STATUS_GROUPS).find(group => STATUS_GROUPS[group].includes(row.status as ImageRowStatus)) ?? row.status ?? '-'

/** One status per row, in the words the service desk reads it. */
export function describeStatus(row: ImageBuildRecord): { type: StatusIndicatorProps.Type, label: string } {
    if (isPinned(row)) {
        return {type: 'stopped', label: `Pinned${row.current_image_id ? ` to ${row.current_image_id}` : ''}`}
    }
    const step = BAKE_STEPS.indexOf(row.status as ImageRowStatus)
    if (step >= 0) {
        return {type: 'in-progress', label: `Baking: ${BAKE_STEP_LABEL[row.status!]} (step ${step + 1} of ${BAKE_STEPS.length})`}
    }
    switch (row.status) {
        case 'current':
            return {type: 'success', label: `Current – validated ${formatDay(row.validated_on)}${row.release ? ` (release ${row.release})` : ''}`}
        case 'queued':
            return {type: 'pending', label: 'Baking: queued'}
        case 'failed':
            return {type: 'error', label: `Failed: ${row.error ?? 'no reason recorded'} – still on ${row.current_image_id ?? 'no image'}`}
        case 'waiting_capacity':
            return {type: 'pending', label: `Waiting for capacity – retry ${formatDate(row.retry_after)}`}
        case 'unsupported':
            return {type: 'stopped', label: 'Unsupported in this region'}
        default:
            return {type: 'info', label: row.status ?? 'Never built'}
    }
}

type Filters = { kind: string[], family: string[], architecture: string[], variant: string[], status: string[] }
const NO_FILTERS: Filters = {kind: [], family: [], architecture: [], variant: [], status: []}

const matches = (row: ImageBuildRecord, filters: Filters, text: string): boolean => {
    const within = (selected: string[], value: string) => selected.length === 0 || selected.includes(value)
    const needle = text.trim().toLowerCase()
    return within(filters.kind, row.kind ?? '')
        && within(filters.family, familyOf(row))
        && within(filters.architecture, row.architecture ?? '')
        && within(filters.variant, variantOf(row))
        && within(filters.status, statusGroup(row))
        && (!needle || [row.base_os, row.architecture, VARIANT_LABEL[variantOf(row)], row.kind, row.current_image_id, row.previous_image_id, describeStatus(row).label]
            .some(value => (value ?? '').toLowerCase().includes(needle)))
}

/**
 * The table filters as one ImageRowFilter, so the service picks the rows itself, or undefined when
 * they cannot be said that way (free text, or two values in a single-value field): then the visible keys go.
 */
export function toRowFilter(filters: Filters, text: string): ImageRowFilter | undefined {
    if (text.trim() || [filters.kind, filters.family, filters.architecture, filters.variant].some(values => values.length > 1)) {
        return undefined
    }
    return {
        kind: filters.kind[0] as ImageKind | undefined,
        base_os_family: filters.family[0],
        architecture: filters.architecture[0],
        variant: filters.variant[0] as ImageRowFilter['variant'],
        statuses: filters.status.length > 0 ? filters.status.flatMap(group => STATUS_GROUPS[group] ?? []) : undefined
    }
}

type Refresh = { title: string, count: number, force?: boolean, request: { all: true } | { rows: ImageBuildRecord[] } | { filter: ImageRowFilter } }

const options = (values: string[], label: (value: string) => string = value => value): MultiselectProps.Option[] =>
    Array.from(new Set(values)).filter(value => !!value).sort().map(value => ({value, label: label(value)}))

// Custom images: builds an admin starts by hand, outside the managed refresh/promote pipeline.

const SCHEDULER_DEFAULT_REFERENCE = 'scheduler default'
const QUEUE_PROFILE_REFERENCE = 'queue profile: '
const DEFAULT_INSTANCE_TYPE: Record<ImageKind, string> = {compute: 'c7i.large', desktop: 'm7i.large'}
/** Both modules default an arm64 build to the same builder size, whatever the kind. */
const DEFAULT_ARM64_INSTANCE_TYPE = 'm8g.large'
const DEFAULT_ARCHITECTURE = 'x86_64'
const COMPUTE_ARCHITECTURES = ['x86_64', 'arm64']

/** The queue profiles named in a row's Referenced by column, as the service wrote them. */
const referencedQueueProfiles = (row: ImageInventoryRow): string[] =>
    (row.referenced_by ?? [])
        .filter(reference => reference.startsWith(QUEUE_PROFILE_REFERENCE))
        .map(reference => reference.slice(QUEUE_PROFILE_REFERENCE.length))

/** The image a completed custom build produced, when the row does not use it yet. */
const builtImage = (row: ImageInventoryRow): string | undefined => {
    const build = row.last_build
    return build?.status === 'complete' && build.image_id && build.image_id !== row.image_id ? build.image_id : undefined
}

/** The image Set as default would write: the completed custom build if there is one, else the row's image. */
const defaultCandidate = (row: ImageInventoryRow): string | undefined => builtImage(row) ?? row.image_id

/** Joins the parts of a cell that do not fit, for display in a popover. */
const joinDetail = (parts: (string | undefined)[]): string => parts.filter(part => !!part).join('. ')

interface CustomImagesProps {
    compute: boolean
    desktop: boolean
    flash: (content: React.ReactNode, type: 'success' | 'info' | 'warning' | 'error') => void
}

interface CustomImagesState {
    compute: ImageInventoryRow[]
    desktop: ImageInventoryRow[]
    computeError?: string
    desktopError?: string
    loading: boolean
    // No row means Add image mode, where the base OS comes from the addOs picker.
    buildDialog?: { kind: ImageKind, row?: ImageInventoryRow }
    baseAmi: string
    instanceType: string
    efa: boolean
    fsxLustre: boolean
    submitting: boolean
    defaultDialog?: ImageInventoryRow
    supportedBaseOs: string[]
    addOs: string
    addArchitecture: string
    computeNodeOs?: string
}

export class CustomImages extends Component<CustomImagesProps, CustomImagesState> {

    private pollTimer?: ReturnType<typeof setInterval>

    constructor(props: CustomImagesProps) {
        super(props)
        this.state = {
            compute: [], desktop: [], loading: true, baseAmi: '', instanceType: '', efa: true, fsxLustre: true,
            submitting: false, supportedBaseOs: [], addOs: '', addArchitecture: DEFAULT_ARCHITECTURE
        }
    }

    componentDidMount() {
        this.load()
    }

    componentWillUnmount() {
        this.stopPolling()
    }

    /** The compute rows worth showing: a combination with no image and no build stays hidden. */
    visibleComputeRows(): ImageInventoryRow[] {
        return this.state.compute.filter(row => row.state !== 'none' || !!row.last_build)
    }

    /** The supported (base OS, architecture) pairs with no visible row, OSes with no image at all first. */
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

    missingArchitectures(base_os: string): string[] {
        return this.missingComputeCombinations().filter(row => row.base_os === base_os).map(row => row.architecture)
    }

    load = () => {
        this.setState({loading: true})
        const clients = AppContext.get().client()
        // Each table fetches and fails on its own, so a controller error does not blank the compute rows.
        const compute = this.props.compute
            ? clients.schedulerAdmin().listComputeImages({}).then(result => {
                this.setState({compute: result.listing ?? [], supportedBaseOs: result.supported_base_os ?? [], computeNodeOs: result.compute_node_os, computeError: undefined})
            }).catch(error => this.setState({computeError: `Failed to list compute images: ${error.message}`}))
            : Promise.resolve()
        const desktop = this.props.desktop
            ? clients.virtualDesktopAdmin().listDesktopImages({}).then(result => {
                this.setState({desktop: result.listing ?? [], desktopError: undefined})
            }).catch(error => this.setState({desktopError: `Failed to list desktop images: ${error.message}`}))
            : Promise.resolve()
        Promise.all([compute, desktop]).then(() => this.setState({loading: false}, this.syncPolling))
    }

    private syncPolling = () => {
        const building = [...this.state.compute, ...this.state.desktop].some(row => row.state === 'building' || row.last_build?.status === 'building')
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

    openBuild = (kind: ImageKind, row?: ImageInventoryRow) => {
        const first = row ? undefined : this.missingComputeCombinations()[0]
        this.setState({
            buildDialog: {kind, row},
            addOs: first?.base_os ?? '',
            addArchitecture: first?.architecture ?? DEFAULT_ARCHITECTURE,
            baseAmi: '', instanceType: '', efa: true, fsxLustre: true, submitting: false
        })
    }

    closeBuild = () => this.setState({buildDialog: undefined, submitting: false})

    submitBuild = () => {
        const dialog = this.state.buildDialog
        const baseOs = dialog?.row?.base_os ?? this.state.addOs
        if (!dialog || !baseOs) {
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
                base_os: baseOs, architecture, base_ami: baseAmi, instance_type: instanceType,
                enable_drivers: [...(this.state.efa ? ['efa'] : []), ...(this.state.fsxLustre ? ['fsx_lustre'] : [])]
            }).then(result => result.record)
            // a custom desktop build never repoints a base stack: only a validated managed image does that
            : clients.virtualDesktopAdmin().buildDesktopImage({
                base_os: baseOs, architecture, base_ami: baseAmi, instance_type: instanceType, update_stack: false
            }).then(result => result.record)
        started.then((record?: ImageBuildRecord) => {
            this.closeBuild()
            this.props.flash(`Build started for ${baseOs} (${architecture}): ${record?.ami_name ?? ''} from ${record?.base_ami ?? 'the stock image'}. This takes about 20 minutes; the row refreshes on its own.`, 'success')
            this.load()
        }).catch(error => {
            this.setState({submitting: false})
            this.props.flash(`Build failed to start: ${error.message}`, 'error')
        })
    }

    // Set as scheduler default

    submitSetDefault = () => {
        const row = this.state.defaultDialog
        const image = row ? defaultCandidate(row) : undefined
        if (!row || !image) {
            return
        }
        const moduleId = Utils.asString(AppContext.get().getClusterSettingsService().getModuleId(Constants.MODULE_SCHEDULER), Constants.MODULE_SCHEDULER)
        this.setState({submitting: true})
        AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId, settings: {compute_node_ami: image}}).then(result => {
            this.setState({defaultDialog: undefined, submitting: false})
            if (Utils.asBoolean(result.success, false)) {
                this.props.flash(`scheduler.compute_node_ami is now ${image} (${row.base_os}). Jobs without an explicit AMI use it from the next launch.`, 'success')
            } else {
                this.props.flash('Failed to update the scheduler default image', 'error')
            }
            this.load()
        }).catch(error => {
            this.setState({submitting: false})
            this.props.flash(`Failed to update the scheduler default image: ${error.message}`, 'error')
        })
    }

    // Rendering

    /** The architecture the scheduler default runs: its single AMI can hold no other. */
    schedulerDefaultArchitecture(): string {
        const row = this.state.compute.find(candidate => (candidate.referenced_by ?? []).includes(SCHEDULER_DEFAULT_REFERENCE))
        return row?.architecture ?? DEFAULT_ARCHITECTURE
    }

    architectureMismatch(kind: ImageKind, row: ImageInventoryRow): boolean {
        return kind === 'compute' && (row.architecture ?? DEFAULT_ARCHITECTURE) !== this.schedulerDefaultArchitecture()
    }

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
                // A row with no image but a completed build has an image to use, so it is idle rather than absent.
                return <StatusIndicator type="stopped">{row.last_build?.status === 'complete' ? 'Not in use' : 'None'}</StatusIndicator>
        }
    }

    renderState(kind: ImageKind, row: ImageInventoryRow) {
        const indicator = this.stateIndicator(row)
        const note = this.architectureMismatch(kind, row) && referencedQueueProfiles(row).length === 0
            ? `Scheduler default runs ${this.schedulerDefaultArchitecture()}. Assign this image to a queue profile with ${row.architecture ?? DEFAULT_ARCHITECTURE} instance types.`
            : undefined
        return note
            ? <Popover dismissButton={false} position="top" size="small" triggerType="text" content={note}>{indicator}</Popover>
            : indicator
    }

    renderLastBuild(row: ImageInventoryRow) {
        const build = row.last_build
        if (!build) {
            return '-'
        }
        const type = build.status === 'complete' ? 'success' : build.status === 'failed' ? 'error' : build.status === 'skipped' ? 'stopped' : 'in-progress'
        const label = build.status === 'complete' ? 'Complete' : build.status === 'failed' ? 'Failed' : build.status === 'skipped' ? 'Skipped' : 'Building'
        const when = build.status === 'building' ? `started ${formatDate(build.started_on)}` : formatDate(build.finished_on)
        const detail = joinDetail([
            build.instance_id ? `builder ${build.instance_id}` : undefined,
            build.requested_by ? `requested by ${build.requested_by}` : undefined
        ])
        const indicator = <StatusIndicator type={type}>{label}</StatusIndicator>
        const built = builtImage(row)
        return (
            <SpaceBetween size="xxs">
                <Box>
                    {detail ? <Popover dismissButton={false} position="top" size="small" triggerType="custom" content={detail}>{indicator}</Popover> : indicator}
                    {' '}
                    <Box variant="span" fontSize="body-s" color="text-body-secondary">{when}</Box>
                </Box>
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
        const secondary = row.build_date ? `built ${formatDay(row.build_date)}` : (row.image_name ?? '')
        const detail = joinDetail([row.base_ami_id ? `base: ${row.base_ami_id}` : undefined, row.build_date ? row.image_name : undefined, row.notes])
        return (
            <SpaceBetween size="xxs">
                <Box>{detail ? <Popover dismissButton={false} position="top" size="small" triggerType="text" content={detail}>{row.image_id}</Popover> : row.image_id}</Box>
                {secondary !== '' && <Box fontSize="body-s" color="text-body-secondary">{secondary}</Box>}
            </SpaceBetween>
        )
    }

    renderActions(kind: ImageKind, row: ImageInventoryRow) {
        const candidate = defaultCandidate(row)
        const isDefault = (row.referenced_by ?? []).includes(SCHEDULER_DEFAULT_REFERENCE) && candidate === row.image_id
        // The scheduler default never crosses operating systems or architectures.
        const canSetDefault = kind === 'compute'
            && !this.architectureMismatch(kind, row)
            && !!candidate
            && !!this.state.computeNodeOs
            && row.base_os === this.state.computeNodeOs
            && !isDefault
        return (
            <SpaceBetween direction="horizontal" size="xs">
                <Button variant="inline-link" disabled={row.state === 'building'} onClick={() => this.openBuild(kind, row)}>Build</Button>
                {canSetDefault && <Button variant="inline-link" onClick={() => this.setState({defaultDialog: row, submitting: false})}>Set as default</Button>}
            </SpaceBetween>
        )
    }

    renderTable(kind: ImageKind, rows: ImageInventoryRow[], title: string, description: string, error?: string, headerActions?: React.ReactNode) {
        return (
            <Table
                variant="container"
                resizableColumns={true}
                wrapLines={true}
                loading={this.state.loading && rows.length === 0}
                items={rows}
                empty={error ? <StatusIndicator type="error">{error}</StatusIndicator> : <Box textAlign="center">No images</Box>}
                header={
                    <SpaceBetween size="xs">
                        <Header variant="h2" description={description} actions={headerActions}>{title}</Header>
                        {error && rows.length > 0 && <StatusIndicator type="error">{error} (showing the last successful listing)</StatusIndicator>}
                    </SpaceBetween>
                }
                columnDefinitions={[
                    {id: 'base_os', header: 'Base OS', minWidth: 180, cell: row => (
                        <SpaceBetween size="xxs">
                            <Box>{row.base_os}</Box>
                            <Box fontSize="body-s" color="text-body-secondary">{row.architecture ?? '-'}</Box>
                        </SpaceBetween>
                    )},
                    {id: 'image', header: 'Image', cell: row => this.renderImage(row), minWidth: 180},
                    {id: 'state', header: 'State', cell: row => this.renderState(kind, row), minWidth: 150},
                    {id: 'referenced_by', header: 'Referenced by', minWidth: 150, cell: row => (row.referenced_by ?? []).length === 0 ? '-' : (
                        <SpaceBetween size="xxs">{(row.referenced_by ?? []).map(ref => <Box key={ref} fontSize="body-s">{ref}</Box>)}</SpaceBetween>
                    )},
                    {id: 'last_build', header: 'Last custom build', cell: row => this.renderLastBuild(row), minWidth: 190},
                    {id: 'actions', header: 'Actions', cell: row => this.renderActions(kind, row), minWidth: 150}
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
                header={dialog.row ? `Build custom ${isCompute ? 'compute' : 'desktop'} image: ${dialog.row.base_os} (${dialog.row.architecture})` : 'Add compute image'}
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
                        This launches a builder instance from a {baseOs} {architecture} image, installs everything a {isCompute ? 'compute node' : 'desktop'} needs, snapshots it and terminates the builder. About 20 minutes and one instance hour.
                        {isCompute
                            ? ' The new image is not used until you click Set as default or point a queue profile at it.'
                            : ' No software stack is changed. To use the image for a project, create a software stack from it.'}
                        {' '}A custom build is not validated and the managed refresh never touches it.
                    </Box>
                    <FormField label="Base AMI" description="Leave empty to use the newest stock image the vendor publishes for this OS.">
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
                </SpaceBetween>
            </Modal>
        )
    }

    renderSetDefaultDialog() {
        const row = this.state.defaultDialog
        if (!row) {
            return null
        }
        const close = () => this.setState({defaultDialog: undefined, submitting: false})
        return (
            <Modal
                visible={true}
                onDismiss={close}
                header="Set as scheduler default"
                footer={
                    <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" onClick={close} disabled={this.state.submitting}>Cancel</Button>
                            <Button variant="primary" onClick={this.submitSetDefault} loading={this.state.submitting}>Set as default</Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                This sets <strong>scheduler.compute_node_ami</strong> to {defaultCandidate(row)}, a {row.base_os} image like the current default. Jobs that do not name an AMI use it from their next launch. Queue profiles with their own instance_ami are not changed.
            </Modal>
        )
    }

    render() {
        const missing = this.missingComputeCombinations()
        const addImage = (
            <Button data-testid="add-image" disabled={missing.length === 0} onClick={() => this.openBuild('compute')}>Add image</Button>
        )
        return (
            <SpaceBetween size="l">
                {this.props.compute && this.renderTable('compute', this.visibleComputeRows(), 'Compute images',
                    'Images jobs run on: the scheduler default and the queue profiles that name an image. Add image builds one for an OS or architecture that has none.',
                    this.state.computeError,
                    missing.length === 0 ? <span title="Every supported base OS and architecture already has a compute image.">{addImage}</span> : addImage)}
                {this.props.desktop && this.renderTable('desktop', this.state.desktop, 'Desktop images',
                    'Builds from the base OS images, for project software stacks. A build here does not change any stack.', this.state.desktopError)}
                {this.renderBuildDialog()}
                {this.renderSetDefaultDialog()}
            </SpaceBetween>
        )
    }
}

function HpcCustomAmis(props: HpcCustomAmisProps) {
    const context = AppContext.get()
    const clients = context.client()
    const kinds = useMemo(() => {
        const allowed: ImageKind[] = []
        if (hasAccess(context, 'desktop-admin')) allowed.push('desktop')
        if (hasAccess(context, 'jobs-admin')) allowed.push('compute')
        return allowed
    }, [context])
    const api = useCallback((kind: ImageKind) => kind === 'desktop' ? clients.virtualDesktopAdmin() : clients.schedulerAdmin(), [clients])
    // the API takes force only from a cluster administrator (not a manager)
    const canForce = context.auth().getGroups().includes('administrators-cluster-group')

    const [rows, setRows] = useState<ImageBuildRecord[]>([])
    const [errors, setErrors] = useState<string[]>([])
    const [loading, setLoading] = useState(true)
    const [selected, setSelected] = useState<ImageBuildRecord[]>([])
    const [filters, setFilters] = useState<Filters>(NO_FILTERS)
    const [text, setText] = useState('')
    const [refresh, setRefresh] = useState<Refresh>()
    const [rollback, setRollback] = useState<ImageBuildRecord>()
    const [detail, setDetail] = useState<ImageBuildRecord>()
    const [submitting, setSubmitting] = useState(false)
    const [schedule, setSchedule] = useState<GetImageScheduleResponse>()
    const [timezone, setTimezone] = useState<string>()
    const [scheduleEdit, setScheduleEdit] = useState<{ enabled: boolean, hour: number }>()
    const pollTimer = useRef<ReturnType<typeof setInterval>>(undefined)

    const flash = (content: React.ReactNode, type: 'success' | 'info' | 'warning' | 'error') =>
        props.onFlashbarChange({items: [{type, content, dismissible: true}]})

    const load = useCallback(() => {
        setLoading(true)
        // Each kind fetches and fails on its own, so a controller error does not blank the compute rows.
        Promise.allSettled(kinds.map(kind => api(kind).listImageRows({}).then(result =>
            (result.listing ?? []).map(row => ({...row, kind: row.kind ?? kind})))))
            .then(results => {
                const failed = results.flatMap((result, i) => result.status === 'rejected' ? [`Failed to list ${kinds[i]} images: ${result.reason?.message}`] : [])
                setErrors(failed)
                setRows(previous => results.flatMap((result, i) => result.status === 'fulfilled'
                    ? result.value
                    : previous.filter(row => row.kind === kinds[i])))
                setLoading(false)
            })
    }, [kinds, api])

    const loadSchedule = useCallback(() => {
        // one schedule runs both kinds; a compute-only admin reads it from the scheduler
        const request = kinds.includes('desktop')
            ? clients.virtualDesktopAdmin().getImageSchedule({})
            : kinds.includes('compute') ? clients.schedulerAdmin().getImageSchedule({}) : undefined
        request?.then(setSchedule).catch(() => setSchedule(undefined))
    }, [kinds, clients])

    useEffect(() => {
        load()
        loadSchedule()
        // the settings service throws before its first load, so the call rides inside the promise
        Promise.resolve().then(() => context.getClusterSettingsService().getClusterSettings())
            .then(settings => setTimezone(settings?.timezone))
            .catch(() => undefined)
    }, [load, loadSchedule, context])

    // Poll only while a row is in flight, and stop once every row is idle.
    const busy = rows.some(isInFlight)
    useEffect(() => {
        if (!busy) {
            return
        }
        pollTimer.current = setInterval(load, POLL_INTERVAL_MS)
        return () => clearInterval(pollTimer.current)
    }, [busy, load])

    const visible = useMemo(() => rows.filter(row => matches(row, filters, text)), [rows, filters, text])
    const visibleKeys = new Set(visible.map(rowKey))
    const selectedVisible = selected.filter(row => visibleKeys.has(rowKey(row)))
    const filtered = text.trim() !== '' || Object.values(filters).some(values => values.length > 0)

    // Refresh and validate

    const openRefreshAll = () => {
        if (!filtered) {
            setRefresh({title: 'Refresh and validate all images', count: rows.length, request: {all: true}})
            return
        }
        const filter = toRowFilter(filters, text)
        setRefresh({
            title: `Refresh and validate ${visible.length} shown image${visible.length === 1 ? '' : 's'}`,
            count: visible.length,
            request: filter ? {filter} : {rows: visible}
        })
    }

    const openRefreshRows = (targets: ImageBuildRecord[], title: string, force = false) =>
        setRefresh({title, count: targets.length, force, request: {rows: targets}})

    const openForceRebake = (targets: ImageBuildRecord[]) => openRefreshRows(targets, targets.length === 1
        ? `Force rebake ${targets[0].base_os} ${targets[0].architecture} ${VARIANT_LABEL[variantOf(targets[0])]}`
        : `Force rebake ${targets.length} selected images`, true)

    const submitRefresh = () => {
        if (!refresh) {
            return
        }
        const request = refresh.request
        const force = refresh.force ? {force: true} : {}
        const calls: { kind: ImageKind, req: RefreshImagesRequest }[] = ('rows' in request
            ? kinds.map(kind => ({kind, req: {rows: request.rows.filter(row => row.kind === kind).map(keyOf)}})).filter(call => call.req.rows!.length > 0)
            : 'filter' in request
                ? kinds.filter(kind => !request.filter.kind || request.filter.kind === kind).map(kind => ({kind, req: {filter: {...request.filter, kind}}}))
                : kinds.map(kind => ({kind, req: {all: true}})))
            .map(call => ({kind: call.kind, req: {...call.req, ...force}}))
        setSubmitting(true)
        Promise.allSettled(calls.map(call => api(call.kind).refreshImages(call.req))).then(settled => {
            const results: ImageRefreshResult[] = settled.flatMap(result => result.status === 'fulfilled' ? result.value.results ?? [] : [])
            const failures = settled.flatMap((result, i) => result.status === 'rejected' ? [`${calls[i].kind}: ${result.reason?.message}`] : [])
            const count = (outcome: string) => results.filter(result => result.outcome === outcome).length
            const bakedToday = results.filter(result => result.outcome === 'baked_today')
                .map(result => `${result.row?.base_os} ${result.row?.architecture}`)
            const skipped = results.length - count('queued') - count('in_flight') - bakedToday.length
            const problems = [
                ...results.filter(result => result.outcome === 'error' || result.outcome === 'not_found')
                    .map(result => `${result.row?.base_os} ${result.row?.architecture}: ${result.message ?? result.outcome}`),
                ...failures
            ]
            const message = `Queued ${count('queued')}, already in progress ${count('in_flight')}`
                + (skipped > 0 ? `, skipped ${skipped} (pinned, unsupported or not found)` : '')
                + '.'
                + (bakedToday.length > 0 ? ` Already updated today, skipped: ${bakedToday.join(', ')}. An image is baked at most once a day; Force rebake bakes it again.` : '')
                + (problems.length > 0 ? ` Not started: ${problems.join('; ')}.` : '')
            setRefresh(undefined)
            setSubmitting(false)
            setSelected([])
            flash(message, problems.length > 0 ? 'warning' : 'success')
            load()
        })
    }

    // Row actions

    const submitRollback = () => {
        if (!rollback) {
            return
        }
        setSubmitting(true)
        api(rollback.kind!).rollbackImage({row: keyOf(rollback)}).then(() => {
            flash(`${rollback.base_os} ${rollback.architecture}: new launches use ${rollback.previous_image_id}. Automatic updates are paused for this image until the next manual refresh.`, 'success')
            setRollback(undefined)
            load()
        }).catch(error => {
            flash(`Roll back failed: ${error.message}`, 'error')
        }).finally(() => setSubmitting(false))
    }

    const togglePin = (row: ImageBuildRecord) => {
        const pinned = !isPinned(row)
        api(row.kind!).setImagePinned({row: keyOf(row), pinned}).then(() => {
            flash(pinned
                ? `${row.base_os} ${row.architecture} is pinned: it is not rebuilt or switched until you unpin it.`
                : `${row.base_os} ${row.architecture} is unpinned: automatic updates resume.`, 'success')
            load()
        }).catch(error => flash(`${pinned ? 'Pin' : 'Unpin'} failed: ${error.message}`, 'error'))
    }

    // Schedule

    const submitSchedule = () => {
        if (!scheduleEdit) {
            return
        }
        setSubmitting(true)
        clients.virtualDesktopAdmin().updateImageSchedule({
            schedule: {enabled: scheduleEdit.enabled, day: schedule?.schedule?.day ?? 'first sunday', hour: scheduleEdit.hour}
        }).then(() => {
            setScheduleEdit(undefined)
            loadSchedule()
        }).catch(error => flash(`Schedule was not saved: ${error.message}`, 'error'))
            .finally(() => setSubmitting(false))
    }

    // Rendering

    const renderStatus = (row: ImageBuildRecord) => {
        const status = describeStatus(row)
        return (
            <SpaceBetween size="xxs">
                <StatusIndicator type={status.type}>{status.label}</StatusIndicator>
                {row.status === 'failed' && row.log_link && <Link href={row.log_link} external={true} fontSize="body-s">View log</Link>}
                {row.rollback_hold && <Box fontSize="body-s" color="text-body-secondary">Rolled back: automatic updates paused</Box>}
            </SpaceBetween>
        )
    }

    const renderActions = (row: ImageBuildRecord) => {
        const inFlight = isInFlight(row)
        const pinned = isPinned(row)
        return (
            <SpaceBetween direction="horizontal" size="xs">
                <Button variant="inline-link" disabled={inFlight || pinned || row.status === 'unsupported'}
                        onClick={() => openRefreshRows([row], `Rebuild ${row.base_os} ${row.architecture} ${VARIANT_LABEL[variantOf(row)]}`)}>Rebuild</Button>
                {canForce && <Button variant="inline-link" disabled={inFlight || pinned || row.status === 'unsupported'}
                                     onClick={() => openForceRebake([row])}>Force rebake</Button>}
                <Button variant="inline-link" disabled={inFlight || !row.previous_image_id} onClick={() => setRollback(row)}>Roll back</Button>
                <Button variant="inline-link" disabled={inFlight} onClick={() => togglePin(row)}>{pinned ? 'Unpin' : 'Pin'}</Button>
                <Button variant="inline-link" onClick={() => setDetail(row)}>Details</Button>
            </SpaceBetween>
        )
    }

    const filterSelect = (field: keyof Filters, placeholder: string, values: MultiselectProps.Option[]) => (
        <Multiselect
            placeholder={placeholder}
            ariaLabel={placeholder}
            selectedOptions={values.filter(option => filters[field].includes(option.value!))}
            options={values}
            onChange={event => setFilters({...filters, [field]: event.detail.selectedOptions.map(option => option.value!)})}
        />
    )

    const scheduleLine = () => {
        const rule = schedule?.schedule
        const zone = timezone ? ` (${timezone})` : ''
        const when = rule?.enabled === false
            ? 'off'
            : `${capitalize(rule?.day ?? 'first sunday')} of each month at ${formatHour(rule?.hour)}${zone}`
        return `Checks for new vendor images: ${when} · last run ${formatDate(schedule?.last_run_on)} · next run ${rule?.enabled === false ? '-' : formatDate(schedule?.next_run_on)}`
    }

    const renderRefreshModal = () => refresh && (
        <Modal
            visible={true}
            onDismiss={() => setRefresh(undefined)}
            header={refresh.title}
            footer={
                <Box float="right">
                    <SpaceBetween direction="horizontal" size="xs">
                        <Button variant="link" onClick={() => setRefresh(undefined)} disabled={submitting}>Cancel</Button>
                        <Button variant="primary" onClick={submitRefresh} loading={submitting}>{refresh.force ? 'Force rebake' : 'Refresh and validate'}</Button>
                    </SpaceBetween>
                </Box>
            }
        >
            <SpaceBetween size="s">
                <Box>For each of the {refresh.count} image{refresh.count === 1 ? '' : 's'}, this:</Box>
                <ul>
                    <li>builds a new image from the newest vendor image;</li>
                    <li>test-launches a desktop or a job from it;</li>
                    <li>switches new desktops and jobs to it only if every check passes. If a check fails, the image stays on what it has now.</li>
                </ul>
                <Box>{refresh.force
                    ? 'An image is normally baked at most once a day. Force rebake bakes these again even if they were baked today, at the cost of another bake each.'
                    : 'An image already baked today is skipped; Force rebake bakes it again.'}</Box>
                <Box>Running desktops and jobs are not touched. Rows already in progress are skipped. Expect roughly {Math.ceil(Math.max(refresh.count, 1) / CONCURRENT_BAKES) * MINUTES_PER_IMAGE} minutes.</Box>
            </SpaceBetween>
        </Modal>
    )

    const renderRollbackModal = () => rollback && (
        <Modal
            visible={true}
            onDismiss={() => setRollback(undefined)}
            header={`Roll back ${rollback.base_os} ${rollback.architecture} ${VARIANT_LABEL[variantOf(rollback)]}`}
            footer={
                <Box float="right">
                    <SpaceBetween direction="horizontal" size="xs">
                        <Button variant="link" onClick={() => setRollback(undefined)} disabled={submitting}>Cancel</Button>
                        <Button variant="primary" onClick={submitRollback} loading={submitting}>Roll back</Button>
                    </SpaceBetween>
                </Box>
            }
        >
            <Box>
                New launches switch from {rollback.current_image_id ?? 'the current image'} to the previous validated image, {rollback.previous_image_id}.
                Automatic updates for this image are paused until the next manual refresh. Running desktops and jobs are not touched.
            </Box>
        </Modal>
    )

    const renderDetailModal = () => detail && (
        <Modal visible={true} size="large" onDismiss={() => setDetail(undefined)}
               header={`${detail.base_os} ${detail.architecture} ${VARIANT_LABEL[variantOf(detail)]}`}>
            <SpaceBetween size="m">
                <ColumnLayout columns={3} variant="text-grid">
                    {[
                        ['Vendor image', detail.source_ami ?? detail.base_ami],
                        ['Candidate image', detail.image_id],
                        ['Trigger', detail.trigger],
                        ['Requested by', detail.requested_by],
                        ['Attempts', detail.attempts?.toString()],
                        ['Started', formatDate(detail.started_on)]
                    ].map(([label, value]) => (
                        <div key={label}>
                            <Box variant="awsui-key-label">{label}</Box>
                            <Box>{value ?? '-'}</Box>
                        </div>
                    ))}
                </ColumnLayout>
                <Table
                    variant="embedded"
                    header={<Header variant="h3">Checks</Header>}
                    items={detail.checks ?? []}
                    empty={<Box textAlign="center">No checks have run yet</Box>}
                    columnDefinitions={[
                        {id: 'name', header: 'Check', cell: check => check.name},
                        {id: 'ok', header: 'Result', cell: check => <StatusIndicator type={check.ok ? 'success' : 'error'}>{check.ok ? 'Passed' : 'Failed'}</StatusIndicator>},
                        {id: 'detail', header: 'Detail', cell: check => check.detail ?? '-'},
                        {id: 'seconds', header: 'Seconds', cell: check => check.seconds ?? '-'}
                    ]}
                />
            </SpaceBetween>
        </Modal>
    )

    const renderScheduleModal = () => scheduleEdit && (
        <Modal
            visible={true}
            onDismiss={() => setScheduleEdit(undefined)}
            header="Vendor image check"
            footer={
                <Box float="right">
                    <SpaceBetween direction="horizontal" size="xs">
                        <Button variant="link" onClick={() => setScheduleEdit(undefined)} disabled={submitting}>Cancel</Button>
                        <Button variant="primary" onClick={submitSchedule} loading={submitting}>Save</Button>
                    </SpaceBetween>
                </Box>
            }
        >
            <SpaceBetween size="m">
                <Box>On the {schedule?.schedule?.day ?? 'first sunday'} of each month, images whose vendor published a newer base are rebuilt and validated. Images with no new vendor image are left alone.</Box>
                <Toggle checked={scheduleEdit.enabled} onChange={event => setScheduleEdit({...scheduleEdit, enabled: event.detail.checked})}>
                    Check for new vendor images every month
                </Toggle>
                <FormField label="Hour" description={`Cluster time${timezone ? ` (${timezone})` : ''}.`}>
                    <Select
                        disabled={!scheduleEdit.enabled}
                        selectedOption={{value: String(scheduleEdit.hour), label: formatHour(scheduleEdit.hour)}}
                        options={Array.from({length: 24}, (_, hour) => ({value: String(hour), label: formatHour(hour)}))}
                        onChange={event => setScheduleEdit({...scheduleEdit, hour: Number(event.detail.selectedOption.value)})}
                    />
                </FormField>
            </SpaceBetween>
        </Modal>
    )

    return (
        <IdeaAppLayout
            ideaPageId={props.ideaPageId}
            toolsOpen={props.toolsOpen}
            tools={props.tools}
            onToolsChange={props.onToolsChange}
            onPageChange={props.onPageChange}
            sideNavHeader={props.sideNavHeader}
            sideNavItems={props.sideNavItems}
            onSideNavChange={props.onSideNavChange}
            onFlashbarChange={props.onFlashbarChange}
            flashbarItems={props.flashbarItems}
            breadcrumbItems={[
                {text: 'IDEA', href: '#/'},
                {text: 'Images and applications', href: '#/virtual-desktop/software-stacks'},
                {text: 'Images', href: ''}
            ]}
            content={
                <SpaceBetween size="l">
                    <Header
                        variant="h1"
                        description="Managed images are rebuilt from the newest vendor image and switch new desktops and jobs over only after they pass every check. Custom images are builds you start yourself; the managed refresh never touches them. Running desktops and jobs are never touched."
                    >
                        Images
                    </Header>
                    <Tabs tabs={[
                        {id: 'managed', label: 'Managed images', content: (
                            <SpaceBetween size="l">
                                {kinds.length > 0 && (
                                    <Container>
                                        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                                            <Box data-testid="schedule-line">{scheduleLine()}</Box>
                                            {/* the desktop controller owns the schedule; a compute-only admin reads it */}
                                            {kinds.includes('desktop') && (
                                                <Button variant="inline-link" disabled={!schedule}
                                                        onClick={() => setScheduleEdit({enabled: schedule?.schedule?.enabled !== false, hour: schedule?.schedule?.hour ?? 2})}>Edit</Button>
                                            )}
                                        </SpaceBetween>
                                    </Container>
                                )}
                                {errors.map(error => <StatusIndicator key={error} type="error">{error}</StatusIndicator>)}
                                <Table
                                    variant="container"
                                    resizableColumns={true}
                                    wrapLines={true}
                                    loading={loading && rows.length === 0}
                                    loadingText="Loading images"
                                    items={visible}
                                    trackBy={rowKey}
                                    selectionType="multi"
                                    selectedItems={selectedVisible}
                                    onSelectionChange={event => setSelected(event.detail.selectedItems)}
                                    isItemDisabled={row => isInFlight(row) || isPinned(row) || row.status === 'unsupported'}
                                    ariaLabels={{
                                        selectionGroupLabel: 'Image selection',
                                        itemSelectionLabel: (_, row) => `${row.base_os} ${row.architecture} ${variantOf(row)}`,
                                        allItemsSelectionLabel: () => 'Select all shown images'
                                    }}
                                    empty={<Box textAlign="center">No images</Box>}
                                    header={
                                        <Header
                                            variant="h2"
                                            counter={`(${visible.length})`}
                                            description={busy ? 'Rows in progress refresh every 30 seconds.' : undefined}
                                            actions={
                                                <SpaceBetween direction="horizontal" size="xs">
                                                    <Button iconName="refresh" ariaLabel="Reload" onClick={load} loading={loading}/>
                                                    <Button disabled={selectedVisible.length === 0}
                                                            onClick={() => openRefreshRows(selectedVisible, `Refresh and validate ${selectedVisible.length} selected image${selectedVisible.length === 1 ? '' : 's'}`)}>
                                                        Refresh and validate selected
                                                    </Button>
                                                    {canForce && <Button disabled={selectedVisible.length === 0} onClick={() => openForceRebake(selectedVisible)}>
                                                        Force rebake selected
                                                    </Button>}
                                                    <Button variant="primary" disabled={visible.length === 0} onClick={openRefreshAll}>
                                                        {filtered ? `Refresh and validate shown (${visible.length})` : 'Refresh and validate all'}
                                                    </Button>
                                                </SpaceBetween>
                                            }
                                        >
                                            Images
                                        </Header>
                                    }
                                    filter={
                                        <SpaceBetween size="xs">
                                            <TextFilter filteringText={text} filteringPlaceholder="Find images" filteringAriaLabel="Find images"
                                                        countText={`${visible.length} match${visible.length === 1 ? '' : 'es'}`}
                                                        onChange={event => setText(event.detail.filteringText)}/>
                                            <ColumnLayout columns={5}>
                                                {filterSelect('kind', 'Kind', options(rows.map(row => row.kind ?? ''), value => KIND_LABEL[value as ImageKind] ?? value))}
                                                {filterSelect('family', 'OS family', options(rows.map(familyOf)))}
                                                {filterSelect('architecture', 'Architecture', options(rows.map(row => row.architecture ?? '')))}
                                                {filterSelect('variant', 'GPU variant', options(rows.map(variantOf), value => VARIANT_LABEL[value] ?? value))}
                                                {filterSelect('status', 'Status', Object.keys(STATUS_GROUPS).map(value => ({value, label: value})))}
                                            </ColumnLayout>
                                        </SpaceBetween>
                                    }
                                    columnDefinitions={[
                                        {
                                            id: 'image', header: 'Image', minWidth: 180, cell: row => (
                                                <SpaceBetween size="xxs">
                                                    <Box>{row.base_os}</Box>
                                                    <Box fontSize="body-s" color="text-body-secondary">{row.architecture} · {VARIANT_LABEL[variantOf(row)]}</Box>
                                                </SpaceBetween>
                                            )
                                        },
                                        {id: 'kind', header: 'Kind', cell: row => KIND_LABEL[row.kind!] ?? row.kind, width: 110},
                                        {id: 'status', header: 'Status', cell: renderStatus, minWidth: 240},
                                        {id: 'current', header: 'Current image', cell: row => row.current_image_id ?? '-', minWidth: 150},
                                        {id: 'previous', header: 'Previous image', cell: row => row.previous_image_id ?? '-', minWidth: 150},
                                        {id: 'last_check', header: 'Last check', cell: row => formatDate(row.finished_on ?? row.started_on), minWidth: 150},
                                        {id: 'actions', header: 'Actions', cell: renderActions, minWidth: 260}
                                    ]}
                                />
                            </SpaceBetween>
                        )},
                        {id: 'custom', label: 'Custom images', content: (
                            <CustomImages compute={kinds.includes('compute')} desktop={kinds.includes('desktop')} flash={flash}/>
                        )}
                    ]}/>
                    {renderRefreshModal()}
                    {renderRollbackModal()}
                    {renderDetailModal()}
                    {renderScheduleModal()}
                </SpaceBetween>
            }/>
    )
}

export default withRouter(HpcCustomAmis)
