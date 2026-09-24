import React, {useEffect, useState} from 'react';
import {Alert, Badge, Button, Container, FormField, Header, Input, KeyValuePairs, Link, Select, SpaceBetween, Toggle, Box} from '@cloudscape-design/components';
import {SettingDefinition} from '../../client/data-model';
import {AppContext} from '../../common';

export const EFFECT_LABELS: Record<string, string> = {
    restart: 'Restart required',
    deployment: 'Applies on next upgrade',
};
export const settingAnchor = (key: string) => `setting-${key}`;
export const settingValue = (values: any, path: string): any => path.split('.').reduce((value, part) => value?.[part], values);

function Editor({setting, value, disabled, onChange}: {setting: SettingDefinition; value: any; disabled: boolean; onChange: (value: any) => void}) {
    const label = setting.label;
    if (setting.value_type === 'boolean') return <Toggle disabled={disabled} checked={value === true} onChange={event => onChange(event.detail.checked)}/>;
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
    if (setting.value_type === 'boolean') return value === true ? 'On' : 'Off';
    if (Array.isArray(value)) return value.length ? value.join(', ') : 'None';
    if (value == null || value === '') return 'Not set';
    if (setting.value_type === 'secret') return <SpaceBetween direction="horizontal" size="xs"><span>Configured</span>{typeof value === 'string' && value.startsWith('arn:') && <Link external
        href={`https://${value.startsWith('arn:aws-cn:') ? 'console.amazonaws.cn' : value.startsWith('arn:aws-us-gov:') ? 'console.amazonaws-us-gov.com' : 'console.aws.amazon.com'}/secretsmanager/secret?name=${encodeURIComponent(value)}&region=${encodeURIComponent(value.split(':')[3])}`}>Open secret</Link>}</SpaceBetween>;
    return String(value);
};

export default function CatalogSettingsSection({settings, values, moduleId, highlightedKey, editing, editDisabled, onEdit, onEditingEnd, onSaved}: {
    settings: SettingDefinition[]; values: any; moduleId: string; editing: boolean; editDisabled: boolean;
    highlightedKey?: string | null; onEdit: () => void; onEditingEnd: () => void; onSaved: (patch: Record<string, any>) => void;
}) {
    const [changes, setChanges] = useState<Record<string, any>>({});
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    useEffect(() => {
        if (!editing) setChanges({});
    }, [editing]);
    const save = async () => {
        const patch: Record<string, any> = {};
        for (const [path, value] of Object.entries(changes)) {
            const parts = path.split('.');
            const leaf = parts.pop()!;
            const parent = parts.reduce((node, part) => node[part] ??= {}, patch);
            parent[leaf] = value;
        }
        setSaving(true); setError(''); setNotice('');
        try {
            const result = await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId, settings: patch});
            if (result.success === false) throw new Error('Settings were not saved.');
            const effects = Object.values(result.effects ?? {}).filter(effect => effect !== 'runtime');
            const next = Array.from(new Set(effects)).map(effect => EFFECT_LABELS[effect] ?? effect).join('. ');
            setNotice(next ? `Saved. ${next}.` : 'Saved.');
            onSaved(changes); setChanges({}); onEditingEnd();
        } catch (reason: any) {
            setError(reason.message ?? 'Settings could not be saved.');
        } finally { setSaving(false); }
    };
    const actions = editing ? <SpaceBetween direction="horizontal" size="xs">
        <Button disabled={saving} onClick={() => {setChanges({}); setError(''); onEditingEnd();}}>Cancel</Button>
        <Button variant="primary" loading={saving} disabled={!Object.keys(changes).length} onClick={save}>Save</Button>
    </SpaceBetween> : <Button disabled={editDisabled} onClick={onEdit}>Edit</Button>;
    return <Container header={<Header variant="h2" actions={actions}>
        {settings[0].section} <Box variant="small">({settings[0].module})</Box>
    </Header>}><SpaceBetween size="m">
        {error && <Alert type="error">{error}</Alert>}{notice && <Alert type="success">{notice}</Alert>}
        {!editing && <KeyValuePairs columns={2} items={settings.map(setting => ({
            label: setting.label,
            value: <div id={settingAnchor(setting.key)} className={`setting-editor${highlightedKey === setting.key ? ' setting-highlight' : ''}`}><SpaceBetween size="xxs">
                {displayValue(setting, settingValue(values, setting.path))}
                {setting.effect !== 'runtime' && <Badge color="blue">{EFFECT_LABELS[setting.effect]}</Badge>}
            </SpaceBetween></div>
        }))}/>}
        {editing && settings.map(setting => <div key={setting.key} id={settingAnchor(setting.key)} className={`setting-editor${highlightedKey === setting.key ? ' setting-highlight' : ''}`}>
            <FormField label={setting.label} description={setting.description} constraintText={setting.validation.required ? 'Required' : undefined}>
                <SpaceBetween size="xs">{setting.effect !== 'runtime' && <Badge color="blue">{EFFECT_LABELS[setting.effect]}</Badge>}
                    <Editor setting={setting} disabled={saving} value={setting.path in changes ? changes[setting.path] : settingValue(values, setting.path)}
                        onChange={value => {setChanges(current => ({...current, [setting.path]: value})); setNotice('');}}/>
                </SpaceBetween>
            </FormField>
        </div>)}
    </SpaceBetween></Container>;
}
