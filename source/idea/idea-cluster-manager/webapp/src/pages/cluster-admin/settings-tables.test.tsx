import {describe, expect, it} from 'vitest';
import {desktopSchedulePatch} from './settings-tables';

const saved: any = {
    working_hours: {start_up_time: '09:00', shut_down_time: '17:00'},
    schedule: {monday: {type: 'WORKING_HOURS'}, tuesday: {type: 'CUSTOM_SCHEDULE', start_up_time: '08:00', shut_down_time: '12:00'}},
};

describe('desktop schedule save', () => {
    it('writes only the days and hours that changed', () => {
        const draft = structuredClone(saved);
        draft.schedule.monday = {type: 'STOP_ON_IDLE'};
        expect(desktopSchedulePatch(saved, draft)).toEqual({patch: {dcv_session: {schedule: {monday: {type: 'STOP_ON_IDLE', start_up_time: null, shut_down_time: null}}}}});
    });

    it('leaves a day the administrator never set unset', () => {
        const draft = structuredClone(saved);
        const {patch} = desktopSchedulePatch(saved, draft);
        expect(patch).toBeUndefined();
        draft.schedule = {...draft.schedule, wednesday: {type: 'START_ALL_DAY'}};
        expect(Object.keys(desktopSchedulePatch(saved, draft).patch.dcv_session.schedule)).toEqual(['wednesday']);
    });

    it('writes working hours alone when only they changed', () => {
        const draft = structuredClone(saved);
        draft.working_hours = {start_up_time: '08:30', shut_down_time: '17:00'};
        expect(desktopSchedulePatch(saved, draft)).toEqual({patch: {dcv_session: {working_hours: {start_up_time: '08:30', shut_down_time: '17:00'}}}});
    });

    it('refuses a custom day without both times', () => {
        const draft = structuredClone(saved);
        draft.schedule.friday = {type: 'CUSTOM_SCHEDULE', start_up_time: '08:00'};
        expect(desktopSchedulePatch(saved, draft).error).toMatch(/friday: custom hours/);
    });
});
