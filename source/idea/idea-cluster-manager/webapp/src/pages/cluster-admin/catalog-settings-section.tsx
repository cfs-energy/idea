import React, {useEffect, useState} from 'react';
import {Alert, Badge, Button, ColumnLayout, Container, FormField, Header, Input, KeyValuePairs, Link, Select, SpaceBetween, Toggle, ExpandableSection, Modal, Table} from '@cloudscape-design/components';
import {SettingDefinition} from '../../client/data-model';
import {AppContext} from '../../common';

export const EFFECT_LABELS: Record<string, string> = {
    restart: 'Applies after restart',
    deployment: 'Applies on next upgrade',
};
export const settingAnchor = (key: string) => `setting-${key}`;
export const settingValue = (values: any, path: string): any => path.split('.').reduce((value, part) => value?.[part], values);

function Editor({setting, value, disabled, onChange}: {setting: SettingDefinition; value: any; disabled: boolean; onChange: (value: any) => void}) {
    const label = setting.label;
    const [model, setModel] = useState('');
    const [modelError, setModelError] = useState('');
    const [remove, setRemove] = useState<string | null>(null);
    if (setting.key === 'cluster-manager.bedrock.model_ids') {
        const models: string[] = Array.isArray(value) ? value : [];
        return <SpaceBetween size="m">
            <Alert type="info" header="Review model terms and pricing">Review <Link external href="https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html">model access and terms</Link> and <Link external href="https://aws.amazon.com/bedrock/pricing/">Amazon Bedrock pricing</Link> before adding a model.</Alert>
            <FormField label="Model ID" errorText={modelError} secondaryControl={<Button disabled={disabled} onClick={() => {
                const next = model.trim();
                if (!next || /\s/.test(next) || models.includes(next)) {setModelError(!next ? 'Enter a model id.' : 'Use a unique model ID without spaces.'); return;}
                onChange([...models, next]); setModel(''); setModelError('');
            }}>Add model</Button>}><Input disabled={disabled} value={model} placeholder="vendor.model-name" onChange={event => setModel(event.detail.value)}/></FormField>
            <Table variant="embedded" ariaLabels={{tableLabel: label}} items={models} wrapLines={true} trackBy={item => item} columnDefinitions={[
                {id: 'model', header: 'Model ID', cell: item => item},
                {id: 'remove', header: '', cell: item => <Button disabled={disabled} ariaLabel={`Remove ${item}`} onClick={() => setRemove(item)}>Remove</Button>},
            ]}/>
            {remove !== null && <Modal visible={true} onDismiss={() => setRemove(null)} header="Remove model from catalog" footer={<SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setRemove(null)}>Cancel</Button><Button variant="primary" onClick={() => {onChange(models.filter(item => item !== remove)); setRemove(null);}}>Remove model</Button>
            </SpaceBetween>}>Every project that lists {remove} loses access when this card is saved. Those projects retain the model reference, marked as unavailable.</Modal>}
        </SpaceBetween>;
    }
    if (setting.value_type === 'boolean') return <Toggle ariaLabel={label} disabled={disabled} checked={value === true} onChange={event => onChange(event.detail.checked)}/>;
    if (setting.value_type === 'enum') return <Select disabled={disabled} selectedOption={value == null ? null : {value, label: value || 'None'}}
        options={setting.choices?.map(choice => ({value: choice, label: choice || 'None'}))} onChange={event => onChange(event.detail.selectedOption.value)}/>;
    if (setting.value_type === 'list') {
        const items: string[] = Array.isArray(value) ? value : [];
        return <SpaceBetween size="xs">{items.map((item, index) => <SpaceBetween key={index} direction="horizontal" size="xs">
            <Input disabled={disabled} ariaLabel={`${label} ${index + 1}`} value={item} onChange={event => onChange(items.map((current, offset) => offset === index ? event.detail.value : current))}/>
            <Button disabled={disabled} ariaLabel={`Remove ${label} ${index + 1}`} onClick={() => onChange(items.filter((_, offset) => offset !== index))}>Remove</Button>
        </SpaceBetween>)}<Button disabled={disabled} onClick={() => onChange([...items, ''])}>Add {label.toLowerCase()}</Button></SpaceBetween>;
    }
    return <SpaceBetween size="xs">
        <Input disabled={disabled} ariaLabel={label} type={['integer', 'number'].includes(setting.value_type) ? 'number' : 'text'} value={value == null ? '' : String(value)}
            onChange={event => onChange(event.detail.value === '' && ['integer', 'number'].includes(setting.value_type) ? null : event.detail.value)}/>
        {setting.value_type === 'secret' && typeof value === 'string' && value.startsWith('arn:') && <Link external
            href={`https://${value.startsWith('arn:aws-cn:') ? 'console.amazonaws.cn' : value.startsWith('arn:aws-us-gov:') ? 'console.amazonaws-us-gov.com' : 'console.aws.amazon.com'}/secretsmanager/secret?name=${encodeURIComponent(value)}&region=${encodeURIComponent(value.split(':')[3])}`}>Open secret</Link>}
    </SpaceBetween>;
}

const displayValue = (setting: SettingDefinition, value: any): React.ReactNode => {
    if (setting.key === 'cluster-manager.bedrock.model_ids') return <Table variant="embedded" ariaLabels={{tableLabel: setting.label}} wrapLines={true} items={Array.isArray(value) ? value : []} columnDefinitions={[{id: 'model', header: 'Model ID', cell: item => item}]}/>;
    if (setting.value_type === 'boolean') return value === true ? 'On' : 'Off';
    if (Array.isArray(value)) return value.length ? value.join(', ') : 'None';
    if (value == null || value === '') return 'Not set';
    if (setting.value_type === 'secret') return <SpaceBetween direction="horizontal" size="xs"><span>Configured</span>{typeof value === 'string' && value.startsWith('arn:') && <Link external
        href={`https://${value.startsWith('arn:aws-cn:') ? 'console.amazonaws.cn' : value.startsWith('arn:aws-us-gov:') ? 'console.amazonaws-us-gov.com' : 'console.aws.amazon.com'}/secretsmanager/secret?name=${encodeURIComponent(value)}&region=${encodeURIComponent(value.split(':')[3])}`}>Open secret</Link>}</SpaceBetween>;
    return String(value);
};

export const isReadOnly = (setting: SettingDefinition) => 'read_only' in setting && setting.read_only === true;
export const isHidden = (setting: SettingDefinition) => 'hidden' in setting && setting.hidden === true;

export default function CatalogSettingsSection({settings, values, moduleId, title, children, highlightedKey, editing, editDisabled, onEdit, onEditingEnd, onSaved}: {
    settings: SettingDefinition[]; values: Record<string, any>; moduleId: (module: string) => string; title: string; children?: React.ReactNode;
    editing: boolean; editDisabled: boolean; highlightedKey?: string | null;
    onEdit: () => void; onEditingEnd: () => void; onSaved: (module: string, patch: Record<string, any>) => void;
}) {
    const [changes, setChanges] = useState<Record<string, any>>({});
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [advanced, setAdvanced] = useState(false);
    const order = (setting: SettingDefinition) => title === 'Portal' ? ['Title', 'Subtitle', 'Logo URL', 'Administrator email', 'Default landing page', 'Session management'].indexOf(setting.label) : title === 'Amazon Bedrock' ? setting.key === 'cluster-manager.bedrock.enabled' ? 0 : setting.key === 'cluster-manager.bedrock.model_ids' ? 1 : 2 : 0;
    const visible = settings.filter(setting => !isHidden(setting)).map(setting => title === 'Amazon Bedrock' && /invocation|usage|retention|claude_code/.test(setting.path) ? {...setting, advanced: true} : setting).sort((a, b) => order(a) - order(b));
    const bedrock = values['cluster-manager']?.bedrock;
    const desktop = values['virtual-desktop-controller']?.bedrock;
    const editable = visible.filter(setting => !isReadOnly(setting));
    const deferred = editable.filter(setting => setting.effect !== 'runtime');
    const effect = deferred.some(setting => setting.effect === 'deployment') ? 'deployment' : deferred.length ? 'restart' : null;
    useEffect(() => {if (!editing) setChanges({});}, [editing]);
    useEffect(() => {if (settings.some(setting => setting.key === highlightedKey && (setting.advanced || (title === 'Amazon Bedrock' && /invocation|usage|retention|claude_code/.test(setting.path))))) setAdvanced(true);}, [settings, highlightedKey, title]);
    const save = async () => {
        if (saving) return;
        setSaving(true); setError(''); setNotice('');
        const saved: string[] = []; const effects = new Set<string>();
        for (const module of Array.from(new Set(editable.filter(setting => setting.key in changes).map(setting => setting.module)))) {
            const patch: Record<string, any> = {}; const flat: Record<string, any> = {};
            for (const setting of editable.filter(setting => setting.module === module && setting.key in changes)) {
                const parts = setting.path.split('.'); const leaf = parts.pop()!;
                parts.reduce((node, part) => node[part] ??= {}, patch)[leaf] = changes[setting.key]; flat[setting.path] = changes[setting.key];
            }
            try {
                const result = await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId(module), settings: patch});
                if (result.success === false) throw new Error('Settings were not saved.');
                Object.values(result.effects ?? {}).filter(value => value !== 'runtime').forEach(value => effects.add(value));
                saved.push(module); onSaved(module, flat);
                setChanges(current => Object.fromEntries(Object.entries(current).filter(([key]) => !settings.some(setting => setting.module === module && setting.key === key))));
            } catch (reason: any) {
                setError(`${saved.length ? `Saved: ${saved.join(', ')}. ` : 'No modules saved. '}${module} failed: ${reason.message ?? 'Settings could not be saved.'} Remaining drafts are retained.`);
                setNotice(Array.from(effects).map(value => EFFECT_LABELS[value] ?? value).join('. ')); setSaving(false); return;
            }
        }
        setNotice(`Saved: ${saved.join(', ')}.${effects.size ? ` ${Array.from(effects).map(value => EFFECT_LABELS[value] ?? value).join('. ')}.` : ''}`);
        setSaving(false); setChanges({}); onEditingEnd();
    };
    const actions = editing ? <SpaceBetween direction="horizontal" size="xs">
        <Button disabled={saving} onClick={() => {setChanges({}); setError(''); onEditingEnd();}}>Cancel</Button>
        <Button variant="primary" loading={saving} disabled={saving || !Object.keys(changes).length} onClick={save}>Save</Button>
    </SpaceBetween> : editable.length ? <Button disabled={editDisabled} onClick={onEdit}>Edit</Button> : undefined;
    const field = (setting: SettingDefinition) => <div key={setting.key} id={settingAnchor(setting.key)} className={`setting-editor${highlightedKey === setting.key ? ' setting-highlight' : ''}`}>
        {editing && !isReadOnly(setting) ? <FormField label={setting.label} description={setting.description} constraintText={setting.validation.required ? 'Required' : undefined}>
            <Editor setting={setting} disabled={saving} value={setting.key in changes ? changes[setting.key] : settingValue(values[setting.module], setting.path)}
                onChange={value => {setChanges(current => ({...current, [setting.key]: value})); setNotice('');}}/>
        </FormField> : <KeyValuePairs columns={1} items={[{label: setting.label, value: displayValue(setting, settingValue(values[setting.module], setting.path))}]}/>}
    </div>;
    const stack = (items: SettingDefinition[]) => {
        const middle = Math.ceil(items.length / 2);
        return items.length < 2 ? items.map(field) : <ColumnLayout columns={2} minColumnWidth={280} variant={editing ? 'default' : 'text-grid'}>
            <SpaceBetween size="m">{items.slice(0, middle).map(field)}</SpaceBetween>
            <SpaceBetween size="m">{items.slice(middle).map(field)}</SpaceBetween>
        </ColumnLayout>;
    };
    const fields = (advanced: boolean) => {
        const items = visible.filter(setting => Boolean(setting.advanced) === advanced);
        if (title === 'Collection' && !advanced) return <SpaceBetween size="m">
            {items.filter(setting => setting.path === 'metrics.cost.enabled').map(field)}
            {stack(['lookback_days', 'interval_hours'].flatMap(name => items.filter(setting => setting.path === `metrics.cost.${name}`)))}
            {stack(['module_tag', 'project_tag', 'owner_tag'].flatMap(name => items.filter(setting => setting.path === `metrics.cost.${name}`)))}
        </SpaceBetween>;
        const groups = new Map<string, SettingDefinition[]>();
        for (const setting of items) {
            let name = '';
            if (setting.module === 'shared-storage') name = setting.path.split('.')[0];
            if (setting.path.includes('.rules.')) name = `${setting.module === 'cluster' ? 'Cluster' : 'Desktop'} backup rule: ${setting.path.split('.rules.')[1].split('.')[0]}`;
            else if (title === 'Directory mapping') name = setting.path.split('.')[0].replaceAll('_', ' ');
            else if (title === 'Network and connectivity') name = `${setting.module}: ${setting.path.split('.')[0].replaceAll('_', ' ')}`;
            else if (setting.path.startsWith('iam.') || (setting.value_type === 'list' && /iam|policy_arns/.test(setting.path))) name = 'IAM policies';
            groups.set(name, [...(groups.get(name) ?? []), setting]);
        }
        return Array.from(groups, ([name, entries]) => {
            const blocks: React.ReactNode[] = []; let short: SettingDefinition[] = [];
            const flush = () => {if (short.length) {blocks.push(<React.Fragment key={short[0].key}>{stack(short)}</React.Fragment>); short = [];}};
            for (const setting of entries) {
                if (setting.value_type === 'list' || setting.key === 'cluster-manager.bedrock.model_ids') {flush(); blocks.push(field(setting));}
                else short.push(setting);
            }
            flush();
            return <div className="settings-subgroup" key={name}><SpaceBetween size="m">{name && <Header variant="h3">{name}</Header>}{blocks}</SpaceBetween></div>;
        });
    };
    return <Container header={<Header variant="h2" actions={actions} description={editing && deferred.length ? `Deferred settings: ${deferred.map(setting => setting.label).join(', ')}.` : undefined}>
        {title} {effect && <Badge color="grey">{EFFECT_LABELS[effect]}</Badge>}
    </Header>}><SpaceBetween size="m">
        {error && <Alert type="error">{error}</Alert>}{notice && <Alert type="success">{notice}</Alert>}
        {children}
        {title === 'Amazon Bedrock' && <SpaceBetween size="m">
            {bedrock?.enabled && !bedrock?.invocation_log_role_arn && <Alert type="warning" header="Bedrock is enabled, but the cluster-manager module has not been redeployed">Redeploy the module to provision project permissions and invocation logging, then update affected projects to retry reconciliation.</Alert>}
            {bedrock?.enabled && AppContext.get().getClusterSettingsService().isVirtualDesktopDeployed() && !desktop?.project_pass_role_arn && <Alert type="warning" header="Bedrock is enabled, but the virtual-desktop-controller module has not been redeployed">Redeploy the module before launching desktops that use project roles.</Alert>}
            {bedrock?.enabled && bedrock?.invocation_logging?.manage_configuration === false && <Alert type="info" header="IDEA is not managing Bedrock model invocation logging">Configure delivery to <span>{bedrock.invocation_log_group_name}</span> to collect usage. Budget enforcement follows collected usage.</Alert>}
            <p>Models approved here can be granted to individual projects. Budget enforcement follows collected usage.</p>
        </SpaceBetween>}
        {fields(false)}
        {visible.some(setting => setting.advanced) && <ExpandableSection headerText="Advanced" expanded={advanced} onChange={event => setAdvanced(event.detail.expanded)}><SpaceBetween size="m">{title === 'Amazon Bedrock' && <Alert type="info">Payload capture stores model prompts and responses in invocation logs. Enable it only when this data may be retained. Account and Region logging changes require an explicit choice.</Alert>}{fields(true)}</SpaceBetween></ExpandableSection>}
    </SpaceBetween></Container>;
}
