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

import {
    HpcQueueProfile, ListProjectsRequest, SocaFSxLustreConfig, ListProjectsResult, Project, SocaJobParams, SocaJob, SocaCapacityType, SocaInstanceTypeOptions
} from "../../client/data-model";
import Utils from "../../common/utils";
import dot from "dot-object";

export type JobElapsedState = 'not-started' | 'no-walltime' | 'within-limit' | 'near-limit' | 'over-limit'

export interface JobElapsedSummary {
    state: JobElapsedState
    text: string
}

// a job is flagged as approaching its walltime at this fraction of the request.
// elapsed uses the browser clock against server timestamps, so the threshold stays coarse.
const NEAR_WALLTIME_RATIO = 0.9

// states a job cannot leave. a job in one of these stopped accruing time, so a
// missing end_time must never be substituted with the browser clock.
const TERMINAL_JOB_STATES = ['finished', 'exit']

/** Parses an HH:MM:SS walltime into seconds, matching the server's ModelUtils.walltime_to_seconds.
 * Returns null for anything it cannot parse. */
/**
 * the "jobs may create a new FSx for Lustre" box: a new profile starts unchecked; a
 * profile saved before the setting existed has no value and allows it, so it shows checked
 */
export function allowNewFsxLustreInitialValue(isCreate: boolean, queueProfile?: HpcQueueProfile): boolean {
    if (isCreate) {
        return false
    }
    const stored = queueProfile?.queue_management_params?.allow_new_fsx_lustre
    return stored !== false
}

const FSX_THROUGHPUT: {[deployment: string]: {values: number[], default: number}} = {
    persistent_1: {values: [50, 100, 200], default: 200},
    persistent_2: {values: [125, 250, 500, 1000], default: 125}
}

// the form's notices that this edit clears stored defaults; never sent
export const SCRATCH_OFF_NOTICE = 'scratch_off_notice'
export const SCRATCH_PROVIDER_NOTICE = 'scratch_provider_notice'
export const SPOT_OFF_NOTICE = 'spot_off_notice'

/** a deep, frozen copy of a profile, to compare a form's edits against */
export function snapshotQueueProfile(queueProfile: any): any {
    const freeze = (value: any): any => {
        if (value != null && typeof value === 'object') {
            Object.values(value).forEach(freeze)
            Object.freeze(value)
        }
        return value
    }
    return freeze(JSON.parse(JSON.stringify(queueProfile ?? {})))
}

/** the stored profile had scratch on and the form now has it off */
export function scratchTurnedOff(stored: any, queueProfile: any): boolean {
    return dot.pick('default_job_params.enable_scratch', stored || {}) === true
        && dot.pick('default_job_params.enable_scratch', queueProfile) !== true
}

/** the stored profile had Spot on and the form now has it off */
export function spotTurnedOff(stored: any, queueProfile: any): boolean {
    return dot.pick('default_job_params.spot', stored || {}) === true
        && dot.pick('default_job_params.spot', queueProfile) !== true
}

/** the provider the form shows for a profile: EBS when none is stored, as the form does */
export function scratchProviderShown(profile: any): string {
    return dot.pick('default_job_params.scratch_provider', profile || {}) ?? 'ebs'
}

/**
 * scratch is on in the form, and was off or had another provider when loaded: the other
 * provider's stored settings are this edit's to clear. an untouched profile keeps them
 */
export function scratchProviderChanged(stored: any, queueProfile: any): boolean {
    return dot.pick('default_job_params.enable_scratch', stored || {}) !== true
        || scratchProviderShown(stored) !== scratchProviderShown(queueProfile)
}

// the queue default FSx settings, each sent as null to clear it when scratch is turned off
const FSX_LUSTRE_CLEARED = {
    existing_fsx: null,
    s3_backend: null,
    import_path: null,
    export_path: null,
    deployment_type: null,
    per_unit_throughput: null,
    size: null,
}

/**
 * what the queue profile form sends on save: the stored profile plus the form's edits,
 * with the defaults the chosen scratch and Spot settings no longer use sent as null so the
 * server removes them, and everything else left as stored
 */
export function prepareQueueProfileForSave(queueProfile: any, stored: any = {}): any {
    dot.del(SCRATCH_OFF_NOTICE, queueProfile)
    dot.del(SCRATCH_PROVIDER_NOTICE, queueProfile)
    dot.del(SPOT_OFF_NOTICE, queueProfile)
    const project_ids = dot.pick('project_ids', queueProfile, true) || []
    queueProfile.projects = project_ids.map((project_id: string) => ({project_id: project_id}))
    applyFsxThroughputForSave(queueProfile)
    const utils = new QueueUtils(queueProfile)

    const keep_forever = Utils.asBoolean(dot.pick('keep_forever', queueProfile), false)
    if (keep_forever) {
        dot.del('scaling_mode', queueProfile)
    } else if (dot.pick('scaling_mode', queueProfile) === 'single-job') {
        queueProfile.terminate_when_idle = 0
    }

    if (utils.isScratchStorageEnabled() && !scratchProviderChanged(stored, queueProfile)) {
        // the provider this profile already had: its stored settings go back as stored
    } else if (utils.isScratchStorageEnabled()) {
        if (utils.isScratchEBS()) {
            clearQueueDefault(queueProfile, 'fsx_lustre')
        } else if (utils.isScratchFsxLustre()) {
            clearQueueDefault(queueProfile, 'scratch_storage_size')
            clearQueueDefault(queueProfile, 'scratch_storage_iops')
            dot.str('default_job_params.fsx_lustre.enabled', true, queueProfile)
            if (utils.isScratchNewFsxLustre()) {
                // a new file system per job: an existing one stored earlier no longer applies
                dot.str('default_job_params.fsx_lustre.existing_fsx', null, queueProfile)
            }
        }
    } else if (scratchTurnedOff(stored, queueProfile)) {
        // only when this edit turns scratch off: a profile stored with scratch off (one
        // written through the API can still carry FSx defaults) goes back as stored
        clearQueueDefault(queueProfile, 'scratch_provider')
        clearQueueDefault(queueProfile, 'scratch_storage_size')
        clearQueueDefault(queueProfile, 'scratch_storage_iops')
        // every stored FSx setting is sent as null so the server removes it: kept, a job
        // asking fsx_lustre=true on this queue got the old bucket and type, and a later
        // re-enable started from them
        clearQueueDefault(queueProfile, 'fsx_lustre')
        dot.str('default_job_params.fsx_lustre', {...FSX_LUSTRE_CLEARED, enabled: false}, queueProfile)
    }
    if (spotTurnedOff(stored, queueProfile)) {
        // only when this edit turns Spot off: a profile stored with Spot off keeps its
        // stored price, count and strategy, as the published release kept them
        clearQueueDefault(queueProfile, 'spot_price')
        clearQueueDefault(queueProfile, 'spot_allocation_count')
        clearQueueDefault(queueProfile, 'spot_allocation_strategy')
    }
    return queueProfile
}

/**
 * a default the form clears is sent as null: the server removes a default sent as null and
 * keeps one that is left out, so an API client that sends part of the defaults loses nothing
 */
export function clearQueueDefault(queueProfile: any, name: string) {
    queueProfile.default_job_params = queueProfile.default_job_params || {}
    queueProfile.default_job_params[name] = null
}

/**
 * the queue form has one throughput field per persistent deployment type, so each offers
 * only its own values; a stored value fills the field of its type
 */
export function fsxThroughputFormValues(fsx?: SocaFSxLustreConfig): {[deployment: string]: string} {
    const result: {[deployment: string]: string} = {}
    for (const [deployment, rule] of Object.entries(FSX_THROUGHPUT)) {
        const stored = fsx?.deployment_type === deployment ? fsx?.per_unit_throughput : undefined
        result[deployment] = String(stored != null && rule.values.includes(stored) ? stored : rule.default)
    }
    return result
}

/**
 * moves the throughput of the chosen deployment type into the saved profile, and drops it
 * for scratch types, which take none
 */
export function applyFsxThroughputForSave(queueProfile: any) {
    const choices = queueProfile.fsx_lustre_throughput || {}
    delete queueProfile.fsx_lustre_throughput
    const fsx = queueProfile.default_job_params?.fsx_lustre
    if (!fsx) {
        return
    }
    const rule = FSX_THROUGHPUT[fsx.deployment_type]
    if (!rule) {
        delete fsx.per_unit_throughput
        return
    }
    const chosen = Number(choices[fsx.deployment_type])
    fsx.per_unit_throughput = rule.values.includes(chosen) ? chosen : rule.default
}

export function parseWalltimeSeconds(walltime?: string): number | null {
    if (Utils.isEmpty(walltime)) {
        return null
    }
    const parts = Utils.asString(walltime).trim().split(':')
    if (parts.length !== 3) {
        return null
    }
    const values = parts.map((part) => Number(part.trim()))
    if (!values.every((value) => Number.isInteger(value) && value >= 0)) {
        return null
    }
    return values[0] * 3600 + values[1] * 60 + values[2]
}

/** Formats a duration to the nearest minute. Durations are computed from the browser clock, so
 * seconds would only ever render clock drift. */
export function formatDurationMinutes(seconds?: number | null): string {
    if (seconds == null || !Number.isFinite(seconds) || seconds < 0) {
        return '-'
    }
    const totalMinutes = Math.floor(seconds / 60)
    if (totalMinutes < 1) {
        return 'less than 1 min'
    }
    const hours = Math.floor(totalMinutes / 60)
    const minutes = totalMinutes % 60
    if (hours === 0) {
        return `${minutes} min`
    }
    if (minutes === 0) {
        return `${hours} hr`
    }
    return `${hours} hr ${minutes} min`
}

function toEpochMillis(value?: string): number | null {
    if (Utils.isEmpty(value)) {
        return null
    }
    const epochMillis = new Date(Utils.asString(value)).getTime()
    if (Number.isNaN(epochMillis)) {
        return null
    }
    return epochMillis
}

export class JobParamUtils {
    params: SocaJobParams

    constructor(params: SocaJobParams) {
        this.params = params
    }

    isEnableSpot(): boolean {
        if (this.params.spot != null) {
            return this.params.spot
        }
        return false
    }

    isScratchStorageEnabled(): boolean {
        if (this.params == null) {
            return false
        }
        if (this.params.enable_scratch == null) {
            return false
        }
        return this.params.enable_scratch
    }

    getScratchStorageProvider(): string | null {
        if (!this.isScratchStorageEnabled()) {
            return null
        }
        if (this.params.scratch_provider != null) {
            return this.params.scratch_provider
        }
        return null
    }

    isScratchEBS(): boolean {
        const scratchProvider = this.getScratchStorageProvider()
        if (scratchProvider == null) {
            return false
        }
        return scratchProvider === 'ebs'
    }

    isScratchFsxLustre(): boolean {
        const scratchProvider = this.getScratchStorageProvider()
        if (scratchProvider == null) {
            return false
        }
        return scratchProvider === 'fsx-lustre-existing' || scratchProvider === 'fsx-lustre-new'
    }

    isScratchExistingFsxLustre(): boolean {
        const scratchProvider = this.getScratchStorageProvider()
        if (scratchProvider == null) {
            return false
        }
        return scratchProvider === 'fsx-lustre-existing'
    }

    isScratchNewFsxLustre(): boolean {
        const scratchProvider = this.getScratchStorageProvider()
        if (scratchProvider == null) {
            return false
        }
        return scratchProvider === 'fsx-lustre-new'
    }
}

export class QueueUtils extends JobParamUtils {

    queue: HpcQueueProfile

    constructor(queue: HpcQueueProfile) {
        super((queue.default_job_params) ? queue.default_job_params : {})
        this.queue = queue
    }

    hasMetrics(): boolean {
        return false
    }

    getQueueMetric(name: string): number {
        return 0
    }

    getActiveJobs(): number {
        return this.getQueueMetric('active_jobs')
    }

    getDesiredCapacity(): number {
        return this.getQueueMetric('desired_capacity')
    }

    getOnDemandCapacity(): number {
        return this.getQueueMetric('ondemand_capacity')
    }

    getOnDemandNodes(): number {
        return this.getQueueMetric('ondemand_nodes')
    }

    getSpotCapacity(): number {
        return this.getQueueMetric('spot_capacity')
    }

    getSpotNodes(): number {
        return this.getQueueMetric('spot_nodes')
    }

}

export class JobUtils extends JobParamUtils {

    job: SocaJob

    constructor(job: SocaJob) {
        super((job.params) ? job.params : {})
        this.job = job
    }

    getCapacityType(): SocaCapacityType | null {
        if (this.job.params == null) {
            return null
        }
        if (Utils.isTrue(this.job.params.spot)) {
            const spotAllocationCount = Utils.asNumber(this.job.params.spot_allocation_count, 0)
            if (spotAllocationCount === 0) {
                return 'spot'
            } else {
                return 'mixed'
            }
        } else {
            return 'on-demand'
        }
    }

    isSpotCapacity(): boolean {
        const capacityType = this.getCapacityType()
        if (capacityType == null) {
            return false
        }
        return capacityType === 'spot'
    }

    isOnDemandCapacity(): boolean {
        const capacityType = this.getCapacityType()
        if (capacityType == null) {
            return false
        }
        return capacityType === 'on-demand'
    }

    isMixedCapacity(): boolean {
        const capacityType = this.getCapacityType()
        if (capacityType == null) {
            return false
        }
        return capacityType === 'mixed'
    }

    getOnDemandNodes(): number {
        if (this.job.params == null) {
            return 0
        }
        if (this.isSpotCapacity()) {
            return 0
        }
        const nodes = Utils.asNumber(this.job.params.nodes, 0)
        const spotAllocationCount = Utils.asNumber(this.job.params.spot_allocation_count, 0)
        return nodes - spotAllocationCount
    }

    getOnDemandCapacity(): number {
        return this.getOnDemandNodes() * this.getWeightedCapacity()
    }

    getDesiredNodes(): number {
        if (this.job.params == null) {
            return 0
        }
        return Utils.asNumber(this.job.params.nodes, 0)
    }

    getDesiredCapacity(instanceType?: string): number {
        if (this.job.params == null) {
            return 0
        }
        if (this.isSharedCapacity()) {
            return this.getDesiredNodes() * Utils.asNumber(this.job.params.cpus, 1)
        } else {
            return this.getDesiredNodes() * this.getWeightedCapacity(instanceType)
        }
    }

    getSpotNodes(): number {
        if (this.isSpotCapacity()) {
            return this.getDesiredNodes()
        } else if (this.isMixedCapacity()) {
            return Utils.asNumber(this.job.params?.spot_allocation_count, 0)
        }
        return 0
    }

    getSpotCapacity(): number {
        return this.getSpotNodes() * this.getWeightedCapacity()
    }

    getDefaultInstanceTypeOption(): SocaInstanceTypeOptions | null {
        if (this.job.provisioning_options == null) {
            return null
        }
        if (this.job.provisioning_options.instance_types == null || this.job.provisioning_options.instance_types.length === 0) {
            return null
        }
        return this.job.provisioning_options.instance_types[0]
    }

    getInstanceTypeOption(instanceType: string): SocaInstanceTypeOptions | null {
        if (this.job.provisioning_options == null) {
            return null
        }
        if (this.job.provisioning_options.instance_types == null || this.job.provisioning_options.instance_types.length === 0) {
            return null
        }
        const found = this.job.provisioning_options.instance_types.find(option => option.name === instanceType)
        if (found) {
            return found
        }
        return null
    }

    getWeightedCapacity(instanceType?: string): number {
        let option
        if (Utils.isNotEmpty(instanceType)) {
            option = this.getInstanceTypeOption(instanceType!)
        } else {
            option = this.getDefaultInstanceTypeOption()
        }
        if (option == null) {
            return 0
        }
        return Utils.asNumber(option.default_vcpu_count, 0)
    }

    getInstanceTypeCpuCount(instanceType?: string): number {
        const instanceTypeOption = (!instanceType) ? this.getDefaultInstanceTypeOption() : this.getInstanceTypeOption(instanceType)
        return Utils.asNumber(instanceTypeOption?.threads_per_core, 0) * Utils.asNumber(instanceTypeOption?.default_core_count, 0)
    }

    isEphemeralCapacity(): boolean {
        if (this.job.provisioning_options == null) {
            return false
        }
        if (Utils.isTrue(this.job.provisioning_options.keep_forever)) {
            return false
        }
        const terminateWhenIdle = Utils.asNumber(this.job.provisioning_options.terminate_when_idle, 0)
        if (terminateWhenIdle > 0) {
            return false
        }
        return true
    }

    isSharedCapacity(): boolean {
        return !this.isEphemeralCapacity()
    }

    isPersistentCapacity(): boolean {
        if (this.job.provisioning_options == null) {
            return false
        }
        return Utils.isTrue(this.job.provisioning_options.keep_forever)
    }

    getFormattedSpotPrice(): string {
        if (this.params == null) {
            return '-'
        }
        if (!this.isSpotCapacity() && !this.isMixedCapacity()) {
            return '-'
        }
        if (this.params.spot_price != null) {
            return `${this.params.spot_price.amount} ${this.params.spot_price.unit}`
        } else {
            return 'auto'
        }
    }

    getRequestedWalltimeSeconds(): number | null {
        return parseWalltimeSeconds(this.params?.walltime)
    }

    isTerminal(): boolean {
        return TERMINAL_JOB_STATES.indexOf(Utils.asString(this.job.state)) >= 0
    }

    /** Seconds the job has been running, ending at end_time once it is set. Null when the job has not
     * started; never the requested walltime, which is what over-bills a job that never ran. */
    getElapsedSeconds(now: Date = new Date()): number | null {
        const startTime = toEpochMillis(this.job.start_time)
        if (startTime == null) {
            return null
        }
        const endTime = toEpochMillis(this.job.end_time)
        if (endTime != null) {
            return Math.max(0, Math.floor((endTime - startTime) / 1000))
        }
        if (this.isTerminal()) {
            // a terminal job with no end_time reports the scheduler-recorded duration; measuring to
            // now would grow every time the panel opens and eventually flag a job that finished within its walltime.
            return this.getTotalTimeSeconds()
        }
        return Math.max(0, Math.floor((now.getTime() - startTime) / 1000))
    }

    /** Seconds the job has waited in the queue, ending when it starts. Measured wait, not a prediction
     * of when the job will start. */
    getQueuedSeconds(now: Date = new Date()): number | null {
        const queueTime = toEpochMillis(this.job.queue_time)
        if (queueTime == null) {
            return null
        }
        // a job that never started stopped waiting when it ended
        const startTime = toEpochMillis(this.job.start_time)
        const until = (startTime != null) ? startTime : toEpochMillis(this.job.end_time)
        if (until != null) {
            return Math.max(0, Math.floor((until - queueTime) / 1000))
        }
        if (this.isTerminal()) {
            return null
        }
        return Math.max(0, Math.floor((now.getTime() - queueTime) / 1000))
    }

    /** The run time the scheduler recorded. Only the persisted value, so a job that never ran reports
     * nothing rather than its requested walltime. */
    getTotalTimeSeconds(): number | null {
        if (this.job.total_time_secs == null) {
            return null
        }
        const totalTimeSecs = Number(this.job.total_time_secs)
        if (!Number.isFinite(totalTimeSecs) || totalTimeSecs < 0) {
            return null
        }
        return totalTimeSecs
    }

    /** Elapsed run time against the requested walltime, to the nearest minute. A job that has not
     * started reports that, never a duration. */
    getElapsedSummary(now: Date = new Date()): JobElapsedSummary {
        const elapsedSeconds = this.getElapsedSeconds(now)
        if (elapsedSeconds == null) {
            return {state: 'not-started', text: 'Not started'}
        }
        const requestedSeconds = this.getRequestedWalltimeSeconds()
        if (requestedSeconds == null || requestedSeconds <= 0) {
            return {state: 'no-walltime', text: `${formatDurationMinutes(elapsedSeconds)} elapsed`}
        }
        const text = `${formatDurationMinutes(elapsedSeconds)} of ${formatDurationMinutes(requestedSeconds)} requested`
        if (elapsedSeconds >= requestedSeconds) {
            return {state: 'over-limit', text: text}
        }
        if (elapsedSeconds >= requestedSeconds * NEAR_WALLTIME_RATIO) {
            return {state: 'near-limit', text: text}
        }
        return {state: 'within-limit', text: text}
    }

}

/** Both fixes when ncpus is above an instance's physical cores with hyper-threading off. */
export function ncpusExceedsCoresMessage(instanceType: string, cores: number): string {
    return `${instanceType} has ${cores} cores with hyper-threading off; set CPUs per instance to ${cores} or enable hyper-threading`
}

/** The attempt the job is on, out of the configured cap. The scheduler's persistent per-job counter,
 * not the attempt number in the PBS comment - only this one survives a scheduler restart. */
export function formatProvisioningAttempt(attempt?: number | null, maxAttempts?: number | null, held: boolean = false): string | null {
    if (attempt == null || !Number.isFinite(attempt) || attempt < 1) {
        return null
    }
    // a held job is not on an attempt: provisioning stopped retrying it. saying
    // "attempt 3 of 3" reads the same as a live third attempt.
    if (maxAttempts == null || !Number.isFinite(maxAttempts) || maxAttempts < attempt) {
        return (held) ? `held after ${attempt} attempts` : `attempt ${attempt}`
    }
    return (held) ? `held after ${attempt} of ${maxAttempts} attempts` : `attempt ${attempt} of ${maxAttempts}`
}

const QUEUE_LIMIT_LABELS: {[limitType: string]: string} = {
    max_provisioned_instances: 'machines',
    max_running_jobs: 'running jobs'
}

/** What a queue limit limits, in words. */
export function queueLimitLabel(limitType?: string | null): string | null {
    if (Utils.isEmpty(limitType)) {
        return null
    }
    return QUEUE_LIMIT_LABELS[limitType!] ?? 'its size'
}

/** The queue limit holding the job. The type only: the threshold and the current usage describe the
 * whole queue, not this owner. */
export function formatBlockingLimit(limitType?: string | null): string | null {
    const label = queueLimitLabel(limitType)
    return label == null ? null : `The queue is at its limit for ${label}. Your job starts when others finish. Nothing to do.`
}

/** The next step for a job that failed with an exit code. PBS reports a job killed by signal N as 256 + N. */
export function jobFailureNextStep(job: SocaJob): string | null {
    const code = job.exit_status
    if (code == null || code <= 0) {
        return null
    }
    if (code > 256) {
        return 'Stopped by a signal, often the walltime limit.'
    }
    // PBS names an array job's files per subjob: plasma.o123.1 for subjob 1 of 123[]
    const jobId = job.job_id ?? ''
    const suffix = jobId.endsWith('[]') ? `${jobId.slice(0, -2)}.<index>` : jobId
    return `Exit code ${code} came from your script. Check the job's output files (${job.name}.o${suffix} and ${job.name}.e${suffix} in the folder it was submitted from, unless the script sets -o or -e).`
}

/** What is true about a job that has not started: how long it has waited, which provisioning attempt
 * it is on, and which queue limit is holding it. Never a queue position or a predicted start. */
export function getJobWaitingSignals(job: SocaJob, now: Date = new Date()): string[] {
    const signals: string[] = []
    const held = job.state === 'held'

    if (Utils.isEmpty(job.start_time)) {
        const queuedSeconds = new JobUtils(job).getQueuedSeconds(now)
        if (queuedSeconds != null) {
            signals.push(`waiting ${formatDurationMinutes(queuedSeconds)}`)
        }
    }

    const attempt = formatProvisioningAttempt(job.provisioning_attempt, job.max_provisioning_attempts, held)
    if (attempt != null) {
        signals.push(attempt)
    }

    const limit = formatBlockingLimit(job.blocking_limit_type)
    if (limit != null) {
        signals.push(limit)
    }

    return signals
}

/** Every project, however many pages the server splits them into. */
export async function listAllProjects(client: {listProjects: (req: ListProjectsRequest) => Promise<ListProjectsResult>}): Promise<Project[]> {
    const projects: Project[] = []
    let cursor: string | undefined = undefined
    do {
        const result: ListProjectsResult = await client.listProjects({paginator: {page_size: 100, cursor}})
        projects.push(...(result.listing ?? []))
        cursor = result.paginator?.cursor
    } while (cursor)
    return projects
}
