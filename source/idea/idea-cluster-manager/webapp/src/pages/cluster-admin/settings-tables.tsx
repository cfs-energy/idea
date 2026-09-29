import React, {useEffect, useRef, useState} from 'react';
import {Alert, Button, Container, FormField, Header, Link, Select, SpaceBetween, Table, TimeInput, Toggle} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {SettingDefinition} from '../../client/data-model';
import {settingAnchor, settingValue} from './catalog-settings-section';

export const SCHEDULE_DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
export const SCHEDULE_LABELS: Record<string, string> = {NO_SCHEDULE: 'No schedule', WORKING_HOURS: 'Working hours', STOP_ON_IDLE: 'Stop on idle', START_ALL_DAY: 'Run all day', CUSTOM_SCHEDULE: 'Custom hours'};
const validRange = (start: string, stop: string) => /^([01]\d|2[0-3]):[0-5]\d$/.test(start) && /^([01]\d|2[0-3]):[0-5]\d$/.test(stop) && start < stop;

/**
 * The settings a save writes: only working hours and days that differ from the saved values. A day the
 * administrator never set stays unset, and a concurrent edit to another day is not overwritten.
 */
export function desktopSchedulePatch(saved: any, draft: any): {patch?: any; error?: string} {
    const hours = draft.working_hours ?? {};
    if (!validRange(hours.start_up_time, hours.shut_down_time)) return {error: 'Working hours require HH:mm times with start before stop on the same day.'};
    const session: any = {};
    const savedHours = saved.working_hours ?? {};
    if (hours.start_up_time !== savedHours.start_up_time || hours.shut_down_time !== savedHours.shut_down_time) session.working_hours = {start_up_time: hours.start_up_time, shut_down_time: hours.shut_down_time};
    const schedule: Record<string, any> = {};
    for (const day of SCHEDULE_DAYS) {
        const row = draft.schedule?.[day];
        if (!row) continue;
        if (!(row.type in SCHEDULE_LABELS)) return {error: `${day}: choose a schedule.`};
        if (row.type === 'CUSTOM_SCHEDULE' && !validRange(row.start_up_time, row.shut_down_time)) return {error: `${day}: custom hours require both HH:mm times with start before stop on the same day.`};
        const next = {type: row.type, start_up_time: row.type === 'CUSTOM_SCHEDULE' ? row.start_up_time : null, shut_down_time: row.type === 'CUSTOM_SCHEDULE' ? row.shut_down_time : null};
        const before = saved.schedule?.[day];
        const same = before && before.type === next.type && (before.start_up_time ?? null) === next.start_up_time && (before.shut_down_time ?? null) === next.shut_down_time;
        if (!same) schedule[day] = next;
    }
    if (Object.keys(schedule).length) session.schedule = schedule;
    return Object.keys(session).length ? {patch: {dcv_session: session}} : {};
}

export function DesktopScheduleTable({values, moduleId, timezone, editing, editDisabled, onEdit, onEditingEnd, onSaved}: {
    values: any; moduleId: string; timezone?: string; editing: boolean; editDisabled: boolean;
    onEdit: () => void; onEditingEnd: () => void; onSaved: (patch: any) => void;
}) {
    const [draft, setDraft] = useState<any>({});
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const errorRef = useRef<HTMLDivElement>(null);
    const session = editing ? draft : values.dcv_session ?? {};
    const hours = session.working_hours ?? {};
    useEffect(() => {if (editing) setDraft(structuredClone(values.dcv_session ?? {}));}, [editing]); // eslint-disable-line react-hooks/exhaustive-deps
    useEffect(() => {if (error) errorRef.current?.focus();}, [error]);
    const changeHours = (key: string, value: string) => setDraft((current: any) => ({...current, working_hours: {...current.working_hours, [key]: value}}));
    const changeDay = (day: string, patch: any) => setDraft((current: any) => ({...current, schedule: {...current.schedule, [day]: {...current.schedule?.[day], ...patch}}}));
    const save = async () => {
        if (busy) return;
        setError(''); setNotice('');
        const {patch, error: problem} = desktopSchedulePatch(values.dcv_session ?? {}, session);
        if (problem) {setError(problem); return;}
        if (!patch) {onEditingEnd(); setNotice('No changes to save.'); return;}
        setBusy(true);
        try {
            const result = await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId, settings: patch});
            if (result.success === false) throw new Error('Desktop schedule was not saved.');
            onSaved(patch); onEditingEnd(); setNotice('Desktop schedule saved.');
        } catch (reason: any) {setError(reason.message ?? 'Desktop schedule could not be saved.');}
        finally {setBusy(false);}
    };
    const time = (day: string, key: string) => {
        const row = session.schedule?.[day] ?? {type: 'STOP_ON_IDLE'};
        const label = `${day[0].toUpperCase() + day.slice(1)} ${key === 'start_up_time' ? 'start' : 'stop'}`;
        return <div id={settingAnchor(`virtual-desktop-controller.dcv_session.schedule.${day}.${key}`)}>{editing && row.type === 'CUSTOM_SCHEDULE'
            ? <FormField label={label}><TimeInput ariaLabel={label} disabled={busy} format="hh:mm" value={row[key] ?? ''} onChange={event => changeDay(day, {[key]: event.detail.value})}/></FormField>
            : row.type === 'WORKING_HOURS' ? hours[key] || '—' : row.type === 'CUSTOM_SCHEDULE' ? row[key] || '—' : '—'}</div>;
    };
    return <Container header={<Header variant="h2" actions={editing ? <SpaceBetween direction="horizontal" size="xs">
        <Button disabled={busy} onClick={() => {setDraft({}); setError(''); onEditingEnd();}}>Cancel</Button><Button variant="primary" loading={busy} disabled={busy} onClick={save}>Save</Button>
    </SpaceBetween> : <Button disabled={editDisabled} onClick={() => {setNotice(''); onEdit();}}>Edit</Button>}>Desktop schedule</Header>}><SpaceBetween size="m">
        {error && <div ref={errorRef} tabIndex={-1} role="alert"><Alert type="error">{error}</Alert></div>}{notice && <Alert type="success">{notice}</Alert>}
        <SpaceBetween direction="horizontal" size="m">{(['start_up_time', 'shut_down_time'] as const).map(key => <div key={key} id={settingAnchor(`virtual-desktop-controller.dcv_session.working_hours.${key}`)}><FormField label={`Working hours ${key === 'start_up_time' ? 'start' : 'stop'}`}>
            {editing ? <TimeInput disabled={busy} format="hh:mm" value={hours[key] ?? ''} onChange={event => changeHours(key, event.detail.value)}/> : hours[key] || '—'}
        </FormField></div>)}</SpaceBetween>
        <div>Cluster timezone: {timezone ?? 'Not available'}. <Link href="#/cluster/settings/appearance?key=cluster.timezone">Regional defaults</Link></div>
        <Table variant="embedded" ariaLabels={{tableLabel: 'Desktop schedule'}} items={SCHEDULE_DAYS} trackBy={day => day} columnDefinitions={[
            {id: 'day', header: 'Day', cell: day => day[0].toUpperCase() + day.slice(1)},
            {id: 'schedule', header: 'Schedule', cell: day => <div id={settingAnchor(`virtual-desktop-controller.dcv_session.schedule.${day}.type`)}>{editing ? <Select ariaLabel={`${day} schedule`} disabled={busy}
                selectedOption={{value: session.schedule?.[day]?.type ?? 'STOP_ON_IDLE', label: SCHEDULE_LABELS[session.schedule?.[day]?.type ?? 'STOP_ON_IDLE']}}
                options={Object.entries(SCHEDULE_LABELS).map(([value, label]) => ({value, label}))} onChange={event => changeDay(day, {type: event.detail.selectedOption.value, start_up_time: null, shut_down_time: null})}/>
                : SCHEDULE_LABELS[session.schedule?.[day]?.type ?? 'STOP_ON_IDLE']}</div>},
            {id: 'start', header: 'Start', cell: day => time(day, 'start_up_time')}, {id: 'stop', header: 'Stop', cell: day => time(day, 'shut_down_time')},
        ]}/>
    </SpaceBetween></Container>;
}

export function NotificationsTable({settings, values, moduleId, editing, editDisabled, onEdit, onEditingEnd, onSaved}: {
    settings: SettingDefinition[]; values: Record<string, any>; moduleId: (module: string) => string;
    editing: boolean; editDisabled: boolean; onEdit: () => void; onEditingEnd: () => void; onSaved: (module: string, patch: Record<string, any>) => void;
}) {
    const [draft, setDraft] = useState<Record<string, any>>({});
    const [templates, setTemplates] = useState<string[]>([]);
    const [loaded, setLoaded] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const errorRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        let cancelled = false;
        void (async () => {
            try {
                const names: string[] = []; let cursor: string | undefined;
                do {
                    const result = await AppContext.get().client().emailTemplates().listEmailTemplates({paginator: {page_size: 100, cursor}});
                    names.push(...(result.listing ?? []).map(item => item.name!).filter(Boolean)); cursor = result.paginator?.cursor;
                } while (cursor);
                if (!cancelled) {setTemplates(names); setLoaded(true);}
            } catch (reason: any) {if (!cancelled) setError(`Email templates could not be loaded: ${reason.message}. Reload to retry.`);}
        })();
        return () => {cancelled = true;};
    }, []);
    useEffect(() => {if (!editing) setDraft({});}, [editing]);
    useEffect(() => {if (error) errorRef.current?.focus();}, [error]);
    const value = (setting: SettingDefinition) => setting.key in draft ? draft[setting.key] : settingValue(values[setting.module], setting.path);
    const switches = settings.filter(setting => ['notifications.email.enabled', 'notifications.enabled'].includes(setting.path));
    const rows = settings.filter(setting => setting.path.endsWith('.email_template') && !setting.path.includes('stopped_session_cleanup'));
    const cleanup = settings.find(setting => setting.path === 'dcv_session.stopped_session_cleanup.email_template');
    const enabledSetting = (row: SettingDefinition) => settings.find(setting => setting.module === row.module && setting.path === row.path.replace(/email_template$/, 'enabled'));
    const master = (module: string) => switches.find(setting => setting.module === module);
    const enabled = (row: SettingDefinition) => {const setting = enabledSetting(row) ?? master('scheduler'); return setting ? value(setting) === true : false;};
    const change = (setting: SettingDefinition, next: any) => {setDraft(current => ({...current, [setting.key]: next})); setNotice('');};
    const template = (setting: SettingDefinition) => <div id={settingAnchor(setting.key)}><FormField label={setting.label} errorText={loaded && value(setting) && !templates.includes(value(setting)) ? `Unknown template: ${value(setting)}. Choose an existing template or create it in Email templates.` : undefined}>
        {editing ? <Select disabled={busy || !loaded} ariaLabel={setting.label} selectedOption={value(setting) ? {value: value(setting), label: value(setting)} : null}
            options={templates.map(name => ({value: name, label: name}))} onChange={event => change(setting, event.detail.selectedOption.value)}/> : value(setting) || 'Not set'}
    </FormField></div>;
    const save = async () => {
        if (busy) return;
        setError(''); setNotice('');
        for (const row of [...rows, ...(cleanup ? [cleanup] : [])]) {
            const required = row === cleanup ? settingValue(values[row.module], 'dcv_session.stopped_session_cleanup.enabled') === true : enabled(row);
            if ((required || value(row)) && (!loaded || !templates.includes(value(row)))) {setError(`${row.label}: choose an existing email template before saving.`); return;}
        }
        const modules = Array.from(new Set(settings.filter(setting => setting.key in draft).map(setting => setting.module)));
        const emailMaster = master('cluster-manager');
        if (emailMaster && draft[emailMaster.key] === true) modules.sort((a, b) => Number(a === 'cluster-manager') - Number(b === 'cluster-manager'));
        setBusy(true);
        const saved: string[] = [];
        for (const module of modules) {
            const patch: Record<string, any> = {}; const flat: Record<string, any> = {};
            settings.filter(setting => setting.module === module && setting.key in draft).forEach(setting => {
                const parts = setting.path.split('.'); const leaf = parts.pop()!;
                parts.reduce((node, part) => node[part] ??= {}, patch)[leaf] = draft[setting.key]; flat[setting.path] = draft[setting.key];
            });
            try {
                const result = await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId(module), settings: patch});
                if (result.success === false) throw new Error('Settings were not saved.');
                saved.push(module); onSaved(module, flat);
                setDraft(current => Object.fromEntries(Object.entries(current).filter(([key]) => !settings.some(setting => setting.module === module && setting.key === key))));
            } catch (reason: any) {
                setError(`${saved.length ? `Saved: ${saved.join(', ')}. ` : 'No modules saved. '}${module} failed: ${reason.message}. Remaining drafts are retained.`); setBusy(false); return;
            }
        }
        setBusy(false); setNotice(`Saved: ${saved.join(', ')}.`); onEditingEnd();
    };
    return <Container header={<Header variant="h2" actions={editing ? <SpaceBetween direction="horizontal" size="xs"><Button disabled={busy} onClick={() => {setDraft({}); setError(''); onEditingEnd();}}>Cancel</Button>
        <Button variant="primary" loading={busy} disabled={busy || !loaded || !Object.keys(draft).length} onClick={save}>Save</Button></SpaceBetween> : <Button disabled={editDisabled} onClick={onEdit}>Edit</Button>}>Notifications</Header>}><SpaceBetween size="m">
        {error && <div ref={errorRef} role="alert" tabIndex={-1}><Alert type="error">{error}</Alert></div>}{notice && <Alert type="success">{notice}</Alert>}
        {switches.map(setting => <div key={setting.key} id={settingAnchor(setting.key)}><FormField label={setting.label}>{editing ? <Toggle ariaLabel={setting.label} disabled={busy} checked={value(setting) === true} onChange={event => change(setting, event.detail.checked)}/> : value(setting) ? 'On' : 'Off'}</FormField>
            {!value(setting) && <p>{setting.label} is off. Event settings are retained; notifications following this switch will not be delivered.</p>}</div>)}
        <Table variant="embedded" ariaLabels={{tableLabel: 'Notifications'}} trackBy="key" items={rows} columnDefinitions={[
            {id: 'service', header: 'Service', cell: row => row.module === 'scheduler' ? 'Jobs' : 'Desktops'},
            {id: 'event', header: 'Event', cell: row => enabledSetting(row)?.label ?? row.label.replace(/ email template$/, '')},
            {id: 'enabled', header: 'Delivery', cell: row => {const setting = enabledSetting(row); return setting ? <div id={settingAnchor(setting.key)}>{editing ? <Toggle ariaLabel={setting.label} disabled={busy} checked={value(setting) === true} onChange={event => change(setting, event.detail.checked)}/> : value(setting) ? 'On' : 'Off'}</div> : 'Follows Job notifications';}},
            {id: 'template', header: 'Email template', cell: template},
        ]}/>
        {cleanup && template(cleanup)}
    </SpaceBetween></Container>;
}
