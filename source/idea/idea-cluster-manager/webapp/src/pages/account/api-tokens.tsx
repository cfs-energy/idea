import React, {useEffect, useState} from 'react'
import {Alert, Box, Button, Container, CopyToClipboard, FormField, Header, Input, Modal, Select, SpaceBetween, Table} from '@cloudscape-design/components'
import AppContext from '../../common/app-context'
import {ApiToken, CreateApiTokenResult} from '../../client/data-model'

const EXPIRIES = [30, 90, 180, 365].map(days => ({label: `${days} days`, value: `${days}`}))
const date = (seconds?: number | null) => seconds ? new Date(seconds * 1000).toLocaleString() : 'Never'

export default function ApiTokens() {
    const [tokens, setTokens] = useState<ApiToken[]>([])
    const [loading, setLoading] = useState(true)
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState('')
    const [visible, setVisible] = useState(false)
    const [name, setName] = useState('')
    const [expiry, setExpiry] = useState(EXPIRIES[0])
    const [created, setCreated] = useState<CreateApiTokenResult | null>(null)
    const [loadFailed, setLoadFailed] = useState(false)
    const [toRevoke, setToRevoke] = useState<ApiToken | null>(null)
    const client = AppContext.get().client().auth()

    const load = () => {
        setLoading(true)
        setError('')
        setLoadFailed(false)
        client.listApiTokens().then(result => setTokens(result.listing ?? [])).catch(error => {
            setError(error?.message ?? 'Could not load API tokens.')
            setLoadFailed(true)
        }).finally(() => setLoading(false))
    }
    useEffect(load, [client])

    const close = () => {
        if (busy) return
        setVisible(false)
        setCreated(null)
        setName('')
    }
    const create = async () => {
        setBusy(true)
        setError('')
        try {
            const result = await client.createApiToken({name: name.trim(), expires_in_days: Number(expiry.value)})
            setCreated(result)
            setTokens(rows => [...rows, {
                token_id: result.token_id, username: AppContext.get().auth().getUsername(), name: name.trim(),
                created_on: result.expires_on - Number(expiry.value) * 86400, expires_on: result.expires_on
            }])
        } catch (error: any) {
            setError(error?.message ?? 'Could not create API token.')
        } finally {
            setBusy(false)
        }
    }
    const revoke = async (token: ApiToken) => {
        setToRevoke(null)
        setBusy(true)
        setError('')
        try {
            await client.deleteApiToken({token_id: token.token_id})
            setTokens(rows => rows.filter(row => row.token_id !== token.token_id))
        } catch (error: any) {
            setError(error?.message ?? 'Could not revoke API token.')
        } finally {
            setBusy(false)
        }
    }

    return <Container header={<Header variant="h2" actions={<Button disabled={loading || busy} onClick={() => {setError(''); setVisible(true)}}>Create token</Button>}>API tokens</Header>}>
        <SpaceBetween size="m">
            <Box>Tokens use your current permissions for automation. Revocation takes effect across services within 60 seconds.</Box>
            {error && !visible && <Alert type="error" action={loadFailed ? <Button onClick={load}>Retry</Button> : undefined}>{error}</Alert>}
            <Table variant="embedded" items={tokens} loading={loading} loadingText="Loading API tokens" trackBy="token_id"
                empty={<Box>No API tokens</Box>}
                columnDefinitions={[
                    {id: 'name', header: 'Name', cell: item => item.name},
                    {id: 'created', header: 'Created', cell: item => date(item.created_on)},
                    {id: 'expires', header: 'Expires', cell: item => date(item.expires_on)},
                    {id: 'last-used', header: 'Last used', cell: item => date(item.last_used_on)},
                    {id: 'revoke', header: 'Actions', cell: item => <Button disabled={busy} ariaLabel={`Revoke ${item.name}`} onClick={() => setToRevoke(item)}>Revoke</Button>}
                ]}/>
            <Modal visible={toRevoke != null} onDismiss={() => setToRevoke(null)} header="Revoke token"
                footer={<SpaceBetween direction="horizontal" size="s">
                    <Button onClick={() => setToRevoke(null)}>Cancel</Button>
                    <Button variant="primary" onClick={() => revoke(toRevoke!)}>Revoke</Button>
                </SpaceBetween>}>
                Revoke {toRevoke?.name}? Anything using this token stops working within 60 seconds.
            </Modal>
            <Modal visible={visible} onDismiss={close} header={created ? 'API token created' : 'Create API token'}
                footer={<SpaceBetween direction="horizontal" size="s">
                    <Button disabled={busy} onClick={close}>{created ? 'Done' : 'Cancel'}</Button>
                    {!created && <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={create}>Create</Button>}
                </SpaceBetween>}>
                {created ? <SpaceBetween size="m">
                    <Alert type="warning">Copy this token now. It will not be shown again.</Alert>
                    <CopyToClipboard copyButtonText="Copy token" copySuccessText="Token copied" copyErrorText="Could not copy token" textToCopy={created.token}/>
                    <Box>{created.token}</Box>
                </SpaceBetween> : <SpaceBetween size="m">
                    {error && <Alert type="error">{error}</Alert>}
                    <FormField label="Token name"><Input value={name} onChange={event => setName(event.detail.value)} disabled={busy}/></FormField>
                    <FormField label="Expires after"><Select selectedOption={expiry} options={EXPIRIES} disabled={busy}
                        onChange={event => setExpiry(EXPIRIES.find(option => option.value === event.detail.selectedOption.value)!)}/></FormField>
                </SpaceBetween>}
            </Modal>
        </SpaceBetween>
    </Container>
}
