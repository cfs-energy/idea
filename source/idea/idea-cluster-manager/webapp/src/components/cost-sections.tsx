import React, {useState} from "react";
import {Badge, Box, Button, ColumnLayout, Container, Header, SpaceBetween, Table} from "@cloudscape-design/components";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {GetMyCostsSummaryResult, MyCostsDesktopSession, MyCostsJob, MyCostsJobGroup} from "../client/data-model";
import Utils from "../common/utils";

/**
 * The three cost sections, rendered from a summary payload. Shared by the self scoped My Costs
 * page and the admin drill-in so both show a user the same way.
 */

export const ESTIMATED_NOTE = 'These are the costs IDEA measured, not the AWS bill. Treat them as estimates.'

export const money = (value?: number): string => (value == null ? '-' : Utils.getFormattedAmount({amount: value}))

export const number = (value?: number): string => (value == null ? '-' : value.toLocaleString())

export const hours = (value?: number): string => (value == null ? '-' : `${value.toFixed(2)} h`)

const estimatedBadge = () => <Badge color="grey">Estimated</Badge>

/**
 * Formats a subtotal. Reads "Not available" when no row could be priced, and carries a marker
 * when only some rows could, so an incomplete total never reads as the whole spend.
 */
export const summaryCost = (cost?: number, rowCount?: number, unpriced?: number): string => {
    const rows = rowCount ?? 0
    const missing = unpriced ?? 0
    if (rows > 0 && missing >= rows) {
        return 'Not available'
    }
    return missing > 0 ? `${money(cost)} *` : money(cost)
}

export const unpricedNote = (unpriced?: number, rowCount?: number, noun?: string) => {
    const rows = rowCount ?? 0
    const missing = unpriced ?? 0
    if (missing === 0 || missing >= rows) {
        return null
    }
    return (
        <Box variant="small" color="text-body-secondary">
            {`* ${missing} of ${rows} ${noun} could not be priced and are not in this total.`}
        </Box>
    )
}

const empty = (title: string, message: string) => (
    <Box textAlign="center" color="inherit">
        <b>{title}</b>
        <Box variant="p" color="inherit">{message}</Box>
    </Box>
)

interface AiRow {
    id: string
    label: string
    total_tokens: number
    invocations: number
    cost?: number
    cost_unavailable?: boolean
    children?: AiRow[]
}

const AI_COLUMNS: TableProps.ColumnDefinition<AiRow>[] = [
    {id: 'label', header: 'Project / model', cell: (item) => item.label},
    {id: 'requests', header: 'Requests', cell: (item) => number(item.invocations)},
    {id: 'tokens', header: 'Tokens', cell: (item) => number(item.total_tokens)},
    {id: 'cost', header: 'Cost', cell: (item) => (item.cost_unavailable ? 'Not available' : money(item.cost))}
]

const JOB_COLUMNS: TableProps.ColumnDefinition<MyCostsJob>[] = [
    {id: 'job_id', header: 'Job', cell: (item) => item.job_id},
    {id: 'name', header: 'Name', cell: (item) => item.name},
    {id: 'queue', header: 'Queue', cell: (item) => item.queue},
    {id: 'project', header: 'Project', cell: (item) => item.project},
    {id: 'end_time', header: 'Finished', cell: (item) => item.end_time},
    // A job the scheduler could not price must not read as a free job.
    {id: 'cost', header: 'Cost', cell: (item) => (item.cost_unavailable ? 'Price not available' : money(item.cost))}
]

const GROUP_COLUMNS = (header: string): TableProps.ColumnDefinition<MyCostsJobGroup>[] => [
    {id: 'name', header: header, cell: (item) => item.name},
    {id: 'job_count', header: 'Jobs', cell: (item) => number(item.job_count)},
    {id: 'cost', header: 'Cost', cell: (item) => money(item.cost)}
]

const DESKTOP_COLUMNS: TableProps.ColumnDefinition<MyCostsDesktopSession>[] = [
    {id: 'name', header: 'Session', cell: (item) => item.name},
    {id: 'instance_type', header: 'Instance type', cell: (item) => item.instance_type},
    {id: 'state', header: 'State', cell: (item) => item.state},
    {
        id: 'hours',
        header: 'Hours',
        // An inferred stop time is an upper bound, so the hours are marked estimated.
        cell: (item) => (item.stop_time_estimated ? `${hours(item.hours)} (estimated)` : hours(item.hours))
    },
    {id: 'cost', header: 'Cost', cell: (item) => (item.price_unavailable ? 'Price not available' : money(item.cost))}
]

export interface CostSectionsProps {
    summary: GetMyCostsSummaryResult | null
    loading: boolean
    error?: string | null
    onRetry?: () => void
    /** Empty state wording: "You have not" reads wrong on another user's page. */
    subject?: 'self' | 'user'
}

/**
 * Hides the AI section for a user with no tokens in the window, rather than showing it empty. A
 * failed read still shows the section, because unavailable is not the same as unused.
 */
export const showAiSection = (summary: GetMyCostsSummaryResult | null, loading: boolean): boolean => {
    const ai = summary?.ai
    if (loading || ai?.is_unavailable) {
        return true
    }
    return (ai?.total_tokens ?? 0) > 0 || (ai?.invocations ?? 0) > 0
}

const CostSections: React.FC<CostSectionsProps> = ({summary, loading, error, onRetry, subject = 'self'}) => {

    const [expandedAi, setExpandedAi] = useState<AiRow[]>([])

    const who = subject === 'self' ? 'You have' : 'This user has'

    const aiRows = (): AiRow[] => (summary?.ai?.projects ?? []).map((project) => ({
        id: project.project_id ?? '',
        label: project.project_title ?? project.project_name ?? '',
        total_tokens: project.total_tokens ?? 0,
        invocations: project.invocations ?? 0,
        cost: project.cost,
        cost_unavailable: project.cost_unavailable,
        children: (project.by_model ?? []).map((model) => ({
            id: `${project.project_id}/${model.model_id}`,
            label: model.model_id ?? '',
            total_tokens: model.total_tokens ?? 0,
            invocations: model.invocations ?? 0,
            cost: model.cost,
            cost_unavailable: project.cost_unavailable
        }))
    }))

    const sectionHeader = (title: string, description: string) => (
        <Header variant="h2" description={description} actions={estimatedBadge()}>{title}</Header>
    )

    const tableEmpty = (title: string, message: string) => {
        if (error != null) {
            return (
                <Box textAlign="center" color="inherit">
                    <b>Could not load these costs</b>
                    <Box variant="p" color="inherit">{error}</Box>
                    {onRetry != null && <Button onClick={onRetry}>Retry</Button>}
                </Box>
            )
        }
        return empty(title, message)
    }

    const ai = summary?.ai
    const jobs = summary?.jobs
    const desktops = summary?.desktops

    const renderAi = () => (
        <Container header={sectionHeader('AI usage', "Bedrock tokens IDEA attributed, and the share of each project's Bedrock spend they account for.")}>
            <SpaceBetween size="s">
                <ColumnLayout columns={3} variant="text-grid">
                    <div>
                        <Box variant="awsui-key-label">Requests</Box>
                        <Box>{number(ai?.invocations)}</Box>
                    </div>
                    <div>
                        <Box variant="awsui-key-label">Tokens</Box>
                        <Box>{number(ai?.total_tokens)}</Box>
                    </div>
                    <div>
                        <Box variant="awsui-key-label">Cost</Box>
                        <Box>{money(ai?.cost)}</Box>
                    </div>
                </ColumnLayout>
                <Table
                    variant="embedded"
                    trackBy="id"
                    items={aiRows()}
                    loading={loading}
                    loadingText="Retrieving AI usage ..."
                    columnDefinitions={AI_COLUMNS}
                    ariaLabels={{
                        expandButtonLabel: (item) => `Show models for ${item.label}`,
                        collapseButtonLabel: (item) => `Hide models for ${item.label}`
                    }}
                    expandableRows={{
                        getItemChildren: (item) => item.children ?? [],
                        isItemExpandable: (item) => (item.children?.length ?? 0) > 0,
                        expandedItems: expandedAi,
                        onExpandableItemToggle: ({detail}) => {
                            const rest = expandedAi.filter((row) => row.id !== detail.item.id)
                            setExpandedAi(detail.expanded ? [...rest, detail.item] : rest)
                        }
                    }}
                    empty={ai?.is_unavailable
                        ? empty('AI usage is not available', 'IDEA could not read the Bedrock usage.')
                        : tableEmpty('No AI usage', `${who} not invoked a model in a project in the last 30 days.`)}
                />
            </SpaceBetween>
        </Container>
    )

    const renderDesktops = () => (
        <Container header={sectionHeader('Desktops', 'Hours IDEA recorded for virtual desktops, priced at the on-demand rate for the instance type.')}>
            <SpaceBetween size="s">
                <ColumnLayout columns={3} variant="text-grid">
                    <div>
                        <Box variant="awsui-key-label">Sessions</Box>
                        <Box>{number(desktops?.session_count)}</Box>
                    </div>
                    <div>
                        <Box variant="awsui-key-label">Hours</Box>
                        <Box>{hours(desktops?.hours)}</Box>
                    </div>
                    <div>
                        <Box variant="awsui-key-label">Cost</Box>
                        <Box>{summaryCost(desktops?.cost, desktops?.session_count, desktops?.unpriced_sessions)}</Box>
                    </div>
                </ColumnLayout>
                {unpricedNote(desktops?.unpriced_sessions, desktops?.session_count, 'sessions')}
                <Table
                    variant="embedded"
                    trackBy="idea_session_id"
                    items={desktops?.sessions ?? []}
                    loading={loading}
                    loadingText="Retrieving desktops ..."
                    columnDefinitions={DESKTOP_COLUMNS}
                    empty={desktops?.is_unavailable
                        ? empty('Desktop costs are not available', 'IDEA could not read the desktop sessions.')
                        : tableEmpty('No desktops', `${who} not run a virtual desktop in the last 30 days.`)}
                />
            </SpaceBetween>
        </Container>
    )

    const renderJobs = () => (
        <Container header={sectionHeader('Jobs', 'Jobs completed in the window, priced from the estimate the scheduler recorded for each one.')}>
            <SpaceBetween size="s">
                <ColumnLayout columns={2} variant="text-grid">
                    <div>
                        <Box variant="awsui-key-label">Completed jobs</Box>
                        <Box>{number(jobs?.job_count)}</Box>
                    </div>
                    <div>
                        <Box variant="awsui-key-label">Cost</Box>
                        <Box>{jobs?.cost_unavailable
                            ? 'Not available'
                            : summaryCost(jobs?.cost, jobs?.job_count, jobs?.unpriced_jobs)}</Box>
                    </div>
                </ColumnLayout>
                {unpricedNote(jobs?.unpriced_jobs, jobs?.job_count, 'jobs')}
                <Table
                    variant="embedded"
                    trackBy="name"
                    items={jobs?.by_project ?? []}
                    loading={loading}
                    loadingText="Retrieving jobs ..."
                    columnDefinitions={GROUP_COLUMNS('Project')}
                    empty={jobs?.is_unavailable
                        ? empty('Job costs are not available', 'IDEA could not read the completed jobs.')
                        : tableEmpty('No completed jobs', `${who} not completed a job in the last 30 days.`)}
                />
                <Table
                    variant="embedded"
                    trackBy="name"
                    items={jobs?.by_queue ?? []}
                    loading={loading}
                    loadingText="Retrieving jobs ..."
                    columnDefinitions={GROUP_COLUMNS('Queue')}
                    empty={empty('No queues', 'There are no jobs to break down by queue.')}
                />
                <Table
                    variant="embedded"
                    trackBy="job_id"
                    header={<Header variant="h3">Most recent jobs</Header>}
                    items={jobs?.recent_jobs ?? []}
                    loading={loading}
                    loadingText="Retrieving jobs ..."
                    columnDefinitions={JOB_COLUMNS}
                    empty={empty('No completed jobs', `${who} not completed a job in the last 30 days.`)}
                />
            </SpaceBetween>
        </Container>
    )

    return (
        <SpaceBetween size="l">
            {showAiSection(summary, loading) && renderAi()}
            {renderDesktops()}
            {renderJobs()}
        </SpaceBetween>
    )
}

export default CostSections
