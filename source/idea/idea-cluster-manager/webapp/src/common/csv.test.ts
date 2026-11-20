import {afterEach, describe, expect, it, vi} from 'vitest';
import {csvCell, downloadCsv} from './csv';

describe('a csv cell', () => {
    it.each([
        ['=1+1', `"'=1+1"`],
        ['+marker', `"'+marker"`],
        ['@marker', `"'@marker"`],
        ['-marker', `"'-marker"`],
        ['-1+1', `"'-1+1"`],
        ['--', `"'--"`],
        [' =marker', `"' =marker"`],
    ])('writes "%s" as text', (value, cell) => {
        expect(csvCell(value)).toBe(cell);
    });

    it.each(['-', '-5', '+5', '3.25', '-0.5', '12'])('leaves "%s" as it is', value => {
        expect(csvCell(value)).toBe(`"${value}"`);
    });

    it('leaves the "-" placeholder the desktop sessions list shows for an empty cell', () => {
        // virtual-desktop-sessions.tsx: failure reason and updated time show "-" when there is none
        expect([csvCell('-'), csvCell('-')].join(',')).toBe('"-","-"');
    });
});

describe('a csv download', () => {
    afterEach(() => {vi.useRealTimers(); vi.restoreAllMocks();});

    it('releases the file only after the download has started', () => {
        vi.useFakeTimers();
        const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
        vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:marker');
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
        downloadCsv('marker.csv', 'a,b');
        expect(click).toHaveBeenCalledTimes(1);
        expect(revoke).not.toHaveBeenCalled();
        vi.runAllTimers();
        expect(revoke).toHaveBeenCalledWith('blob:marker');
    });
});
