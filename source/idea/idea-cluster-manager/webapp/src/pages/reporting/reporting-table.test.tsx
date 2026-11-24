import {render, screen} from '@testing-library/react';
import ReportingTable, {DEFAULT_COLUMNS} from './reporting-table';

const row = (key: string, spend: string | null) => ({key, label: key, spend_total: spend, spend_by_facet: {}, coverage: {}}) as any;
const data = (listing: any[], total = 357): any => ({listing, coverage: {}, paginator: {}, total_rows: total});
const table = (rows: any) => <ReportingTable table="user" data={rows} currency="USD" loading={false} disabled={false} sortBy="spend_total" descending page={1} pageSize={25}
    columns={DEFAULT_COLUMNS} onSort={() => {}} onPage={() => {}} onPreferences={() => {}}/>;

describe('breakdown table at real scale', () => {
    it('says how many rows there are in all', () => {
        render(table(data([row('user-a', '10')])));
        expect(screen.getByText('(357)')).toBeInTheDocument();
    });

    it('keeps its columns on a later page whose rows have no figures', () => {
        const view = render(table(data([row('user-a', '10')])));
        expect(screen.getByText('Total spend')).toBeInTheDocument();
        view.rerender(table(data([row('user-z', null)])));
        expect(screen.getByText('Total spend')).toBeInTheDocument();
    });
});
