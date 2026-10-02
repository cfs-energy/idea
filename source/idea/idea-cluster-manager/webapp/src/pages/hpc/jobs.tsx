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

import {buildBudgetUsage} from '../../components/job-budget-usage';
import React, {Component, RefObject} from "react";

import IdeaListView from "../../components/list-view";
import {TableProps} from "@cloudscape-design/components/table";
import {DeleteJobRequest, DeleteJobResult, SocaJob} from "../../client/data-model"
import {AppContext} from "../../common";
import {SchedulerAdminClient, SchedulerClient} from "../../client"
import IdeaSplitPanel from "../../components/split-panel";
import {Box, Button, ColumnLayout, SpaceBetween, StatusIndicator, Table, Tabs} from "@cloudscape-design/components";
import {KeyValue, KeyValueGroup} from "../../components/key-value";
import IdeaConfirm from "../../components/modals";
import Utils from "../../common/utils";
import {formatDurationMinutes, formatProvisioningAttempt, getJobWaitingSignals, JobElapsedState, JobUtils} from "./hpc-utils";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";

/** Second line under the status of a job that has not started, e.g. "waiting 42 min - attempt 2 of 3 -
 * queue limit: max_provisioned_instances". Renders nothing once the job is running. */
export function JobWaitingSignals(props: { job: SocaJob, now?: Date }) {
    const signals = getJobWaitingSignals(props.job, (props.now) ? props.now : new Date())
    if (signals.length === 0) {
        return null
    }
    return <Box variant="small" color="text-body-secondary">{signals.join(' \u00b7 ')}</Box>
}

export function JobStatus({job}: {job: SocaJob}) {
    const disposition = job.disposition ?? (job.start_time ? (job.exit_status ? 'failed' : 'ran') : 'deleted')
    const failed = job.exit_status ? `Failed (exit ${job.exit_status})` : 'Failed'
    const label = job.state === 'finished' ? (disposition === 'ran' ? 'Finished' : disposition === 'failed' ? failed : disposition[0].toUpperCase() + disposition.slice(1))
        : job.state === 'held' ? 'Held'
        : job.state === 'running' ? 'Running'
        : job.state === 'exit' ? (job.exit_status ? failed : 'Finishing')
        : job.params?.compute_stack === 'tbd' ? 'Queued' : 'Provisioning'
    const type = label === 'Held' || label.startsWith('Failed') ? 'error'
        : label === 'Deleted' ? 'stopped' : label === 'Finished' || label === 'Running' ? 'success' : 'pending'
    return <SpaceBetween size="xxs">
        <StatusIndicator type={type}>{label}</StatusIndicator>
        {(job.status_reason || job.error_message) && <Box variant="small">{job.status_reason || job.error_message}</Box>}
        <JobWaitingSignals job={job}/>
    </SpaceBetween>
}

/** Empty state for the user's Active jobs table: one line and the two ways to start a job. */
export function ActiveJobsEmpty({navigate}: {navigate: (path: string) => void}) {
    return <Box textAlign="center" color="inherit">
        <SpaceBetween size="s">
            <Box variant="p" color="inherit">You have no active jobs.</Box>
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                <Button onClick={() => navigate('/soca/jobs/submit-job')}>Submit</Button>
                <Button variant="primary" onClick={() => navigate('/home/script-workbench')}>Write script</Button>
            </SpaceBetween>
        </SpaceBetween>
    </Box>
}

export function JobBudgetImpact({job}: {job: SocaJob}) {
    if (!job.estimated_bom_cost || job.estimated_bom_cost.price_unavailable) {
        return <Box>Price not available</Box>
    }
    return buildBudgetUsage(job.estimated_budget_usage!)
}

export function JobCosts({job}: {job: SocaJob}) {
    const cost = job.estimated_bom_cost
    const columns: TableProps.ColumnDefinition<NonNullable<NonNullable<SocaJob['estimated_bom_cost']>['line_items']>[number]>[] = [
        {id: 'title', header: 'Item', cell: item => item.title},
        {id: 'qty', header: 'Qty', cell: item => item.quantity},
        {id: 'unit', header: 'Unit', cell: item => item.unit},
        {id: 'unit-price', header: 'Unit Price', cell: item => Utils.getFormattedAmount(item.unit_price)},
        {id: 'total-price', header: 'Total Price', cell: item => Utils.getFormattedAmount(item.total_price)}
    ]
    return <SpaceBetween size="m">
        <Table items={cost?.line_items ?? []} columnDefinitions={columns}/>
        <ColumnLayout columns={2}>
            <Box variant="h3">Estimated Total Cost</Box>
            <Box variant="h3" textAlign="right">{!cost || cost.price_unavailable ? 'Price not available' : Utils.getFormattedAmount(cost.line_items_total)}</Box>
        </ColumnLayout>
    </SpaceBetween>
}

const formatDate = (value?: string) => value ? new Date(value).toLocaleString() : '\u2013'
const timeOf = (value?: string) => value ? new Date(value).getTime() : 0
const runtimeSeconds = (job: SocaJob, now: Date = new Date()) => new JobUtils(job).getElapsedSeconds(now)
// spend is the line items. total subtracts a hypothetical reserved-instance discount on older records.
const costOf = (job: SocaJob) => job.estimated_bom_cost && !job.estimated_bom_cost.price_unavailable ? job.estimated_bom_cost.line_items_total : undefined

/** Columns shared by every jobs view. Completed views add cost estimate and exit status (see jobColumns). */
export const JOB_TABLE_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<SocaJob>[] = [
    {id: 'id', header: 'Job ID', cell: job => job.job_id, sortingField: 'job_id'},
    {id: 'name', header: 'Name', cell: job => job.name, sortingField: 'name'},
    {id: 'status', header: 'Status', cell: job => <JobStatus job={job}/>, sortingField: 'state'},
    {id: 'owner', header: 'Owner', cell: job => job.owner, sortingField: 'owner'},
    {id: 'queue', header: 'Queue', cell: job => job.queue, sortingField: 'queue'},
    {id: 'project', header: 'Project', cell: job => job.project, sortingField: 'project'},
    {
        id: 'queued-on',
        header: 'Submitted',
        cell: job => formatDate(job.queue_time),
        sortingComparator: (a, b) => timeOf(a.queue_time) - timeOf(b.queue_time)
    },
    {
        id: 'runtime',
        header: 'Runtime',
        cell: job => {
            const seconds = runtimeSeconds(job)
            return seconds == null ? '\u2013' : formatDurationMinutes(seconds)
        },
        sortingComparator: (a, b) => (runtimeSeconds(a) ?? -1) - (runtimeSeconds(b) ?? -1)
    }
]

/** Cost is only recorded once a job has finished (from its measured run time), so it is shown on
 * completed views only; active jobs carry no estimate to show. */
export function jobColumns(type: string): TableProps.ColumnDefinition<SocaJob>[] {
    if (type !== 'completed') {
        return JOB_TABLE_COLUMN_DEFINITIONS
    }
    return [
        ...JOB_TABLE_COLUMN_DEFINITIONS,
        {
            id: 'cost',
            header: 'Cost estimate',
            cell: job => costOf(job) ? Utils.getFormattedAmount(costOf(job)) : '\u2013',
            sortingComparator: (a, b) => (costOf(a)?.amount ?? -1) - (costOf(b)?.amount ?? -1)
        },
        {id: 'exit_code', header: 'Exit status', cell: job => job.exit_status, sortingField: 'exit_status'}
    ]
}

/** Owner is hidden by default on a user's own jobs (it is always them) but stays in the column preferences. */
export const MY_JOBS_PREFERENCES_KEY = 'my-jobs'

export function hideOwnerByDefault() {
    // ponytail: seeds IdeaTable's stored column preference because the table has no default-hidden prop;
    // replace with that prop if one is added to components/table.
    const storage = AppContext.get().localStorage()
    const key = `${MY_JOBS_PREFERENCES_KEY}-table-columns`
    if (storage.getItem(key) == null) {
        // carry over the columns chosen under the key this view used before, owner hidden
        let previous = {}
        try {
            previous = JSON.parse(storage.getItem('hpc-jobs-table-columns') ?? '{}') ?? {}
        } catch {
            previous = {}
        }
        storage.setItem(key, JSON.stringify({...previous, owner: false}))
        const pageSize = storage.getItem('hpc-jobs-table-pageSize')
        if (pageSize != null && storage.getItem(`${MY_JOBS_PREFERENCES_KEY}-table-pageSize`) == null) {
            storage.setItem(`${MY_JOBS_PREFERENCES_KEY}-table-pageSize`, pageSize)
        }
    }
}

const ELAPSED_STATUS_INDICATOR: { [k in JobElapsedState]: 'pending' | 'info' | 'warning' | 'error' } = {
    'not-started': 'pending',
    'no-walltime': 'info',
    'within-limit': 'info',
    'near-limit': 'warning',
    'over-limit': 'error'
}

export interface JobInfoProps {
    job: SocaJob
    now?: Date  // fixed clock for tests; defaults to the browser clock
}

/** Job Info tab of the job split panel. Every value is already on the client in the job listing
 * response; nothing is fetched and nothing is predicted. */
export function JobInfo(props: JobInfoProps) {
    const job = props.job
    const now = (props.now) ? props.now : new Date()
    const jobUtil = new JobUtils(job)
    const elapsed = jobUtil.getElapsedSummary(now)
    return (
        <ColumnLayout columns={3} variant="text-grid">
            <KeyValue title="State" value={job.state}/>
            <KeyValue title="Job Id" value={job.job_id}/>
            <KeyValue title="Job Group" value={job.job_group} clipboard={true}/>
            <KeyValue title="Queue" value={job.queue}/>
            <KeyValue title="Queue Type" value={job.queue_type}/>
            <KeyValue title="Scaling Mode" value={job.scaling_mode}/>
            <KeyValue title="Name" value={job.name}/>
            <KeyValue title="Project" value={job.project}/>
            <KeyValue title="Owner" value={job.owner}/>
            <KeyValue title="Queue Time" value={job.queue_time} type="date"/>
            <KeyValue title="Provisioning Time" value={job.provisioning_time} type="date"/>
            <KeyValue title="Start Time" value={job.start_time} type="date"/>
            <KeyValue title="End Time" value={job.end_time} type="date"/>
            <KeyValue title="Exit Status" value={job.exit_status}/>
            <KeyValue title="Queued For" value={formatDurationMinutes(jobUtil.getQueuedSeconds(now))}/>
            {job.provisioning_attempt != null &&
                <KeyValue title="Provisioning Attempt"
                          value={formatProvisioningAttempt(job.provisioning_attempt, job.max_provisioning_attempts, job.state === 'held')}/>}
            {Utils.isNotEmpty(job.blocking_limit_type) &&
                <KeyValue title="Blocking Queue Limit" value={job.blocking_limit_type}/>}
            <KeyValue title="Requested Walltime" value={job.params?.walltime}/>
            <KeyValue title="Elapsed vs Requested" type="react-node" value={
                <StatusIndicator type={ELAPSED_STATUS_INDICATOR[elapsed.state]}>{elapsed.text}</StatusIndicator>
            }/>
            {/* a job that never started has no run time; 0 seconds would read as "less than 1 min" */}
            <KeyValue title="Total Time"
                      value={Utils.isEmpty(job.start_time) ? '-' : formatDurationMinutes(jobUtil.getTotalTimeSeconds())}/>
            {job.status_reason && <KeyValue title="Status Reason" value={job.status_reason}/>}
            {job.disposition && <KeyValue title="Disposition" value={job.disposition}/>}
            {job.reason_class && <KeyValue title="Reason Class" value={job.reason_class}/>}
            {job.state !== 'held' && <KeyValue title="Comment" value={job.comment} clipboard={true}/>}
            {Utils.isNotEmpty(job.error_message) &&
                <KeyValue title="Error Message" type="react-node" value={
                    <StatusIndicator type="error">{job.error_message}</StatusIndicator>
                }/>}
        </ColumnLayout>
    )
}

export interface JobsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
    type: string  // active, completed
    scope: string // user, admin
}

export interface JobsState {
    splitPanelOpen: boolean
    jobSelected: boolean
}

class Jobs extends Component<JobsProps, JobsState> {

    listing: RefObject<IdeaListView | null>
    deleteJobConfirmModal: RefObject<IdeaConfirm | null>
    // the listing clears its selection on every fetch, so the selected job id is
    // tracked here and re-applied against the refreshed listing
    selectedJobId: string | null = null
    linkedJobId: string | null = null

    constructor(props: JobsProps) {
        super(props);
        this.listing = React.createRef()
        this.deleteJobConfirmModal = React.createRef()
        this.linkedJobId = props.type === 'completed' ? props.searchParams?.get('job_id') ?? null : null
        this.selectedJobId = this.linkedJobId
        if (props.scope === 'user') {
            hideOwnerByDefault()
        }
        this.state = {
            splitPanelOpen: !!this.linkedJobId,
            jobSelected: !!this.linkedJobId
        }
    }

    getDeleteJobConfirmModal(): IdeaConfirm {
        return this.deleteJobConfirmModal.current!
    }

    schedulerAdmin(): SchedulerAdminClient {
        return AppContext.get().client().schedulerAdmin()
    }

    scheduler(): SchedulerClient {
        return AppContext.get().client().scheduler()
    }

    getListing(): IdeaListView {
        return this.listing.current!
    }

    isSelected(): boolean {
        return this.state.jobSelected
    }

    getSelected(): SocaJob | null {
        if (this.getListing() == null) {
            return null
        }
        return this.getListing().getSelectedItem()
    }

    isActiveJobs(): boolean {
        return this.props.type === 'active'
    }

    isCompletedJobs(): boolean {
        return this.props.type === 'completed'
    }

    deleteSelectedJob() {
        // Scheduler.DeleteJob runs qdel as the job owner, which stops the job if
        // it is already running. Capture the id before the listing clears it.
        const jobId = this.getSelected()?.job_id
        if (Utils.isEmpty(jobId)) {
            return
        }
        const deleteJob = (request: DeleteJobRequest): Promise<DeleteJobResult> => {
            if (this.props.scope === 'admin') {
                return this.schedulerAdmin().deleteJob(request)
            } else {
                return this.scheduler().deleteJob(request)
            }
        }
        deleteJob({
            job_id: jobId
        }).then(() => {
            this.props.onFlashbarChange({
                items: [
                    {
                        type: 'info',
                        content: `Job Id: ${jobId} will be deleted shortly. If it was running, it is being stopped.`,
                        dismissible: true
                    }
                ]
            })
            this.getListing().fetchRecords()
        }).catch((error) => {
            this.props.onFlashbarChange({
                items: [
                    {
                        type: 'error',
                        content: error.message,
                        dismissible: true
                    }
                ]
            })
        })
    }

    buildDeleteJobConfirmModal() {
        return (
            <IdeaConfirm ref={this.deleteJobConfirmModal}
                         title="Delete job"
                         confirmLabel="Delete job"
                         onConfirm={() => {
                             this.deleteSelectedJob()
                         }}>
                Job Id: <Box variant="strong">{this.getSelected()?.job_id}</Box> will be removed from the queue. If the job is running, deleting
                it stops the job and anything it has not already written to storage is lost. This cannot be undone.
            </IdeaConfirm>
        )
    }

    buildListing() {
        const columnDefinitions = jobColumns(this.props.type)
        return (
            <IdeaListView
                ref={this.listing}
                empty={this.props.scope === 'user' && this.isActiveJobs() ? <ActiveJobsEmpty navigate={this.props.navigate}/> : undefined}
                preferencesKey={this.props.scope === 'user' ? MY_JOBS_PREFERENCES_KEY : 'hpc-jobs'}
                showPreferences={true}
                title={(this.isActiveJobs()) ? 'Active jobs' : 'Completed jobs'}
                selectionType="single"
                enableExportToCsv={this.isCompletedJobs()}
                csvFilename={() => `completed_jobs_export_${new Date().toISOString().replace(/[:.]/g, '-').replace('T', '_').split('Z')[0]}.csv`}
                onExportAllRecords={this.isCompletedJobs() ? async () => {
                    try {
                        // Get current filters and date range, with fallbacks
                        const filters = this.getListing()?.getFilters() || [];
                        const dateRange = this.getListing()?.getDateRange();

                        const requestParams = {
                            filters: filters,
                            paginator: {
                                start: 0,
                                page_size: 10000 // Maximum allowed by Elasticsearch/OpenSearch
                            },
                            date_range: dateRange ? {
                                ...dateRange,
                                key: 'queue_time'
                            } : undefined
                        };

                        // Fetch all completed jobs with current filters but no pagination limit
                        const result = this.props.scope === 'user'
                            ? await this.scheduler().listCompletedJobs(requestParams)
                            : await this.schedulerAdmin().listCompletedJobs(requestParams);

                        const records = result.listing || [];

                        // Show success message after export
                        setTimeout(() => {
                            const message = records.length === 10000
                                ? `Successfully exported ${records.length} completed jobs to CSV (maximum allowed per export).`
                                : `Successfully exported ${records.length} completed jobs to CSV.`;
                            this.props.onFlashbarChange({
                                items: [
                                    {
                                        content: message,
                                        type: 'success',
                                        dismissible: true
                                    }
                                ]
                            });
                        }, 500);

                        return records;
                    } catch (error) {
                        console.error('CSV Export error:', error);
                        const errorMessage = error instanceof Error ? error.message : String(error);
                        this.props.onFlashbarChange({
                            items: [
                                {
                                    content: `Failed to export CSV: ${errorMessage}`,
                                    type: 'error',
                                    dismissible: true
                                }
                            ]
                        });
                        throw error;
                    }
                } : undefined}
                // todo - commented until file picker UI is implemented in submit job form
                secondaryActionsDisabled={this.isCompletedJobs()}
                secondaryActions={[
                    {
                        id: 'delete-job',
                        text: 'Delete job (stops it if running)',
                        disabled: !this.isSelected(),
                        onClick: () => {
                            this.getDeleteJobConfirmModal().show()
                        }
                    }
                ]}
                showPaginator={true}
                showLastRefreshed={true}
                // the active-jobs read shares a lock with the scheduler's provisioning
                // threads, so polling is opt-in and never offered on the all-jobs views
                enableAutoRefresh={this.props.scope === 'user' && this.isActiveJobs()}
                showFilters={true}
                showDateRange={(this.props.type === 'completed')}
                dateRange={{
                    type: 'relative',
                    amount: 1,
                    unit: "month"
                }}
                onDateRange={(dateRange) => {
                    return {
                        key: 'queue_time',
                        start: dateRange.start,
                        end: dateRange.end
                    }
                }}
                filters={[
                    {
                        key: 'any',
                        value: this.linkedJobId ?? undefined
                    }
                ]}
                onFilter={(filters) => {
                    const filterString = Utils.asString(filters[0].value).trim()
                    if (this.linkedJobId && filterString === this.linkedJobId) return [{key: 'job_id', value: this.linkedJobId}]
                    this.linkedJobId = null
                    if (Utils.isEmpty(filterString)) {
                        return []
                    } else if (Utils.isPositiveInteger(filterString)) {
                        return [
                            {
                                key: 'job_id',
                                value: filterString
                            }
                        ]
                    } else if (filterString.includes(',')) {
                        const jobIds = filterString.split(',')
                        return [
                            {
                                key: 'job_id',
                                value: jobIds.map(jobId => jobId.trim().toLowerCase())
                            }
                        ]
                    } else {
                        return [
                            {
                                key: '$all',
                                value: filterString
                            }
                        ]
                    }
                }}
                onRefresh={() => {
                    this.selectedJobId = null
                    this.setState({
                        jobSelected: false
                    }, () => {
                        this.getListing().fetchRecords()
                    })
                }}
                onSelectionChange={() => {
                    this.selectedJobId = this.getSelected()?.job_id ?? null
                    this.setState({
                        splitPanelOpen: true,
                        jobSelected: true
                    }, () => {
                    })
                }}
                onRecordsFetched={(listing: SocaJob[]) => {
                    if (!this.state.jobSelected) {
                        return
                    }
                    const job = listing.find((job) => job.job_id === this.selectedJobId)
                    if (job) {
                        this.getListing().setSelectedItems([job], () => this.setState({jobSelected: true}))
                    } else {
                        this.selectedJobId = null
                        this.setState({
                            jobSelected: false,
                            splitPanelOpen: false
                        })
                    }
                }}
                onFetchRecords={() => {
                    if (this.props.scope === 'user') {
                        if (this.props.type === 'active') {
                            return this.scheduler().listActiveJobs({
                                filters: this.linkedJobId ? [{key: 'job_id', value: this.linkedJobId}] : this.getListing().getFilters(),
                                paginator: this.getListing().getPaginator()
                            })
                        } else {
                            return this.scheduler().listCompletedJobs({
                                filters: this.linkedJobId ? [{key: 'job_id', value: this.linkedJobId}] : this.getListing().getFilters(),
                                paginator: this.getListing().getPaginator(),
                                date_range: this.linkedJobId ? undefined : {
                                    ...this.getListing().getDateRange(),
                                    key: 'queue_time'
                                }
                            })
                        }
                    } else {
                        if (this.props.type === 'active') {
                            return this.schedulerAdmin().listActiveJobs({
                                filters: this.linkedJobId ? [{key: 'job_id', value: this.linkedJobId}] : this.getListing().getFilters(),
                                paginator: this.getListing().getPaginator()
                            })
                        } else {
                            return this.schedulerAdmin().listCompletedJobs({
                                filters: this.linkedJobId ? [{key: 'job_id', value: this.linkedJobId}] : this.getListing().getFilters(),
                                paginator: this.getListing().getPaginator(),
                                date_range: this.linkedJobId ? undefined : {
                                    ...this.getListing().getDateRange(),
                                    key: 'queue_time'
                                }
                            })
                        }
                    }
                }}
                columnDefinitions={columnDefinitions}
                defaultSortingColumn="queue_time"
                defaultSortingDescending={true}
            />
        )
    }

    buildSplitPanelContent() {
        const selected = () => this.getSelected()!
        const jobUtil = () => new JobUtils(selected())
        const jobParams = () => selected().params!
        return (this.isSelected() && this.getSelected() != null &&
            <IdeaSplitPanel
                title={`JobId: ${this.getSelected()?.job_id}`}
            >
                <Tabs
                    tabs={[
                        {
                            label: 'Job Info',
                            id: 'job-info',
                            content: (
                                <JobInfo job={selected()}/>
                            )
                        },
                        {
                            label: 'Compute Stack',
                            id: 'compute-stack',
                            content: (
                                <ColumnLayout columns={2} variant="text-grid">
                                    <KeyValueGroup title="Instance Info">
                                        <KeyValue title="Base OS" value={jobParams().base_os}/>
                                        <KeyValue title="Instance AMI" value={jobParams().instance_ami} clipboard={true}/>
                                        <KeyValue title="Instance Types" value={jobParams().instance_types}/>
                                        <KeyValue title="Keep EBS Volumes" value={jobParams().keep_ebs_volumes}/>
                                        <KeyValue title="Root Storage Size" value={jobParams().root_storage_size} type="memory"/>
                                        <KeyValue title="Enable Elastic Fabric Adapter (EFA)" value={jobParams().enable_efa_support}/>
                                        <KeyValue title="Force Reserved Instances" value={jobParams().force_reserved_instances}/>
                                        <KeyValue title="Enable Hyper-Threading" value={jobParams().enable_ht_support}/>
                                    </KeyValueGroup>

                                    <KeyValueGroup title="Network and Security">
                                        <KeyValue title="Subnet Ids" value={jobParams().subnet_ids} clipboard={true}/>
                                        <KeyValue title="Security Groups" value={jobParams().security_groups} clipboard={true}/>
                                        <KeyValue title="Instance Profile" value={jobParams().instance_profile}/>
                                        <KeyValue title="Enable Placement Group" value={jobParams().enable_placement_group}/>
                                    </KeyValueGroup>

                                    <KeyValueGroup title="Compute Requirements">
                                        <KeyValue title="Nodes" value={jobParams().nodes}/>
                                        <KeyValue title="CPUs" value={jobParams().cpus}/>
                                    </KeyValueGroup>

                                    <KeyValueGroup title="Spot Fleet">
                                        <KeyValue title="Is Spot?" value={jobParams().spot}/>
                                        {jobUtil().isEnableSpot() &&
                                            <KeyValue title="Spot Price" value={jobParams().spot_price} type="amount"/>}
                                        {jobUtil().isEnableSpot() &&
                                            <KeyValue title="Spot Allocation Count" value={jobParams().spot_allocation_count}/>}
                                        {jobUtil().isEnableSpot() &&
                                            <KeyValue title="Spot Allocation Strategy" value={jobParams().spot_allocation_strategy}/>}
                                    </KeyValueGroup>

                                    {!jobUtil().isScratchStorageEnabled() &&
                                        <KeyValueGroup title="Scratch Storage">
                                            <KeyValue title="Is Enabled?" value={false}/>
                                        </KeyValueGroup>
                                    }

                                    {jobUtil().isScratchEBS() &&
                                        <KeyValueGroup title="Scratch Storage: EBS">
                                            <KeyValue title="EBS: Storage Size" value={jobParams().scratch_storage_size}
                                                      type="memory"/>
                                            <KeyValue title="EBS Storage IOPS" value={jobParams().scratch_storage_iops}/>
                                        </KeyValueGroup>
                                    }

                                    {jobUtil().isScratchExistingFsxLustre() &&
                                        <KeyValueGroup title="Scratch Storage: Existing FSx for Lustre">
                                            <KeyValue title="Existing FSx Lustre" value={jobParams().fsx_lustre?.existing_fsx}/>
                                        </KeyValueGroup>
                                    }
                                    {jobUtil().isScratchNewFsxLustre() && <KeyValueGroup title="Scratch Storage: New FSx for Lustre">
                                        <KeyValue title="S3 Backend" value={jobParams().fsx_lustre?.s3_backend}/>
                                        <KeyValue title="Import Path" value={jobParams().fsx_lustre?.import_path}/>
                                        <KeyValue title="Export Path" value={jobParams().fsx_lustre?.export_path}/>
                                        <KeyValue title="Deployment Type" value={jobParams().fsx_lustre?.deployment_type}/>
                                        <KeyValue title="Per Unit Throughput" value={jobParams().fsx_lustre?.per_unit_throughput}/>
                                        <KeyValue title="Size" value={jobParams().fsx_lustre?.size} type="memory"/>
                                    </KeyValueGroup>
                                    }

                                    <KeyValueGroup title="Metrics">
                                        <KeyValue title="Enable System Metrics" value={jobParams().enable_system_metrics}/>
                                        <KeyValue title="Enable Anonymous Metrics" value={jobParams().enable_anonymous_metrics}/>
                                    </KeyValueGroup>
                                </ColumnLayout>
                            )
                        },
                        {
                            label: 'Execution Hosts',
                            id: 'execution-hosts',
                            content: (
                                <Table items={(selected().execution_hosts) ? selected().execution_hosts! : []}
                                       columnDefinitions={[
                                           {
                                               id: 'host',
                                               header: 'Host',
                                               cell: host => host.host
                                           },
                                           {
                                               id: 'instance-id',
                                               header: 'Instance Id',
                                               cell: host => host.instance_id
                                           },
                                           {
                                               id: 'instance-type',
                                               header: 'Instance Type',
                                               cell: host => host.instance_type
                                           },
                                           {
                                               id: 'capacity-type',
                                               header: 'Capacity Type',
                                               cell: host => host.capacity_type
                                           },
                                           {
                                               id: 'tenancy',
                                               header: 'Tenancy',
                                               cell: host => host.tenancy
                                           }
                                       ]}/>
                            )
                        },
                        {
                            label: 'Estimated Costs',
                            id: 'estimated-costs',
                            content: <JobCosts job={selected()}/>
                        },
                        ...(selected().state === 'finished' && selected().estimated_budget_usage ? [{
                            label: 'Budget impact',
                            id: 'budget-impact',
                            content: <JobBudgetImpact job={selected()}/>
                        }] : [])
                    ]}
                />
            </IdeaSplitPanel>)
    }

    render() {

        const breadcrumbs = () => {
            if (this.props.scope === 'user') {
                if (this.props.type === 'active') {
                    return [
                        {
                            text: 'IDEA',
                            href: '#/'
                        },
                        {
                            text: 'Active Jobs',
                            href: '#/home/active-jobs'
                        }
                    ]
                } else {
                    return [
                        {
                            text: 'IDEA',
                            href: '#/'
                        },
                        {
                            text: 'Completed Jobs',
                            href: '#/home/completed-jobs'
                        }
                    ]
                }
            } else {
                if (this.props.type === 'active') {
                    return [
                        {
                            text: 'IDEA',
                            href: '#/'
                        },
                        {
                            text: 'Scale-Out Computing',
                            href: '#/soca/active-jobs'
                        },
                        {
                            text: 'Active Jobs',
                            href: ''
                        }
                    ]
                } else {
                    return [
                        {
                            text: 'IDEA',
                            href: '#/'
                        },
                        {
                            text: 'Scale-Out Computing',
                            href: '#/soca/active-jobs'
                        },
                        {
                            text: 'Completed Jobs',
                            href: ''
                        }
                    ]
                }
            }
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
                breadcrumbItems={breadcrumbs()}
                content={
                    <div>
                        {this.buildDeleteJobConfirmModal()}
                        {this.buildListing()}
                    </div>
                }
                splitPanelOpen={this.state.splitPanelOpen}
                splitPanel={this.buildSplitPanelContent()}
                onSplitPanelToggle={(event: any) => {

                    this.setState({
                        jobSelected: false,
                        splitPanelOpen: event.detail.open
                    })
                }}
            />
        )
    }
}

function _ActiveJobs(props: JobsProps) {
    return (
        <Jobs
            {...props}
            type="active" scope="user"
        />
    )
}

function _CompletedJobs(props: JobsProps) {
    return (
        <Jobs
            {...props}
            type="completed" scope="user"
        />
    )
}

function _AdminActiveJobs(props: JobsProps) {
    return (
        <Jobs
            {...props}
            type="active" scope="admin"
        />
    )
}

function _AdminCompletedJobs(props: JobsProps) {
    return (
        <Jobs
            {...props}
            type="completed" scope="admin"
        />
    )
}

export const ActiveJobs = withRouter(_ActiveJobs)
export const CompletedJobs = withRouter(_CompletedJobs)
export const AdminActiveJobs = withRouter(_AdminActiveJobs)
export const AdminCompletedJobs = withRouter(_AdminCompletedJobs)
