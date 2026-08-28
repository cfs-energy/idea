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
    HpcQueueProfile, SocaJobParams, SocaJob, SocaCapacityType, SocaInstanceTypeOptions
} from "../../client/data-model";
import Utils from "../../common/utils";

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

/** The queue limit holding the job. The type only: the threshold and the current usage describe the
 * whole queue, not this owner. */
export function formatBlockingLimit(limitType?: string | null): string | null {
    if (Utils.isEmpty(limitType)) {
        return null
    }
    return `queue limit: ${limitType}`
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
