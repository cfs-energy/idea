import {JobRow, ReportingInsights} from '../../client/reporting-model';

export const exampleJob: JobRow = {job_id: 'job-1', name: 'Protein study', owner: 'scientist-a', project: 'Project Cedar', queue: 'compute', instance_type: 'c6i.large', nodes: 1,
    finished_at: '2026-09-29T20:33:00Z', elapsed_hours: 2.5, cost: '1524.22', cpu_efficiency_pct: 14, memory_efficiency_pct: 60, walltime_efficiency_pct: 80, wasted_core_hours: 12.5};
export const insightsFixture = (): ReportingInsights => ({
    period: {start: '2026-09-01', end: '2026-09-29', label: 'This month'}, currency: 'USD', updated_at: '2026-09-29T20:33:00Z',
    jobs: {count: 3, cost: '1524.22', savings: '200.00', cpu_efficiency_pct: 14, cpu_efficiency_weighted_pct: 20, memory_efficiency_pct: 60, walltime_efficiency_pct: 80,
        wasted_core_hours: 12.5, wasted_cost: '120.00', jobs_with_efficiency: 2,
        by_user: [{name: 'scientist-a', cost: '1524.22', count: 3, share_pct: 100}], by_project: [{name: 'Project Cedar', cost: '1524.22', count: 3, share_pct: 100}],
        by_queue: [{name: 'compute', cost: '1524.22', count: 3, share_pct: 100}], by_instance_family: [{name: 'c6i', cost: '1524.22', count: 3, share_pct: 100}],
        daily_by_project: [{date: '2026-09-29', project: 'Project Cedar', cost: '1524.22'}], costliest: [exampleJob], least_efficient: [exampleJob]},
    desktops: {cost: '10.00', hours: 3.5, by_user: [{name: 'scientist-a', cost: '10.00', count: null, share_pct: 100}], by_project: [{name: 'Project Cedar', cost: '10.00', count: null, share_pct: 100}], daily_top_users: [{date: '2026-09-29', user: 'scientist-a', cost: '10.00'}]},
    storage: {cost: '5.50', used_bytes: 2 ** 40, by_user: [{name: 'scientist-a', bytes: 2 ** 40, cost: '5.50'}], tier_daily: [{date: '2026-09-29', tier: 'ssd', bytes: 2 ** 30}, {date: '2026-09-29', tier: 'capacity_pool', bytes: 2 ** 40}]},
    budgets: [{project: 'Project Cedar', budget_name: 'Research', limit: '2000.00', spent: '1500.00', forecast: '1800.00', pct_at_forecast: 90, headroom: '200.00', status: 'watch'}], notes: []
});
