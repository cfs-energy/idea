import {budgetPresentation, bytes, cappedSeries, colorByName, rankedColors, palette, calendarDays, date, efficiencyStatus, hours, measuredTierPoints, money, percent, updated} from './reporting-format';

describe('human-readable report values', () => {
    it('formats decimal strings, zero and missing numbers', () => {
        expect(money('1524.22')).toBe('$1,524.22');
        expect(money('0')).toBe('$0.00');
        expect(money('-2.5', 'EUR')).toBe('-€2.50');
        expect(hours('12.345')).toBe('12.3');
        expect(percent(69.7)).toBe('70%');
        expect(bytes(2 ** 30 * 1.25)).toBe('1.3 GiB');
        expect(bytes(2 ** 40)).toBe('1.0 TiB');
        for (const format of [money, hours, percent, bytes]) expect(format(null)).toBe('—');
    });
    it('uses the cluster timezone across midnight and daylight saving time', () => {
        expect(date('2026-09-29T00:33:00Z', 'America/New_York')).toBe('Sep 28, 2026');
        expect(date('2026-09-29', 'America/New_York')).toBe('Sep 29, 2026');
        expect(date('2026-09-29T20:33:00Z', 'America/New_York', true)).toBe('Sep 29, 4:33 PM');
        expect(updated('2026-09-29T20:33:00Z', 'America/New_York')).toBe('Updated 4:33 PM');
        expect(updated('2026-01-29T20:33:00Z', 'America/New_York')).toBe('Updated 3:33 PM');
    });
});
it('caps at eight series including Other, sums duplicates and preserves totals', () => {
    const points = Array.from({length: 12}, (_, i) => ({name: `Project ${i}`, x: '2026-09-29', value: i + 1}));
    points.push({name: 'Project 0', x: '2026-09-28', value: 1});
    const result = cappedSeries(points);
    expect(result).toHaveLength(8);
    expect(result.at(-1)?.title).toBe('Other');
    expect(result.at(-1)?.data).toEqual([{x: '2026-09-28', y: 1}, {x: '2026-09-29', y: 15}]);
    expect(result.flatMap(s => s.data).reduce((total, p) => total + p.y, 0)).toBe(79);
    expect(cappedSeries([])).toEqual([]);
    expect(cappedSeries(points.slice(0, 8))).toHaveLength(8);
});
it('assigns distinct categorical colors by page rank and keeps them across charts', () => {
    const points = [{name: 'Project Cedar', x: 'a', value: 5}, {name: 'Project Elm', x: 'a', value: 2}];
    const ranks = rankedColors(points);
    const first = cappedSeries(points, ranks);
    const second = cappedSeries([{name: 'Project Elm', x: 'b', value: 8}, {name: 'Project Cedar', x: 'b', value: 1}], ranks);
    expect(first[0].color).toBe(palette[0]);
    expect(first[1].color).toBe(palette[1]);
    expect(first.find(s => s.title === 'Project Cedar')?.color).toBe(second.find(s => s.title === 'Project Cedar')?.color);
    expect(rankedColors([...points].reverse())).toEqual(ranks);
    expect(colorByName('Last month')).not.toBe(colorByName('This month'));
});
it('maps budget and efficiency statuses to success, warning and error', () => {
    expect(Object.values(budgetPresentation).map(value => value.type)).toEqual(['success', 'warning', 'error']);

    expect([39, 40, 69, 70].map(efficiencyStatus)).toEqual(['warning', 'warning', 'warning', 'success']);
    expect([0.02, 0, 1].map(percent)).toEqual(['<1%', '0%', '1%']);
});

it('does not turn missing tier measurements into zero bytes', () => {
    const points = [{name: 'SSD', x: '2026-09-28', value: 10}, {name: 'SSD', x: '2026-09-29', value: 12}, {name: 'Capacity pool', x: '2026-09-29', value: 30}];
    expect(measuredTierPoints(points)).toEqual(points.slice(1));
    expect(measuredTierPoints(points.slice(0, 2))).toEqual(points.slice(0, 2));
});

it('fills every calendar day, including leap days, with zero money', () => {
    const days = calendarDays('2024-02-28', '2024-03-01');
    const series = cappedSeries([{name: 'Project Cedar', x: '2024-02-29', value: 2}], undefined, days);
    expect(series[0].data.map(p => p.y)).toEqual([0, 2, 0]);
    expect(hours(254)).toBe('254');
});
