import React, {useEffect, useState} from 'react';
import {Alert, Button, Checkbox, Container, FormField, Header, Input, SpaceBetween, Table} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {Constants} from '../../common/constants';
import {ReconcileReport} from '../../client/accounts-client';

const defaults = {enabled: false, interval_minutes: 60, dry_run: true, reenable: true, max_disable_fraction: 0.25, check_cognito: false, okta: {org_url: '', api_token_secret_arn: ''}};
const flags = {enabled: 'Enable periodic reconciliation', dry_run: 'Periodic dry run', reenable: 'Re-enable restored users', check_cognito: 'Check Cognito'};

export default function AccountReconcileSettings({settings}: {settings: any}) {
    const [values, setValues] = useState(defaults);
    const [interval, setInterval] = useState('60');
    const [fraction, setFraction] = useState('0.25');
    const [dryRun, setDryRun] = useState(true);
    const [report, setReport] = useState<ReconcileReport | null>(null);
    const [reportDryRun, setReportDryRun] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [saved, setSaved] = useState(false);
    useEffect(() => {
        const loaded = {
            enabled: settings?.enabled ?? defaults.enabled,
            dry_run: settings?.dry_run ?? defaults.dry_run,
            reenable: settings?.reenable ?? defaults.reenable,
            check_cognito: settings?.check_cognito ?? defaults.check_cognito,
            interval_minutes: settings?.interval_minutes ?? defaults.interval_minutes,
            max_disable_fraction: settings?.max_disable_fraction ?? defaults.max_disable_fraction,
            okta: {org_url: settings?.okta?.org_url ?? '', api_token_secret_arn: settings?.okta?.api_token_secret_arn ?? ''},
        };
        setValues(loaded);
        setInterval(String(loaded.interval_minutes));
        setFraction(String(loaded.max_disable_fraction));
    }, [settings]);

    const save = async () => {
        setError(''); setSaved(false);
        if (!interval.trim() || !Number.isInteger(Number(interval)) || Number(interval) < 1 || Number(interval) > 1440) {
            setError('Interval must be an integer from 1 to 1440 minutes.'); return;
        }
        if (!fraction.trim() || !Number.isFinite(Number(fraction)) || Number(fraction) < 0 || Number(fraction) > 1) {
            setError('Maximum disable fraction must be a number from 0 to 1.'); return;
        }
        const {org_url: org, api_token_secret_arn: secret} = values.okta;
        if (Boolean(org) !== Boolean(secret)) {setError('Both Okta settings are required.'); return;}
        if (org) {
            try {
                const url = new URL(org);
                if (!org.startsWith('https://') || url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.search || url.hash || (url.pathname !== '/' && url.pathname !== '') || url.port || /\s/.test(org)) throw new Error();
            } catch {setError('Okta org URL must be an HTTPS origin on port 443.'); return;}
        }
        if (secret && !/^arn:aws(?:-us-gov|-cn)?:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$/.test(secret)) {
            setError('Okta token must be a Secrets Manager secret ARN.'); return;
        }
        setBusy(true);
        try {
            const moduleId = AppContext.get().getClusterSettingsService().getModuleId(Constants.MODULE_CLUSTER_MANAGER) || Constants.MODULE_CLUSTER_MANAGER;
            const result = await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId, settings: {accounts: {reconcile: {...values, interval_minutes: Number(interval), max_disable_fraction: Number(fraction)}}}});
            if (!result.success) throw new Error('Failed to update reconciliation settings.');
            setSaved(true);
        } catch (e: any) {setError(e?.message ?? String(e));}
        finally {setBusy(false);}
    };
    const run = async (override = false) => {
        setBusy(true); setError('');
        // A confirmation retains the mode of the refused request, even if the checkbox changed.
        const mode = override ? reportDryRun : dryRun;
        setReport(null);
        try {
            const result = await AppContext.get().client().accounts().reconcileUsers({dry_run: mode, override_max_disable_fraction: override});
            setReport(result); setReportDryRun(mode);
        } catch (e: any) {setError(e?.message ?? String(e));}
        finally {setBusy(false);}
    };
    return <Container header={<Header variant="h2" description="Keep IDEA user state aligned with the directory. Protected accounts are excluded.">Account reconciliation</Header>}>
        <SpaceBetween size="l">
            {error && <Alert type="error">{error}</Alert>}
            {saved && <Alert type="success">Reconciliation settings saved. Allow a few seconds for settings to refresh before running. Periodic checks pick up changes within a minute.</Alert>}
            {(Object.keys(flags) as Array<keyof typeof flags>).map(key => <Checkbox key={key} checked={values[key]} disabled={busy} onChange={e => setValues({...values, [key]: e.detail.checked})}>{flags[key]}</Checkbox>)}
            <FormField label="Interval (minutes)" description="1–1440 minutes between periodic runs."><Input ariaLabel="Interval (minutes)" value={interval} disabled={busy} onChange={e => setInterval(e.detail.value)}/></FormField>
            <FormField label="Maximum disable fraction" description="0–1; refuse the whole run when proposed disables exceed this fraction of eligible enabled users."><Input ariaLabel="Maximum disable fraction" value={fraction} disabled={busy} onChange={e => setFraction(e.detail.value)}/></FormField>
            <FormField label="Okta org URL" description="Deployment-approved HTTPS origin; set both Okta fields or leave both empty."><Input ariaLabel="Okta org URL" value={values.okta.org_url ?? ''} disabled={busy} onChange={e => setValues({...values, okta: {...values.okta, org_url: e.detail.value}})}/></FormField>
            <FormField label="Okta token secret ARN" description="Secrets Manager ARN, never the token. Redeploy cluster-manager IAM permissions after changing it; an encrypted secret may also need a decrypt grant."><Input ariaLabel="Okta token secret ARN" value={values.okta.api_token_secret_arn ?? ''} disabled={busy} onChange={e => setValues({...values, okta: {...values.okta, api_token_secret_arn: e.detail.value}})}/></FormField>
            <Button disabled={busy} onClick={save}>Save reconciliation settings</Button>
            <Header variant="h3">Run on demand</Header>
            <Checkbox checked={dryRun} disabled={busy} onChange={e => setDryRun(e.detail.checked)}>Dry run</Checkbox>
            <Button disabled={busy} onClick={() => run()}>Run now</Button>
            {report && <SpaceBetween size="m">
                {Boolean(report.refused) && <Alert type="warning" header="Reconciliation refused">{report.reason}. Proposed disables: {report.would_disable ?? 0} of {report.eligible_enabled ?? 0} eligible enabled users; cap: {report.max_disable_fraction ?? 0}.
                    {report.reason === 'max_disable_fraction exceeded' && <Button disabled={busy} onClick={() => run(true)}>Proceed anyway</Button>}
                </Alert>}
                <Table header={<Header variant="h3">{reportDryRun ? 'Dry-run report' : 'Applied-run report'}</Header>} items={[
                    {label: 'Checked', count: report.checked}, {label: 'Would disable', count: report.would_disable ?? 0},
                    {label: 'Would re-enable', count: report.would_reenable ?? 0}, {label: 'Missing', count: report.missing},
                    {label: 'Errors', count: report.errors}, {label: 'Disabled', count: report.disabled}, {label: 'Re-enabled', count: report.reenabled},
                ]} columnDefinitions={[{id: 'label', header: 'Result', cell: item => item.label}, {id: 'count', header: 'Count', cell: item => item.count}]}/>
                <Table items={report.changes} empty="No proposed changes" columnDefinitions={[
                    {id: 'user', header: 'User', cell: item => item.username}, {id: 'action', header: 'Action', cell: item => item.action},
                    {id: 'upstream', header: 'Upstream', cell: item => Object.entries(item.upstream).map(([key, value]) => `${key}: ${value}`).join(', ')},
                    {id: 'applied', header: 'Applied', cell: item => item.applied === true ? 'Yes' : 'No'},
                ]}/>
            </SpaceBetween>}
        </SpaceBetween>
    </Container>;
}
