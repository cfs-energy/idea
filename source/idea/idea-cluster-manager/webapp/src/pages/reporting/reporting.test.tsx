import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter, useLocation, useNavigate} from 'react-router-dom';
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import {JwtTokenClaims} from '../../common/token-utils';
import {ReportingCoverage, ReportingRow, ReportingRows, ReportingSummary} from '../../client/reporting-model';
import {ReportingContent} from './reporting';
import ReportingTable, {DEFAULT_COLUMNS} from './reporting-table';
import {insightsFixture} from './insights-fixture';
import {clusterDate, validateReportingPeriod} from './reporting-period-picker';

function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<T>((yes, no) => {resolve = yes; reject = no;});
    return {promise, resolve, reject};
}
const coverage = (status: ReportingCoverage['status'] = 'ready', reason = ''): ReportingCoverage => ({status, reason, source_as_of: '2020-01-01T00:00:00Z', available_start: '2020-01-01', available_end: '2020-01-31', missing_days: 0, missing_records: 0, eligible_count: 2, total_count: 3, freshness_spread_seconds: 10});
const row = (key = 'scientist-a'): ReportingRow => ({key, label: key, spend_total: '0.00', spend_by_facet: {jobs: '0.00', desktops: null, desktop_disks: null, shared_storage: null, ai: null}, job_count: 0, node_hours: '0', requested_walltime_hours: '2', elapsed_hours: '3', efficiency_pct: '150', desktop_hours: '0', idle_stops: null, coverage: {spend_total: coverage(), job_count: coverage(), desktop_hours: coverage('estimated'), efficiency_pct: coverage('partial', 'Missing requested walltime.')}});
const summary = (snapshot_id = 'snapshot'): ReportingSummary => ({snapshot_id, expires_at: new Date(Date.now() + 600000).toISOString(), period: {period: 'this_month', start_date: '2020-01-01', end_date: '2020-01-31', start: '2020-01-01T00:00:00Z', end: '2020-02-01T00:00:00Z', provisional: true}, currency: 'USD', timezone: 'Pacific/Honolulu', as_of: '2020-01-01T00:00:00Z', tiles: {total: row('total'), top_project: {...row('top'), label: 'No priced projects', spend_total: null}, job_spend_difference: {...row('difference'), spend_total: '-2.00'}}, coverage: {spend_total: coverage()}, warnings: ['Partial project attribution.']});
const rows = (listing = [row()], cursor?: string): ReportingRows => ({listing, paginator: {page_size: 50, cursor}, total_rows: 123, coverage: {spend_total: coverage()}, warnings: []});
let context: ReturnType<typeof initTestAppContext>;
beforeEach(() => {localStorage.clear(); context = initTestAppContext();});

function History() {
    const location = useLocation();
    const navigate = useNavigate();
    return <><output data-testid="query">{location.pathname}{location.search}</output><button onClick={() => navigate('/reporting?period=last_month')}>Previous period</button><button onClick={() => navigate('/reporting?period=last_30_days')}>Recent period</button><button onClick={() => navigate(-1)}>Back</button></>;
}
function open(path = '/reporting?table=user', result = summary()) {
    vi.spyOn(context.auth(), 'isReportingResolved').mockReturnValue(true);
    vi.spyOn(context.auth(), 'canReadReporting').mockReturnValue(true);
    vi.spyOn(context.reporting(), 'getInsights').mockResolvedValue(insightsFixture());
    vi.spyOn(context.reporting(), 'getSummary').mockResolvedValue(result);
    vi.spyOn(context.reporting(), 'listRows').mockResolvedValue(rows());
    return render(<MemoryRouter initialEntries={[path]}><ReportingContent/><History/></MemoryRouter>);
}

function claims(username = 'subject-one', issued_at = 1): JwtTokenClaims {
    return {username, issued_at, expires_at: Date.parse('2999-01-01'), auth_time: 1, groups: ['custom-access'], email: '', cluster_name: '', aws_region: '', scope: [], email_verified: false};
}

describe('Reporting capability lifecycle', () => {
    it('defaults to denied, resolves startup and reloads after token renewal without role inference', async () => {
        const auth = context.auth();
        const capability = deferred<{can_read_reporting: boolean}>();
        vi.spyOn(context.client().auth(), 'isLoggedIn').mockResolvedValue(true);
        const getClaims = vi.spyOn(context.client().auth(), 'getClaims').mockResolvedValue(claims());
        const getCapabilities = vi.spyOn(context.reporting(), 'getCapabilities').mockReturnValueOnce(capability.promise).mockResolvedValue({can_read_reporting: false});
        expect(auth.canReadReporting()).toBe(false);
        const startup = auth.isLoggedIn();
        await waitFor(() => expect(getCapabilities).toHaveBeenCalledTimes(1));
        expect(auth.isReportingResolved()).toBe(false);
        capability.resolve({can_read_reporting: true});
        await startup;
        expect(auth.canReadReporting()).toBe(true);
        expect(auth.isAdmin()).toBe(false);
        await auth.isLoggedIn();
        expect(getCapabilities).toHaveBeenCalledTimes(1);
        getClaims.mockResolvedValue(claims('subject-one', 2));
        await auth.isLoggedIn();
        expect(getCapabilities).toHaveBeenCalledTimes(2);
        expect(auth.canReadReporting()).toBe(false);
    });
    it('clears grants on capability failure and logout, including a delayed obsolete user response', async () => {
        vi.spyOn(context.client().auth(), 'isLoggedIn').mockResolvedValue(true);
        const getClaims = vi.spyOn(context.client().auth(), 'getClaims').mockResolvedValue(claims());
        const old = deferred<{can_read_reporting: boolean}>();
        const capabilities = vi.spyOn(context.reporting(), 'getCapabilities').mockReturnValueOnce(old.promise).mockResolvedValue({can_read_reporting: false});
        vi.spyOn(context.client().auth(), 'logout').mockResolvedValue(true);
        const prior = context.auth().isLoggedIn();
        await waitFor(() => expect(capabilities).toHaveBeenCalledTimes(1));
        await context.auth().logout();
        getClaims.mockResolvedValue(claims('subject-two', 2));
        await context.auth().isLoggedIn();
        old.resolve({can_read_reporting: true});
        await prior;
        expect(context.auth().getUsername()).toBe('subject-two');
        expect(context.auth().canReadReporting()).toBe(false);
        capabilities.mockResolvedValueOnce({can_read_reporting: true});
        getClaims.mockResolvedValue(claims('subject-two', 3));
        await context.auth().isLoggedIn();
        expect(context.auth().canReadReporting()).toBe(true);
        capabilities.mockRejectedValueOnce(new Error('Source unavailable'));
        getClaims.mockResolvedValue(claims('subject-two', 4));
        await context.auth().isLoggedIn();
        expect(context.auth().isReportingResolved()).toBe(true);
        expect(context.auth().canReadReporting()).toBe(false);
        await context.auth().logout();
        expect(context.auth().isReportingResolved()).toBe(false);
    });
    it('ignores claims fetched before logout and clears a failed authentication check', async () => {
        const pending = deferred<JwtTokenClaims>();
        const status = vi.spyOn(context.client().auth(), 'isLoggedIn').mockResolvedValue(true);
        const getClaims = vi.spyOn(context.client().auth(), 'getClaims').mockReturnValue(pending.promise);
        const capability = vi.spyOn(context.reporting(), 'getCapabilities').mockResolvedValue({can_read_reporting: true});
        const prior = context.auth().isLoggedIn();
        await waitFor(() => expect(getClaims).toHaveBeenCalled());
        context.auth().clearSession();
        pending.resolve(claims());
        await prior;
        expect(capability).not.toHaveBeenCalled();
        getClaims.mockResolvedValue(claims());
        await context.auth().isLoggedIn();
        status.mockRejectedValueOnce(new Error('Session unavailable'));
        await expect(context.auth().isLoggedIn()).rejects.toThrow('Session unavailable');
        expect(context.auth().canReadReporting()).toBe(false);
    });
});

it('refreshes capabilities before an authenticated RPC and suppresses revoked Reporting requests', async () => {
    vi.spyOn(context.client().auth(), 'isLoggedIn').mockResolvedValue(true);
    const getClaims = vi.spyOn(context.client().auth(), 'getClaims').mockResolvedValue(claims());
    const capability = vi.spyOn(context.reporting(), 'getCapabilities').mockResolvedValueOnce({can_read_reporting: true}).mockResolvedValue({can_read_reporting: false});
    const invoke = vi.spyOn(context.client().auth().props.authContext!, 'invoke').mockResolvedValue({success: true, payload: {}});
    await context.auth().isLoggedIn();
    getClaims.mockResolvedValue(claims('subject-one', 2));
    await expect(context.reporting().getSummary({period: 'this_month'})).rejects.toMatchObject({message: 'Access denied'});
    expect(capability).toHaveBeenCalledTimes(2);
    expect(invoke).not.toHaveBeenCalled();
});

it('resolves an SSO capability and clears it when SSO fails', async () => {
    const initiate = vi.spyOn(context.client().auth(), 'initiateAuth').mockResolvedValue({});
    vi.spyOn(context.client().auth(), 'getClaims').mockResolvedValue(claims());
    vi.spyOn(context.reporting(), 'getCapabilities').mockResolvedValue({can_read_reporting: true});
    expect(await context.auth().login_using_sso_auth_code('code')).toBe(true);
    expect(context.auth().canReadReporting()).toBe(true);
    initiate.mockRejectedValueOnce(new Error('Login failed'));
    await expect(context.auth().login_using_sso_auth_code('code')).rejects.toThrow('Login failed');
    expect(context.auth().canReadReporting()).toBe(false);
});


describe('report views', () => {
    it.each([
        ['overview', 'Daily job cost by project'], ['jobs', 'Top jobs'], ['desktops', 'Daily desktop cost by user'],
        ['storage', 'Storage by tier'], ['user', 'scientist-a'], ['project', 'scientist-a']
    ])('renders the %s tab with a mocked insights response', async (tab, heading) => {
        open(`/reporting?table=${tab}`);
        expect((await screen.findAllByText(heading)).length).toBeGreaterThan(0);
        expect(context.reporting().getInsights).toHaveBeenCalledWith({period: 'this_month'});
        if (tab === 'jobs') expect(screen.getByRole('heading', {name: 'Top jobs (1)'})).toBeInTheDocument();
        expect(screen.getByRole('tab', {name: ({overview: 'Overview', jobs: 'Jobs', desktops: 'Desktops', storage: 'Storage', user: 'Breakdown', project: 'Breakdown'} as Record<string, string>)[tab]})).toHaveAttribute('aria-selected', 'true');
        expect(document.body.textContent).not.toMatch(/\b(facet|projection|index|coverage|eligible|freshness|spread|snapshot|provisional|allocation|v1|recorded label|unavailable)\b/i);
        expect(document.body.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
    });
    it('validates custom dates and does not fetch invalid periods', async () => {
        expect(clusterDate('Pacific/Honolulu', new Date('2020-03-01T01:00:00Z'))).toBe('2020-02-29');
        expect(validateReportingPeriod({period: 'custom', start_date: '2020-01-01', end_date: '2021-01-01'})).toContain('366');
        expect(validateReportingPeriod({period: 'custom', start_date: '2020-02-30', end_date: '2020-03-01'})).toContain('valid');
        open('/reporting?period=custom&start_date=2020-02-02&end_date=2020-02-01');
        expect((await screen.findAllByText('End date must be on or after start date.')).length).toBeGreaterThan(0);
        expect(context.reporting().getInsights).not.toHaveBeenCalled();
    });
    it('preserves period, sort and opaque cursors when switching tables', async () => {
        open('/reporting?table=user&period=custom&start_date=2020-01-01&end_date=2020-01-31&sort_by=job_count&descending=false');
        vi.mocked(context.reporting().listRows).mockResolvedValue(rows([row()], 'opaque+/='));
        await screen.findByText('scientist-a');
        expect(context.reporting().getInsights).toHaveBeenCalledWith({period: 'custom', start_date: '2020-01-01', end_date: '2020-01-31'});
        await userEvent.click(screen.getByRole('button', {name: 'Next page'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({sort_by: 'job_count', descending: false, paginator: {page_size: 25, cursor: 'opaque+/='}})));
        await userEvent.click(screen.getByRole('button', {name: 'Project'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({table: 'project', paginator: {page_size: 25}})));
        expect(context.reporting().getInsights).toHaveBeenCalledTimes(1);
        expect(screen.getByTestId('query')).toHaveTextContent('start_date=2020-01-01');
    });
    it('ignores late results after changing the period', async () => {
        open();
        await screen.findByText('scientist-a');
        const old = deferred<ReportingSummary>();
        vi.mocked(context.reporting().getSummary).mockReturnValueOnce(old.promise).mockResolvedValueOnce(summary('recent'));
        await userEvent.click(screen.getByText('Previous period'));
        await userEvent.click(screen.getByText('Recent period'));
        await screen.findByText('Daily job cost by project');
        await act(async () => old.resolve(summary('obsolete')));
        await userEvent.click(screen.getByRole('tab', {name: 'Breakdown'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenCalledWith(expect.objectContaining({snapshot_id: 'recent'})));
        expect(context.reporting().listRows).not.toHaveBeenCalledWith(expect.objectContaining({snapshot_id: 'obsolete'}));
    });
    it('hides empty columns and puts structured explanations behind info buttons', async () => {
        open();
        await screen.findByText('scientist-a');
        expect(screen.queryByRole('columnheader', {name: /Idle stops/})).toBeNull();
        expect(screen.queryByRole('columnheader', {name: /Desktop spend/})).toBeNull();
        expect(screen.getAllByText('$0.00').length).toBeGreaterThan(0);
        expect(screen.queryByText(/Uses 2 of 3 records/)).toBeNull();
        await userEvent.click(screen.getByRole('button', {name: 'About Walltime efficiency'}));
        expect(await screen.findByText(/Uses 2 of 3 records/)).toBeInTheDocument();
        expect(document.body.textContent).not.toMatch(/\b(coverage|eligible|snapshot)\b/i);
    });
    it('shows an accessible dash for a missing cell while preserving zeros', () => {
        render(<ReportingTable table="user" data={rows([row(), {...row('scientist-b'), spend_total: null}])} currency="USD" loading={false} disabled={false} sortBy="label" descending={false} page={1} pageSize={25} columns={DEFAULT_COLUMNS} onSort={vi.fn()} onPage={vi.fn()} onPreferences={vi.fn()}/>);
        expect(screen.getByLabelText('No data')).toHaveTextContent('—');
        expect(screen.getAllByText('$0.00').length).toBeGreaterThan(0);
    });
    it('downloads server CSV unchanged with all selected columns regardless of the current page', async () => {
        open();
        await screen.findByText('scientist-a');
        const csv = vi.spyOn(context.reporting(), 'exportCsv').mockResolvedValue({filename: 'server.csv', content_type: 'text/csv;charset=utf-8', content: 'server content\r\n', row_count: 123, as_of: null});
        const create = vi.fn().mockReturnValue('blob:report'), revoke = vi.fn();
        vi.stubGlobal('URL', Object.assign(URL, {createObjectURL: create, revokeObjectURL: revoke}));
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
        await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
        expect(csv).toHaveBeenCalledWith({snapshot_id: 'snapshot', table: 'user', sort_by: 'spend_total', descending: true, columns: DEFAULT_COLUMNS.filter(column => column.visible).map(column => column.id)});
        expect(click).toHaveBeenCalled();
        const content = await new Promise(resolve => {const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(create.mock.calls[0][0]);});
        expect(content).toBe('server content\r\n');
        expect(revoke).toHaveBeenCalledWith('blob:report');
        expect(screen.getByText('Downloaded 123 rows across all pages.')).toHaveAttribute('role', 'status');
    });
    it('refreshes an expired export and hides the old snapshot', async () => {
        open();
        await screen.findByText('scientist-a');
        const fresh = deferred<ReportingSummary>();
        vi.mocked(context.reporting().getSummary).mockReturnValueOnce(fresh.promise);
        vi.spyOn(context.reporting(), 'exportCsv').mockRejectedValue({errorCode: 'REPORT_EXPIRED'});
        await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
        await waitFor(() => expect(context.reporting().getSummary).toHaveBeenCalledTimes(2));
        expect(screen.queryByText('scientist-a')).toBeNull();
        expect(screen.queryByText(/expired|No costs or activity/)).toBeNull();
        expect(screen.getByRole('status', {name: 'CSV download status'})).toHaveTextContent('CSV download failed. Reload the report and try again.');
        await act(async () => fresh.resolve(summary('fresh')));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({snapshot_id: 'fresh'})));
    });
    it('filters the current page and allows column preferences', async () => {
        open();
        await screen.findByText('scientist-a');
        await userEvent.type(screen.getByRole('searchbox', {name: 'Find on this page'}), 'no match');
        expect(screen.getByText('No matches on this page')).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Reporting preferences'}));
        const dialog = await screen.findByRole('dialog');
        expect(within(dialog).queryByText('Idle stops')).toBeNull();
        await userEvent.click(within(dialog).getByLabelText('100 rows'));
        await userEvent.click(within(dialog).getByRole('button', {name: 'Confirm'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({paginator: {page_size: 100}})));
    });
});

it('loads rows without a reload loop when the browser clock is past the expiry time', async () => {
    open('/reporting?table=user', {...summary(), expires_at: new Date(Date.now() - 60000).toISOString()});
    expect(await screen.findByText('scientist-a')).toBeInTheDocument();
    await new Promise(resolve => setTimeout(resolve, 1500));
    expect(context.reporting().getSummary).toHaveBeenCalledTimes(1);
});
it('hides the previous page and retries its next cursor after a row error', async () => {
    open();
    vi.mocked(context.reporting().listRows).mockResolvedValueOnce(rows([row('scientist-a')], 'next'));
    await screen.findByText('scientist-a');
    vi.mocked(context.reporting().listRows).mockRejectedValueOnce(new Error('Temporary failure')).mockResolvedValueOnce(rows([row('scientist-b')]));
    await userEvent.click(screen.getByRole('button', {name: 'Next page'}));
    await screen.findByText("Couldn't load the report. Check your connection and try again.");
    expect(screen.queryByText('scientist-a')).toBeNull();
    await userEvent.click(screen.getByRole('button', {name: 'Try again'}));
    expect(await screen.findByText('scientist-b')).toBeInTheDocument();
    expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({paginator: {page_size: 25, cursor: 'next'}}));
});

it.each(['REPORT_SNAPSHOT_EXPIRED', 'REPORT_EXPIRED', 'REPORT_SNAPSHOT_NOT_FOUND'])('hides prior rows after %s', async errorCode => {
    open();
    vi.mocked(context.reporting().listRows).mockResolvedValueOnce(rows([row()], 'next'));
    await screen.findByText('scientist-a');
    const fresh = deferred<ReportingSummary>();
    vi.mocked(context.reporting().getSummary).mockReturnValueOnce(fresh.promise);
    vi.mocked(context.reporting().listRows).mockRejectedValueOnce({errorCode});
    await userEvent.click(screen.getByRole('button', {name: 'Next page'}));
    await waitFor(() => expect(context.reporting().getSummary).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('scientist-a')).toBeNull();
    expect(screen.queryByText(/expired|No costs or activity/)).toBeNull();
    await act(async () => fresh.resolve(summary('fresh')));
    await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({snapshot_id: 'fresh', paginator: {page_size: 25}})));
});
it('never shows a table empty state while initial rows are pending', async () => {
    open();
    const pending = deferred<ReportingRows>();
    vi.mocked(context.reporting().listRows).mockReturnValueOnce(pending.promise);
    await screen.findByRole('tab', {name: 'Breakdown'});
    expect(screen.queryByText('No costs or activity in this period')).toBeNull();
    await act(async () => pending.resolve(rows([])));
    expect(await screen.findByText('No costs or activity in this period')).toBeInTheDocument();
});
it('keeps Breakdown selection and period in the URL through browser back', async () => {
    open('/reporting/projects?period=last_month&sort_by=job_count&descending=false');
    await screen.findByText('scientist-a');
    expect(screen.getByRole('tab', {name: 'Breakdown'})).toHaveAttribute('aria-selected', 'true');
    expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({table: 'project'}));
    await userEvent.click(screen.getByRole('button', {name: 'User'}));
    expect(screen.getByTestId('query')).toHaveTextContent('table=breakdown&group=user');
    expect(screen.getByTestId('query')).toHaveTextContent('period=last_month&sort_by=job_count&descending=false');
    await userEvent.click(screen.getByRole('button', {name: 'Back'}));
    await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({table: 'project'})));
});

it.each([
    ['REPORT_TOO_LARGE', 'The report is too large to load. Choose a shorter period and try again.'],
    ['REPORT_TIMEOUT', 'The report took too long to load. Choose a shorter period and try again.'],
    ['UNAUTHORIZED_ACCESS', 'Reporting access was denied. Ask your administrator for access.']
])('names the cause and action for %s', async (errorCode, message) => {
    open();
    vi.mocked(context.reporting().listRows).mockRejectedValueOnce({errorCode});
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Try again'})).toBeInTheDocument();
});


it.each(['overview', 'jobs', 'breakdown'])('hides old %s data while a new period loads and after failure', async tab => {
    open(`/reporting?table=${tab}`);
    await screen.findByRole('tab', {name: 'Breakdown'});
    if (tab === 'breakdown') await screen.findByText('scientist-a');
    const pending = deferred<ReportingSummary>();
    vi.mocked(context.reporting().getSummary).mockReturnValueOnce(pending.promise);
    await userEvent.click(screen.getByText('Previous period'));
    expect(screen.getByText('Loading report')).toBeInTheDocument();
    expect(screen.queryByText('scientist-a')).toBeNull();
    expect(screen.queryByText('Protein study')).toBeNull();
    expect(screen.queryByText('Daily job cost by project')).toBeNull();
    expect(screen.queryByText('$1,524.22')).toBeNull();
    await act(async () => pending.reject(new Error('Read failed')));
    expect(screen.getByText("Couldn't load the report. Check your connection and try again.")).toBeInTheDocument();
    expect(screen.queryByText('$1,524.22')).toBeNull();
    expect(screen.queryByRole('tab', {name: 'Breakdown'})).toBeNull();
});
it('hides prior snapshot rows until the new snapshot rows arrive', async () => {
    open();
    await screen.findByText('scientist-a');
    const pending = deferred<ReportingRows>();
    vi.mocked(context.reporting().getSummary).mockResolvedValueOnce(summary('fresh'));
    vi.mocked(context.reporting().listRows).mockReturnValueOnce(pending.promise);
    await userEvent.click(screen.getByRole('button', {name: 'Reload'}));
    await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({snapshot_id: 'fresh'})));
    expect(screen.queryByText('scientist-a')).toBeNull();
    await act(async () => pending.reject(new Error('Read failed')));
    expect(screen.queryByText('scientist-a')).toBeNull();
    expect(screen.getByText("Couldn't load the report. Check your connection and try again.")).toBeInTheDocument();
});
it.each(['REPORT_SNAPSHOT_EXPIRED', 'REPORT_EXPIRED', 'REPORT_SNAPSHOT_NOT_FOUND'])('automatically reloads each snapshot only once for %s', async errorCode => {
    open();
    vi.mocked(context.reporting().listRows).mockRejectedValue({errorCode});
    await screen.findByText('The report is no longer available. Reload to try again.');
    expect(context.reporting().getSummary).toHaveBeenCalledTimes(2);
    expect(context.reporting().listRows).toHaveBeenCalledTimes(2);
});
it('downloads selected columns absent from this page even when the browser clock is ahead', async () => {
    localStorage.setItem('reporting.breakdown.user', JSON.stringify({pageSize: 25, columns: [{id: 'label', visible: true}, {id: 'idle_stops', visible: true}, {id: 'jobs', visible: false}]}));
    open('/reporting?table=user', {...summary(), expires_at: '2000-01-01T00:00:00Z'});
    await screen.findByText('scientist-a');
    const csv = vi.spyOn(context.reporting(), 'exportCsv').mockResolvedValue({filename: 'report.csv', content_type: 'text/csv;charset=utf-8', content: 'label,idle_stops', row_count: 123, as_of: null});
    vi.stubGlobal('URL', Object.assign(URL, {createObjectURL: vi.fn().mockReturnValue('blob:report'), revokeObjectURL: vi.fn()}));
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
    expect(csv).toHaveBeenCalledWith(expect.objectContaining({columns: ['label', 'idle_stops']}));
    expect(click).toHaveBeenCalled();
    expect(screen.getByRole('status', {name: 'CSV download status'})).toHaveTextContent('Downloaded 123 rows across all pages.');
});
it('explains why export cannot run while the report is loading', async () => {
    open();
    const pending = deferred<ReportingSummary>();
    vi.mocked(context.reporting().getSummary).mockReturnValueOnce(pending.promise);
    await screen.findByText('scientist-a');
    await userEvent.click(screen.getByRole('button', {name: 'Reload'}));
    await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
    expect(screen.getByRole('status', {name: 'CSV download status'})).toHaveTextContent('CSV download is not ready. Wait for the report and select at least one column.');
    await act(async () => pending.resolve(summary('fresh')));
});
it('reports a cancelled export when the selection changes during the request', async () => {
    open();
    await screen.findByText('scientist-a');
    const pending = deferred<Awaited<ReturnType<ReturnType<typeof context.reporting>['exportCsv']>>>();
    vi.spyOn(context.reporting(), 'exportCsv').mockReturnValueOnce(pending.promise);
    await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
    await userEvent.click(screen.getByText('Previous period'));
    await act(async () => pending.resolve({filename: 'report.csv', content_type: 'text/csv;charset=utf-8', content: '', row_count: 0, as_of: null}));
    expect(screen.getByRole('status', {name: 'CSV download status'})).toHaveTextContent('CSV download cancelled because the report selection changed. Try again.');
});
