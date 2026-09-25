import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter, useLocation, useNavigate} from 'react-router-dom';
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import {JwtTokenClaims} from '../../common/token-utils';
import {ReportingCoverage, ReportingRow, ReportingRows, ReportingSummary} from '../../client/reporting-model';
import {ReportingContent} from './reporting';
import ReportingTable, {DEFAULT_COLUMNS, ReportingMetric} from './reporting-table';
import {clusterDate, validateReportingPeriod} from './reporting-period-picker';

function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<T>((yes, no) => {resolve = yes; reject = no;});
    return {promise, resolve, reject};
}
const coverage = (status: ReportingCoverage['status'] = 'ready', reason = ''): ReportingCoverage => ({status, reason, source_as_of: '2020-01-01T00:00:00Z', available_start: '2020-01-01', available_end: '2020-01-31', missing_days: 0, missing_records: 0, eligible_count: 2, total_count: 3, freshness_spread_seconds: 10});
const row = (key = 'recorded-user'): ReportingRow => ({key, label: key, spend_total: '0.00', spend_by_facet: {jobs: '0.00', desktops: null, desktop_disks: null, shared_storage: null, ai: null}, job_count: 0, node_hours: '0', requested_walltime_hours: '2', elapsed_hours: '3', efficiency_pct: '150', desktop_hours: '0', idle_stops: null, coverage: {spend_total: coverage(), job_count: coverage(), desktop_hours: coverage('estimated'), efficiency_pct: coverage('partial', 'Missing requested walltime.')}});
const summary = (snapshot_id = 'snapshot'): ReportingSummary => ({snapshot_id, expires_at: new Date(Date.now() + 600000).toISOString(), period: {period: 'this_month', start_date: '2020-01-01', end_date: '2020-01-31', start: '2020-01-01T00:00:00Z', end: '2020-02-01T00:00:00Z', provisional: true}, currency: 'USD', timezone: 'Pacific/Honolulu', as_of: '2020-01-01T00:00:00Z', tiles: {total: row('total'), top_project: {...row('top'), label: 'No priced projects', spend_total: null}, job_spend_difference: {...row('difference'), spend_total: '-2.00'}}, coverage: {spend_total: coverage()}, warnings: ['Partial project attribution.']});
const rows = (listing = [row()], cursor?: string): ReportingRows => ({listing, paginator: {page_size: 50, cursor}, total_rows: 123, coverage: {spend_total: coverage()}, warnings: []});
let context: ReturnType<typeof initTestAppContext>;
beforeEach(() => {context = initTestAppContext();});

function History() {
    const location = useLocation();
    const navigate = useNavigate();
    return <><output data-testid="query">{location.pathname}{location.search}</output><button onClick={() => navigate('/reporting?period=last_month')}>Previous period</button><button onClick={() => navigate('/reporting?period=last_30_days')}>Recent period</button><button onClick={() => navigate(-1)}>Back</button></>;
}
function open(path = '/reporting', result = summary()) {
    vi.spyOn(context.auth(), 'isReportingResolved').mockReturnValue(true);
    vi.spyOn(context.auth(), 'canReadReporting').mockReturnValue(true);
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

describe('Reporting periods and snapshots', () => {
    it('validates calendar dates, inclusive duration, ordering and cluster-local future dates', () => {
        expect(clusterDate('Pacific/Honolulu', new Date('2020-03-01T01:00:00Z'))).toBe('2020-02-29');
        expect(validateReportingPeriod({period: 'custom', start_date: '2020-01-01', end_date: '2020-12-31'})).toBeUndefined();
        expect(validateReportingPeriod({period: 'custom', start_date: '2020-01-01', end_date: '2021-01-01'})).toContain('366');
        expect(validateReportingPeriod({period: 'custom', start_date: '2020-02-30', end_date: '2020-03-01'})).toContain('valid');
        expect(validateReportingPeriod({period: 'custom', start_date: '2020-02-02', end_date: '2020-02-01'})).toContain('on or after');
        expect(validateReportingPeriod({period: 'custom', start_date: '2999-01-01', end_date: '2999-01-02'}, 'UTC')).toContain('Future');
    });
    it('uses query dates and sort, reuses the snapshot for tabs and sends opaque cursors', async () => {
        open('/reporting?period=custom&start_date=2020-01-01&end_date=2020-01-31&sort_by=job_count&descending=false');
        vi.mocked(context.reporting().listRows).mockResolvedValue(rows([row()], 'opaque+/='));
        await screen.findByText('recorded-user');
        expect(context.reporting().getSummary).toHaveBeenCalledWith({period: 'custom', start_date: '2020-01-01', end_date: '2020-01-31'});
        expect(context.reporting().listRows).toHaveBeenLastCalledWith({snapshot_id: 'snapshot', table: 'user', sort_by: 'job_count', descending: false, paginator: {page_size: 50}});
        await userEvent.click(screen.getByRole('button', {name: 'Next page'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({paginator: {page_size: 50, cursor: 'opaque+/='}})));
        await userEvent.click(screen.getByRole('tab', {name: 'By project'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({table: 'project', paginator: {page_size: 50}})));
        expect(context.reporting().getSummary).toHaveBeenCalledTimes(1);
        expect(screen.getByTestId('query')).toHaveTextContent('table=project');
        expect(screen.getByTestId('query')).toHaveTextContent('start_date=2020-01-01');
        const header = screen.getByRole('columnheader', {name: /Recorded label/});
        await userEvent.click(header.querySelector('[role="button"]') ?? header);
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({sort_by: 'label', snapshot_id: 'snapshot'})));
        expect(context.reporting().getSummary).toHaveBeenCalledTimes(1);
    });
    it('ignores obsolete period responses and resets paging', async () => {
        open();
        await screen.findByText('recorded-user');
        const old = deferred<ReportingSummary>();
        vi.mocked(context.reporting().getSummary).mockReturnValueOnce(old.promise).mockResolvedValueOnce(summary('recent'));
        await userEvent.click(screen.getByText('Previous period'));
        await userEvent.click(screen.getByText('Recent period'));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenCalledWith(expect.objectContaining({snapshot_id: 'recent'})));
        await act(async () => old.resolve(summary('obsolete')));
        expect(context.reporting().listRows).not.toHaveBeenCalledWith(expect.objectContaining({snapshot_id: 'obsolete'}));
    });
    it('requires explicit reload after expiry and retains rows on server expiry', async () => {
        open();
        await screen.findByText('recorded-user');
        vi.spyOn(context.reporting(), 'exportCsv').mockRejectedValue({errorCode: 'REPORT_EXPIRED', message: 'Snapshot expired. Create a new report.'});
        await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
        expect(await screen.findByText(/Reload explicitly/)).toBeInTheDocument();
        expect(screen.getByText('recorded-user')).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Export CSV'})).toBeDisabled();
        expect(context.reporting().getSummary).toHaveBeenCalledTimes(1);
        vi.mocked(context.reporting().getSummary).mockResolvedValue(summary('fresh'));
        await userEvent.click(screen.getByRole('button', {name: 'Reload snapshot'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenCalledWith(expect.objectContaining({snapshot_id: 'fresh'})));
    });
    it('does not request invalid custom dates', async () => {
        open('/reporting?period=custom&start_date=2020-02-02&end_date=2020-02-01');
        expect((await screen.findAllByText('End date must be on or after start date.')).length).toBeGreaterThan(0);
        expect(context.reporting().getSummary).not.toHaveBeenCalled();
    });
});

describe('Reporting metric presentation and CSV', () => {
    it.each([
        [0, 'ready', '', '0 hours'],
        [null, 'unavailable', 'Collecting stored projection.', 'Collecting / not yet available'],
        ['12', 'partial', 'Stale retained source values.', 'Partial subtotal'],
        [null, 'unavailable', 'Source failed.', 'Source failed.'],
        [null, 'not_applicable', '', '—'],
        [null, 'unavailable', 'Module not deployed.', 'Not deployed']
    ] as const)('preserves %s with %s coverage', (value, status, reason, text) => {
        render(<ReportingMetric value={value} coverage={coverage(status, reason)} unit="hours"/>);
        expect(screen.getByText(new RegExp(text))).toBeInTheDocument();
        if (status === 'not_applicable') expect(screen.getByLabelText('Not applicable')).toBeInTheDocument();
    });
    it('renders missing history, bounds, freshness, eligible jobs and metric definitions', async () => {
        open('/reporting/projects', {...summary(), coverage: {spend_total: {...coverage('partial', 'Incomplete totals.'), missing_days: 4, missing_records: 2}}});
        await screen.findByText('recorded-user');
        expect(screen.getByText(/missing history: 4 days/)).toBeInTheDocument();
        expect(screen.getByText(/current membership is never used/)).toBeInTheDocument();
        expect(screen.getByText(/duration-weighted and may exceed 100%/)).toBeInTheDocument();
        expect(screen.getByText(/2 eligible jobs \/ 3 total jobs/)).toBeInTheDocument();
        expect(screen.getByText(/No priced projects/)).toBeInTheDocument();
        expect(screen.getByText('-2.00 USD')).toBeInTheDocument();
        expect(screen.getByText(/current day is provisional/)).toBeInTheDocument();
    });
    it('shows job and desktop metrics only on applicable facets', () => {
        render(<ReportingTable table="facet" data={rows(['jobs', 'desktops', 'desktop_disks', 'shared_storage', 'ai'].map(key => ({...row(key), job_count: 7, desktop_hours: '9'})))} currency="USD" loading={false} disabled={false} sortBy="label" descending={false} page={1} pageSize={50} columns={DEFAULT_COLUMNS} onSort={vi.fn()} onPage={vi.fn()} onPreferences={vi.fn()}/>);
        expect(screen.getAllByText('7 jobs')).toHaveLength(1);
        expect(screen.getAllByText('9 hours')).toHaveLength(1);
        expect(screen.getAllByText(/Idle stops are not recorded/)).toHaveLength(1);
        expect(screen.getAllByLabelText('Not applicable').length).toBeGreaterThan(20);
    });
    it('exports all pages with selected order and downloads the unchanged server Blob', async () => {
        open();
        await screen.findByText('recorded-user');
        const csv = vi.spyOn(context.reporting(), 'exportCsv').mockResolvedValue({filename: 'server.csv', content_type: 'text/csv;charset=utf-8', content: 'server content\r\n', row_count: 123, as_of: '2020-01-01T00:00:00Z'});
        const create = vi.fn().mockReturnValue('blob:report');
        const revoke = vi.fn();
        vi.stubGlobal('URL', Object.assign(URL, {createObjectURL: create, revokeObjectURL: revoke}));
        const downloads: string[] = [];
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {downloads.push(this.download);});
        await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
        expect(csv).toHaveBeenCalledWith({snapshot_id: 'snapshot', table: 'user', sort_by: 'spend_total', descending: true, columns: DEFAULT_COLUMNS.filter(column => column.visible).map(column => column.id)});
        expect(csv.mock.calls[0][0]).not.toHaveProperty('paginator');
        expect(create.mock.calls[0][0]).toBeInstanceOf(Blob);
        expect(create.mock.calls[0][0].type).toBe('text/csv;charset=utf-8');
        expect(click).toHaveBeenCalled();
        expect(downloads).toEqual(['server.csv']);
        const content = await new Promise(resolve => {const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(create.mock.calls[0][0]);});
        expect(content).toBe('server content\r\n');
        expect(revoke).toHaveBeenCalledWith('blob:report');
        expect(screen.getByText(/Downloaded 123 rows across all pages/)).toHaveAttribute('role', 'status');
    });
    it('retains rows and server narrower-period guidance on failed export', async () => {
        open();
        await screen.findByText('recorded-user');
        vi.spyOn(context.reporting(), 'exportCsv').mockRejectedValue({errorCode: 'REPORT_TOO_LARGE', message: 'Select a narrower period.'});
        await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
        expect(await screen.findByText(/REPORT_TOO_LARGE: Select a narrower period/)).toBeInTheDocument();
        expect(screen.getByText('recorded-user')).toBeInTheDocument();
    });
    it('supports keyboard tabs and preferences with server page sizes', async () => {
        open();
        await screen.findByText('recorded-user');
        screen.getByRole('tab', {name: 'Overview / By user'}).focus();
        fireEvent.keyDown(document.activeElement!, {key: 'ArrowRight', keyCode: 39});
        await userEvent.keyboard('{Enter}');
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenCalledWith(expect.objectContaining({table: 'project'})));
        const preferences = screen.getByRole('button', {name: /preferences/i});
        preferences.focus();
        await userEvent.keyboard('{Enter}');
        const dialog = await screen.findByRole('dialog');
        expect(within(dialog).getByText('Columns and order')).toBeInTheDocument();
        await userEvent.click(within(dialog).getByLabelText('200 rows'));
        await userEvent.click(within(dialog).getByRole('button', {name: 'Confirm'}));
        await waitFor(() => expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({paginator: {page_size: 200}})));
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

it('expires a displayed snapshot without automatically rebuilding it', async () => {
    open('/reporting', {...summary(), expires_at: new Date(Date.now() + 200).toISOString()});
    expect(await screen.findByText(/Reload explicitly/)).toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Export CSV'})).toBeDisabled();
    expect(context.reporting().getSummary).toHaveBeenCalledTimes(1);
});

it('retains the previous page and retries an opaque cursor after a retryable row error', async () => {
    open();
    vi.mocked(context.reporting().listRows).mockResolvedValueOnce(rows([row('first-page')], 'next'));
    await screen.findByText('first-page');
    vi.mocked(context.reporting().listRows).mockRejectedValueOnce({errorCode: 'REPORT_TIMEOUT', message: 'Retry with a narrower period.'}).mockResolvedValueOnce(rows([row('next-page')]));
    await userEvent.click(screen.getByRole('button', {name: 'Next page'}));
    expect(await screen.findByText(/REPORT_TIMEOUT/)).toBeInTheDocument();
    expect(screen.getByText('first-page')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', {name: 'Retry rows'}));
    expect(await screen.findByText('next-page')).toBeInTheDocument();
    expect(context.reporting().listRows).toHaveBeenLastCalledWith(expect.objectContaining({paginator: {page_size: 50, cursor: 'next'}}));
});

it('keeps covered empty data at zero and recorded project labels separate', async () => {
    open();
    vi.mocked(context.reporting().listRows).mockResolvedValueOnce({...rows([]), total_rows: 0});
    expect(await screen.findByText('No recorded rows for this period. See source coverage.')).toBeInTheDocument();
    expect(screen.getByText('0.00 USD')).toBeInTheDocument();
    vi.mocked(context.reporting().listRows).mockResolvedValueOnce(rows([row('Unassigned'), {...row('!unallocated'), label: 'Unallocated to project', spend_by_facet: {desktops: '4.20'}}, {...row('deleted-project'), label: 'Recorded deleted project'}]));
    await userEvent.click(screen.getByRole('tab', {name: 'By project'}));
    expect(await screen.findByText('Unassigned')).toBeInTheDocument();
    expect(screen.getByText('Unallocated to project')).toBeInTheDocument();
    expect(screen.getByText('Recorded deleted project')).toBeInTheDocument();
    expect(screen.getByText('4.20 USD')).toBeInTheDocument();
});

it('uses the confirmed visible column order for all-pages export', async () => {
    open();
    await screen.findByText('recorded-user');
    const csv = vi.spyOn(context.reporting(), 'exportCsv').mockReturnValue(new Promise(() => {}));
    await userEvent.click(screen.getByRole('button', {name: 'Reporting preferences'}));
    const dialog = await screen.findByRole('dialog');
    const handles = within(dialog).getAllByRole('button', {name: /Reorder column/});
    dialog.querySelectorAll('ol li').forEach((item, index) => {
        vi.spyOn(item, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, index * 40, 300, 40));
    });
    handles[0].focus();
    await userEvent.keyboard('[Space]');
    await userEvent.keyboard('[ArrowDown]');
    await userEvent.keyboard('[Space]');
    await userEvent.click(within(dialog).getByRole('checkbox', {name: 'Idle stops'}));
    await userEvent.click(within(dialog).getByRole('button', {name: 'Confirm'}));
    await userEvent.click(screen.getByRole('button', {name: 'Export CSV'}));
    expect(csv.mock.calls[0][0].columns.slice(0, 2)).toEqual(['spend_total', 'label']);
    expect(csv.mock.calls[0][0].columns).not.toContain('idle_stops');
    expect(csv.mock.calls[0][0]).not.toHaveProperty('paginator');
    expect(screen.getByRole('button', {name: 'Export CSV'})).toHaveAttribute('aria-disabled', 'true');
});
