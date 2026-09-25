import {render, screen, waitFor} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApiTokens from './api-tokens'
import {initTestAppContext} from '../../test-support'

const setup = () => {
    const context = initTestAppContext()
    const client = context.client().auth()
    vi.spyOn(context.auth(), 'getUsername').mockReturnValue('user-a')
    vi.spyOn(client, 'listApiTokens').mockResolvedValue({listing: []})
    vi.spyOn(client, 'createApiToken').mockResolvedValue({token: `idea_${'a'.repeat(43)}`, token_id: 'public-id', expires_on: 2000000000})
    vi.spyOn(client, 'deleteApiToken').mockResolvedValue({})
    return client
}

afterEach(() => vi.restoreAllMocks())

it('creates a token, shows it once, copies it and revokes it', async () => {
    const client = setup()
    const user = userEvent.setup()
    render(<ApiTokens/>)
    await waitFor(() => expect(screen.getByRole('button', {name: 'Create token'})).toBeEnabled())
    await user.click(screen.getByRole('button', {name: 'Create token'}))
    await user.type(screen.getByRole('textbox', {name: 'Token name'}), 'automation')
    await user.click(screen.getByRole('button', {name: 'Create'}))
    expect(await screen.findByText('Copy this token now. It will not be shown again.')).toBeInTheDocument()
    expect(client.createApiToken).toHaveBeenCalledWith({name: 'automation', expires_in_days: 30})
    await user.click(screen.getByRole('button', {name: 'Copy token'}))
    await user.click(screen.getByRole('button', {name: 'Done'}))
    expect(screen.queryByText(`idea_${'a'.repeat(43)}`)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', {name: 'Revoke automation'}))
    await waitFor(() => expect(screen.queryByText('automation')).not.toBeInTheDocument())
    expect(client.deleteApiToken).toHaveBeenCalledWith({token_id: 'public-id'})
})

it('keeps the form open when creation fails', async () => {
    const client = setup()
    vi.mocked(client.createApiToken).mockRejectedValue(new Error('Creation failed'))
    const user = userEvent.setup()
    render(<ApiTokens/>)
    await waitFor(() => expect(screen.getByRole('button', {name: 'Create token'})).toBeEnabled())
    await user.click(screen.getByRole('button', {name: 'Create token'}))
    await user.type(screen.getByRole('textbox', {name: 'Token name'}), 'automation')
    await user.click(screen.getByRole('button', {name: 'Create'}))
    expect(await screen.findByText('Creation failed')).toBeInTheDocument()
    expect(screen.getByRole('textbox', {name: 'Token name'})).toHaveValue('automation')
})
