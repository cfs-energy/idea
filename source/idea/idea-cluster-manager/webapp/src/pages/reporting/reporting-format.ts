import {ReportingCoverage, ReportingNumber} from '../../client/reporting-model';

export const numeric = (value: ReportingNumber | null | undefined): number | null => value == null || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
export const money = (value: ReportingNumber | null | undefined, currency = 'USD') => numeric(value) == null ? '—' : new Intl.NumberFormat('en-US', {style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2}).format(Number(value));
export const hours = (value: ReportingNumber | null | undefined) => numeric(value) == null ? '—' : new Intl.NumberFormat('en-US', {minimumFractionDigits: 1, maximumFractionDigits: 1}).format(Number(value));
export const percent = (value: ReportingNumber | null | undefined) => numeric(value) == null ? '—' : `${Math.round(Number(value))}%`;
export const bytes = (value: ReportingNumber | null | undefined) => numeric(value) == null ? '—' : `${hours(Number(value) / (Number(value) >= 2 ** 40 ? 2 ** 40 : 2 ** 30))} ${Number(value) >= 2 ** 40 ? 'TiB' : 'GiB'}`;
export function date(value: string, timezone: string, withTime = false): string {
    // API calendar dates are already cluster-local and must not shift to the previous day.
    const calendar = /^\d{4}-\d{2}-\d{2}$/.test(value);
    return new Intl.DateTimeFormat('en-US', {timeZone: calendar ? 'UTC' : timezone, month: 'short', day: 'numeric', ...(withTime && !calendar ? {hour: 'numeric', minute: '2-digit'} as const : {year: 'numeric'} as const)}).format(new Date(calendar ? `${value}T12:00:00Z` : value));
}
export const updated = (value: string, timezone: string) => `Updated ${new Intl.DateTimeFormat('en-US', {timeZone: timezone, hour: 'numeric', minute: '2-digit'}).format(new Date(value))}`;

export function metricInfo(details: ReportingCoverage | undefined, timezone: string, definition = 'Adds the amounts for this period.'): string {
    if (!details) return definition;
    const sentences = [definition];
    if (details.missing_days > 0 || details.missing_records > 0) sentences.push(`Missing ${details.missing_days} days and ${details.missing_records} records.`);
    else if (details.total_count > 0) sentences.push(`Uses ${details.eligible_count} of ${details.total_count} records.`);
    if (details.available_start && details.available_end) sentences.push(`Data from ${date(details.available_start, timezone)} to ${date(details.available_end, timezone)}.`);
    else if (details.source_as_of) sentences.push(`${updated(details.source_as_of, timezone)}.`);
    return sentences.slice(0, 3).join(' ');
}

const palette = ['#0972d3', '#a45dbb', '#2ea597', '#e07941', '#d63f38', '#6b6bb5', '#688c23', '#c33d85'];
export function colorByName(name: string): string {
    if (name === 'Other') return '#7d8998';
    let hash = 0;
    for (const char of name) hash = (Math.imul(hash, 31) + char.charCodeAt(0)) | 0;
    return palette[(hash >>> 0) % palette.length];
}
export interface NamedPoint {name: string; x: string; value: number}
// Keep eight named series, then combine remaining values at each x coordinate.
export function cappedSeries(points: NamedPoint[]) {
    const totals = new Map<string, number>();
    const xs = Array.from(new Set(points.map(point => point.x))).sort();
    for (const point of points) totals.set(point.name, (totals.get(point.name) ?? 0) + point.value);
    const names = Array.from(totals.keys()).filter(name => name !== 'Other').sort((a, b) => totals.get(b)! - totals.get(a)! || a.localeCompare(b));
    const kept = names.slice(0, 8);
    const grouped = new Map<string, Map<string, number>>();
    for (const point of points) {
        const name = kept.includes(point.name) ? point.name : 'Other';
        if (!grouped.has(name)) grouped.set(name, new Map());
        const values = grouped.get(name)!;
        values.set(point.x, (values.get(point.x) ?? 0) + point.value);
    }
    return [...kept, ...(grouped.has('Other') ? ['Other'] : [])].map(title => ({title, color: colorByName(title), data: xs.map(x => ({x, y: grouped.get(title)?.get(x) ?? 0}))}));
}
// Area charts require aligned dates; missing tier measurements are not zero bytes.
export function measuredTierPoints(points: NamedPoint[]): NamedPoint[] {
    const names = new Set(points.map(point => point.name));
    const measured = new Map<string, Set<string>>();
    for (const point of points) {
        if (!measured.has(point.x)) measured.set(point.x, new Set());
        measured.get(point.x)!.add(point.name);
    }
    return points.filter(point => measured.get(point.x)!.size === names.size);
}
export const budgetPresentation = {
    ok: {type: 'success' as const, color: '#037f0c', label: 'Within budget'},
    watch: {type: 'warning' as const, color: '#8d6605', label: 'Near budget'},
    over: {type: 'error' as const, color: '#d91515', label: 'Over budget'}
};
export const efficiencyStatus = (value: number) => value >= 70 ? 'success' : value >= 40 ? 'warning' : 'error';
