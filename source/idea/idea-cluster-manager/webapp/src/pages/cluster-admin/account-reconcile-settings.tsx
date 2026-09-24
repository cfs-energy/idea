import React, {useEffect, useState} from 'react';
import {Alert, Button, Checkbox, Container, ExpandableSection, FormField, Header, Input, Link, SpaceBetween, Table, Toggle} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {Constants} from '../../common/constants';
import {ReconcileReport} from '../../client/accounts-client';

const defaults = {enabled: false, interval_minutes: 60, dry_run: true, reenable: true, max_disable_fraction: 0.25, check_cognito: false, okta: {org_url: '', api_token_secret_arn: ''}};
const flags = {dry_run: 'Periodic dry run', reenable: 'Re-enable restored users', check_cognito: 'Check Cognito'};
const timestamp = (value: number) => new Date(Number(value) * 1000).toLocaleString();

export default function AccountReconcileSettings({active, identityProvider, mode = 'all'}: {active: boolean; identityProvider: any; mode?: 'policy' | 'runs' | 'all'}) {
    const [values, setValues] = useState(defaults);
    const [stored, setStored] = useState<any>(null);
    const [interval, setInterval] = useState('');
    const [fraction, setFraction] = useState('');
    const [alsoOkta, setAlsoOkta] = useState(false);
    const [report, setReport] = useState<ReconcileReport | null>(null);
    const [reportDryRun, setReportDryRun] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const [saved, setSaved] = useState(false);
    const [expanded, setExpanded] = useState(false);
    const [freshIdentityProvider, setFreshIdentityProvider] = useState<any>(null);
    const provider = freshIdentityProvider ?? identityProvider;
    const providerIsOkta = [provider?.provider, provider?.cognito?.sso_idp_provider_name]
        .some(provider => String(provider ?? '').toLowerCase() === 'okta');
    const moduleId = () => AppContext.get().getClusterSettingsService().getModuleId(Constants.MODULE_CLUSTER_MANAGER) || Constants.MODULE_CLUSTER_MANAGER;
    const fetchSettings = async () => {
        const result = await AppContext.get().client().clusterSettings().getModuleSettings({module_id: moduleId()});
        const settings = (result.settings as any)?.accounts?.reconcile;
        if (!settings) throw new Error('Reconciliation settings could not be read. Reopen this page with administrator access.');
        return settings;
    };
    const showSettings = (settings: any) => {
        const loaded = {...defaults, ...settings, okta: {org_url: settings.okta?.org_url ?? '', api_token_secret_arn: settings.okta?.api_token_secret_arn ?? ''}};
        setValues(loaded);
        setInterval(String(loaded.interval_minutes));
        setFraction(String(loaded.max_disable_fraction));
        setAlsoOkta(Boolean(loaded.okta.org_url || loaded.okta.api_token_secret_arn));
        setStored(settings);
    };
    useEffect(() => {
        if (!active) return;
        let cancelled = false;
        setLoading(true); setStored(null); setReport(null); setError(''); setSaved(false); setExpanded(false);
        setFreshIdentityProvider(null);
        const providerId = AppContext.get().getClusterSettingsService().getModuleId(Constants.MODULE_IDENTITY_PROVIDER);
        if (providerId) {
            AppContext.get().client().clusterSettings().getModuleSettings({module_id: providerId}).then(result => {
                if (!cancelled) setFreshIdentityProvider(result.settings);
            }).catch(e => {if (!cancelled) setError(e?.message ?? String(e));});
        }
        fetchSettings().then(settings => {
            if (cancelled) return;
            showSettings(settings);
            setReport(settings.last_run?.report ?? null);
            setReportDryRun(settings.last_run?.report?.dry_run ?? true);
        }).catch(e => {if (!cancelled) setError(e?.message ?? String(e));})
            .finally(() => {if (!cancelled) setLoading(false);});
        return () => {cancelled = true;};
    }, [active]);
    useEffect(() => {
        if (!active || loading || busy || !stored) return;
        let cancelled = false;
        const timer = window.setInterval(() => {
            fetchSettings().then(settings => {
                if (cancelled) return;
                setStored(settings);
                if (settings.last_run && settings.last_run.at !== stored.last_run?.at) {
                    setReport(settings.last_run.report);
                    setReportDryRun(settings.last_run.report.dry_run);
                }
            }).catch(e => {if (!cancelled) setError(e?.message ?? String(e));});
        }, 10000);
        return () => {cancelled = true; window.clearInterval(timer);};
    }, [active, loading, busy, stored]);

    const write = async (settings: any) => {
        setBusy(true); setError(''); setSaved(false);
        try {
            const result = await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId(), settings: {accounts: {reconcile: settings}}});
            if (!result.success) throw new Error('Failed to update reconciliation settings.');
            showSettings(await fetchSettings());
            setSaved(true);
        } catch (e: any) {setError(e?.message ?? String(e));}
        finally {setBusy(false);}
    };
    const save = async () => {
        setError(''); setSaved(false);
        if (!interval.trim() || !Number.isInteger(Number(interval)) || Number(interval) < 1 || Number(interval) > 1440) {
            setError('Interval must be an integer from 1 to 1440 minutes.'); return;
        }
        if (!fraction.trim() || !Number.isFinite(Number(fraction)) || Number(fraction) < 0 || Number(fraction) > 1) {
            setError('Maximum disable fraction must be a number from 0 to 1.'); return;
        }
        const {org_url: org, api_token_secret_arn: secret} = (providerIsOkta || alsoOkta) ? values.okta : {org_url: '', api_token_secret_arn: ''};
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
        await write({enabled: stored.enabled, interval_minutes: Number(interval), max_disable_fraction: Number(fraction),
            dry_run: values.dry_run, reenable: values.reenable, check_cognito: values.check_cognito,
            okta: (providerIsOkta || alsoOkta) ? values.okta : {org_url: '', api_token_secret_arn: ''}});
    };
    const run = async (mode: boolean, override = false) => {
        setBusy(true); setError('');
        try {
            const result = await AppContext.get().client().accounts().reconcileUsers({dry_run: mode, override_max_disable_fraction: override});
            setReport(result); setReportDryRun(mode);
            setStored(await fetchSettings());
        } catch (e: any) {setError(e?.message ?? String(e));}
        finally {setBusy(false);}
    };
    const nextRun = !stored?.enabled ? 'Off' : stored.last_completed
        ? timestamp(Number(stored.last_completed) + Number(stored.interval_minutes) * 60) : 'Due now';
    const lastRun = stored?.last_run;
    const dirty = stored && (values.dry_run !== stored.dry_run || values.reenable !== stored.reenable || values.check_cognito !== stored.check_cognito
        || interval !== String(stored.interval_minutes) || fraction !== String(stored.max_disable_fraction)
        || values.okta.org_url !== (stored.okta?.org_url ?? '') || values.okta.api_token_secret_arn !== (stored.okta?.api_token_secret_arn ?? '')
        || (!alsoOkta && !providerIsOkta && Boolean(stored.okta?.org_url || stored.okta?.api_token_secret_arn)));
    return <Container header={<Header variant="h2" description="Keep IDEA user state aligned with the directory. Protected accounts are excluded.">{mode === 'policy' ? 'Account synchronization' : 'Reconciliation runs'}</Header>}>
        <SpaceBetween size="l">
            {error && <Alert type="error">{error}</Alert>}
            {loading && <div>Loading saved reconciliation settings…</div>}
            {!loading && !stored && <Button onClick={() => window.location.reload()}>Reload settings</Button>}
            {!loading && stored && <SpaceBetween size="l">
            {mode !== 'runs' && <SpaceBetween size="s">
            {saved && <Alert type="success">Reconciliation settings saved. The periodic worker has been notified.</Alert>}
            <div>Last saved: {stored.last_saved ? timestamp(stored.last_saved) : 'Time unavailable for existing settings'}{dirty ? ' - Unsaved advanced changes' : ' - Saved values loaded'}</div>
            <Toggle checked={Boolean(stored.enabled)} disabled={busy} onChange={e => write({enabled: e.detail.checked})}>Reconciliation on</Toggle>
            <div>Changes notify the periodic worker immediately.</div>
            <div>Next run at: {nextRun}</div>
            </SpaceBetween>}
            {mode !== 'policy' && <SpaceBetween size="s">
            <Link href="#/cluster/settings?group=account-synchronization">Edit synchronization policy in Settings</Link>
            <div>Last run at / result: {lastRun ? `${timestamp(lastRun.at)} / ${lastRun.report.refused ? 'Refused' : lastRun.report.errors ? 'Completed with errors' : 'Completed'} (${lastRun.report.dry_run ? 'dry run' : 'apply'})` : stored.last_completed ? `${timestamp(stored.last_completed)} / Result unavailable` : 'No runs recorded'}</div>
            <SpaceBetween direction="horizontal" size="s">
                <Button disabled={busy} onClick={() => run(true)}>Run now (dry run)</Button>
                <Button disabled={busy} onClick={() => run(false)}>Run now (apply)</Button>
            </SpaceBetween>
            {dirty && <div>Run now uses the saved settings. Save advanced changes to use them.</div>}
            </SpaceBetween>}
            {mode !== 'runs' && <ExpandableSection headerText="Advanced" expanded={expanded} onChange={e => setExpanded(e.detail.expanded)}>
            {expanded && (
            <SpaceBetween size="m">
            {(Object.keys(flags) as Array<keyof typeof flags>).map(key => <Checkbox key={key} checked={values[key]} disabled={busy} onChange={e => setValues({...values, [key]: e.detail.checked})}>{flags[key]}</Checkbox>)}
            <FormField label="Interval (minutes)" description="1–1440 minutes between periodic runs."><Input ariaLabel="Interval (minutes)" value={interval} disabled={busy} onChange={e => setInterval(e.detail.value)}/></FormField>
            <FormField label="Maximum disable fraction" description="0–1; refuse when proposed disables exceed this fraction of eligible enabled users, or read errors exceed this fraction of checked users."><Input ariaLabel="Maximum disable fraction" value={fraction} disabled={busy} onChange={e => setFraction(e.detail.value)}/></FormField>
            {!providerIsOkta && <Checkbox checked={alsoOkta} disabled={busy} onChange={e => setAlsoOkta(e.detail.checked)}>Also check Okta</Checkbox>}
            {(providerIsOkta || alsoOkta) && <SpaceBetween size="m">
            <FormField label="Okta org URL" description="Deployment-approved HTTPS origin; set both Okta fields or leave both empty."><Input ariaLabel="Okta org URL" value={values.okta.org_url ?? ''} disabled={busy} onChange={e => setValues({...values, okta: {...values.okta, org_url: e.detail.value}})}/></FormField>
            <FormField label="Okta token secret ARN" description="Secrets Manager ARN, never the token. Redeploy cluster-manager IAM permissions after changing it; an encrypted secret may also need a decrypt grant."><Input ariaLabel="Okta token secret ARN" value={values.okta.api_token_secret_arn ?? ''} disabled={busy} onChange={e => setValues({...values, okta: {...values.okta, api_token_secret_arn: e.detail.value}})}/></FormField>
            </SpaceBetween>}
            <Button disabled={busy} onClick={save}>Save reconciliation settings</Button>
            </SpaceBetween>
            )}
            </ExpandableSection>}
            </SpaceBetween>}
            {mode === 'policy' && <Link href="#/cluster/users?view=reconciliation">Run now and reports in People and access</Link>}
            {mode !== 'policy' && report && <SpaceBetween size="m">
                {Boolean(report.refused) && <Alert type="warning" header="Reconciliation refused">{report.reason}. Proposed disables: {report.would_disable ?? 0} of {report.eligible_enabled ?? 0} eligible enabled users; cap: {report.max_disable_fraction ?? 0}.
                    {report.reason === 'max_disable_fraction exceeded' && <Button disabled={busy} onClick={() => run(reportDryRun, true)}>Proceed anyway</Button>}
                </Alert>}
                <Table header={<Header variant="h3">{reportDryRun ? 'Dry-run report' : 'Applied-run report'}</Header>} items={[
                    {label: 'Checked', count: report.checked}, {label: 'Would disable', count: report.would_disable ?? 0},
                    {label: 'Would re-enable', count: report.would_reenable ?? 0}, {label: 'Missing', count: report.missing},
                    {label: 'Errors', count: report.errors}, {label: 'Disabled', count: report.disabled}, {label: 'Re-enabled', count: report.reenabled},
                ]} columnDefinitions={[{id: 'label', header: 'Result', cell: item => item.label}, {id: 'count', header: 'Count', cell: item => item.count}]}/>
                {report.truncated && <div>The saved report shows the first 100 rows. Run again for a full report.</div>}
                <Table items={report.changes} empty="No proposed changes" columnDefinitions={[
                    {id: 'user', header: 'User', cell: item => item.username}, {id: 'action', header: 'Action', cell: item => item.action},
                    {id: 'upstream', header: 'Upstream', cell: item => Object.entries(item.upstream).map(([key, value]) => `${key}: ${value}`).join(', ')},
                    {id: 'error', header: 'Error', cell: item => item.error ?? ''},
                    {id: 'applied', header: 'Applied', cell: item => item.applied === true ? 'Yes' : 'No'},
                ]}/>
            </SpaceBetween>}
        </SpaceBetween>
    </Container>;
}
