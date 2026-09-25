import React, {useEffect, useRef, useState} from 'react';
import {Alert, Button, ColumnLayout, Container, FormField, Header, Input, KeyValuePairs, SpaceBetween} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {FetchPricingRatesResult} from '../../client/data-model';
import {hasAccess} from '../../navigation/task-navigation';
import CatalogSettingsSection, {settingAnchor, settingValue} from './catalog-settings-section';

const labels: Record<string, string> = {
    ebs_gp3_storage: 'EBS gp3 storage (USD/GB-month)',
    ebs_io1_storage: 'EBS io1 storage (USD/GB-month)',
    provisioned_iops: 'io1 provisioned IOPS (USD/IOPS-month)',
    fsx_lustre: 'FSx Lustre (USD/GB-hour)',
    default_fsx_lustre_size: 'Default Lustre capacity (GB)',
    ec2_boot_penalty_seconds: 'EC2 boot penalty (seconds)',
};

export default function CostEstimationSettings(props: React.ComponentProps<typeof CatalogSettingsSection>) {
    const {settings, values, moduleId, editing, editDisabled, onEdit, onEditingEnd, onSaved, highlightedKey} = props;
    const clusterRegion = values.cluster?.aws?.region ?? AppContext.get().auth().getAwsRegion();
    const [region, setRegion] = useState(clusterRegion ?? '');
    const [draft, setDraft] = useState<Record<string, number | string>>({});
    const [fetching, setFetching] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [provenance, setProvenance] = useState<FetchPricingRatesResult | null>(null);
    const [stale, setStale] = useState(false);
    const revision = useRef(0);
    const canEdit = hasAccess(AppContext.get(), 'jobs-admin');
    const fields = Object.keys(labels).flatMap(name => settings.filter(setting => setting.path === `cost_estimation.${name}` && !setting.hidden && !setting.read_only));
    useEffect(() => {
        if (!editing) {revision.current++; setDraft({}); setFetching(false);}
        return () => {revision.current++;};
    }, [editing]);
    useEffect(() => {if (!provenance) setRegion(clusterRegion ?? '');}, [clusterRegion]); // eslint-disable-line react-hooks/exhaustive-deps
    const invalidate = () => {revision.current++; setFetching(false); if (provenance) setStale(true);};
    const fetchRates = async () => {
        if (!editing || !canEdit || fetching || !/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(region)) return;
        const request = ++revision.current;
        setFetching(true); setError(''); setNotice('');
        try {
            const result = await AppContext.get().client().clusterSettings().fetchPricingRates({region});
            if (request !== revision.current) return;
            if (result.region !== region) throw new Error('The returned pricing region does not match the requested region.');
            const rates = result.rates ?? {};
            const unavailable = result.unavailable ?? {};
            setDraft(current => {
                const next = {...current};
                for (const setting of fields) {
                    const name = setting.path.split('.')[1] as keyof NonNullable<FetchPricingRatesResult['rates']>;
                    const rate = rates[name];
                    if (typeof rate === 'number' && Number.isFinite(rate) && rate >= 0 && !unavailable[name]) next[setting.path] = rate;
                }
                return next;
            });
            setProvenance(result); setStale(false);
            setError(Object.entries(unavailable).map(([name, reason]) => `${labels[name] ?? name}: ${reason}`).join(' '));
        } catch (reason: any) {if (request === revision.current) setError(reason.message ?? 'Rates could not be fetched. Existing drafts are unchanged.');}
        finally {if (request === revision.current) setFetching(false);}
    };
    const save = async () => {
        if (fetching || saving || !canEdit) return;
        const patch: Record<string, number> = {};
        for (const [path, value] of Object.entries(draft)) {
            const setting = fields.find(item => item.path === path)!;
            const number = Number(value);
            if (value === '' || !Number.isFinite(number) || number < 0 || (setting.value_type === 'integer' && !Number.isInteger(number))) {
                setError(`Enter a valid nonnegative ${setting.value_type === 'integer' ? 'integer' : 'number'} for ${labels[path.split('.')[1]]}.`); return;
            }
            patch[path.split('.')[1]] = number;
        }
        setSaving(true); setError('');
        try {
            const result = await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId('scheduler'), settings: {cost_estimation: patch}});
            if (result.success === false) throw new Error('Settings were not saved.');
            onSaved('scheduler', Object.fromEntries(Object.entries(patch).map(([name, value]) => [`cost_estimation.${name}`, value])));
            setNotice('Cost estimation saved.'); onEditingEnd();
        } catch (reason: any) {setError(`${reason.message ?? 'Settings could not be saved.'} Drafts are retained.`);}
        finally {setSaving(false);}
    };
    const field = (setting: typeof fields[number]) => {
        const label = setting.label || labels[setting.path.split('.')[1]];
        const value = editing && setting.path in draft ? draft[setting.path] : settingValue(values.scheduler, setting.path);
        return <div key={setting.key} id={settingAnchor(setting.key)} className={`setting-editor${highlightedKey === setting.key ? ' setting-highlight' : ''}`}>
            {editing ? <FormField label={label}><Input ariaLabel={label} type="number" disabled={saving} value={value == null ? '' : String(value)} onChange={event => {
                invalidate(); setDraft(current => ({...current, [setting.path]: event.detail.value}));
            }}/></FormField> : <KeyValuePairs columns={1} items={[{label, value: value == null ? 'Not set' : String(value)}]}/>}
        </div>;
    };
    const middle = Math.ceil(fields.length / 2);
    return <Container header={<Header variant="h2" actions={editing ? <SpaceBetween direction="horizontal" size="xs">
        <Button disabled={saving} onClick={() => {invalidate(); setDraft({}); setError(''); setProvenance(null); setRegion(clusterRegion ?? ''); onEditingEnd();}}>Cancel</Button>
        <Button variant="primary" loading={saving} disabled={saving || fetching || !Object.keys(draft).length} onClick={save}>Save</Button>
    </SpaceBetween> : canEdit && fields.length ? <Button disabled={editDisabled} onClick={onEdit}>Edit</Button> : undefined}>Cost estimation</Header>}><SpaceBetween size="m">
        <p>Job-estimation defaults, excluding discounts. These rates do not set shared-storage billing. Lustre assumes SCRATCH_2 / SSD, converting USD/GB-month to USD/GB-hour by dividing by 730. Override the rate for persistent or mixed tiers.</p>
        {error && <div role="alert"><Alert type="error">{error}</Alert></div>}{notice && <Alert type="success">{notice}</Alert>}
        {editing && canEdit && <FormField label="Fetch region" description={`Cluster runs in ${clusterRegion || 'an unknown region'}. Fetch updates drafts; Save applies the rates.`}>
            <SpaceBetween size="s"><Input ariaLabel="Fetch region" disabled={saving} value={region} onChange={event => {invalidate(); setRegion(event.detail.value);}}/>
                <Button disabled={saving || fetching || !fields.length || !/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(region)} loading={fetching} onClick={fetchRates}>Fetch current rates</Button>
            </SpaceBetween>
        </FormField>}
        <p>Rates shown for {provenance?.region ?? region}{stale ? ' — stale: region or rates have changed; fetch again to refresh provenance.' : ''}</p>
        {provenance && <SpaceBetween size="xs"><p>Fetched {provenance.as_of} (retrieval time)</p>{(provenance.assumptions ?? []).map(value => <p key={value}>{value}</p>)}</SpaceBetween>}
        <ColumnLayout columns={2} minColumnWidth={280} variant={editing ? 'default' : 'text-grid'}><SpaceBetween size="m">{fields.slice(0, middle).map(field)}</SpaceBetween><SpaceBetween size="m">{fields.slice(middle).map(field)}</SpaceBetween></ColumnLayout>
    </SpaceBetween></Container>;
}
