import { describe, expect, it, vi } from 'vitest'
import VirtualDesktopAdminClient from './virtual-desktop-admin-client'

describe('VirtualDesktopAdminClient', () => {
    it('refreshBaseSoftwareStackAmis invokes its namespace', async () => {
        const invoke_alt = vi.fn().mockResolvedValue({ results: [] })
        const client = Object.create(VirtualDesktopAdminClient.prototype) as VirtualDesktopAdminClient
        ;(client as any).apiInvoker = { invoke_alt }

        const response = await client.refreshBaseSoftwareStackAmis({})

        expect(invoke_alt).toHaveBeenCalledWith('VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis', {})
        expect(response.results).toEqual([])
    })

    it('carries stack_ids through', async () => {
        const invoke_alt = vi.fn().mockResolvedValue({ results: [] })
        const client = Object.create(VirtualDesktopAdminClient.prototype) as VirtualDesktopAdminClient
        ;(client as any).apiInvoker = { invoke_alt }

        await client.refreshBaseSoftwareStackAmis({ stack_ids: ['ss-base-a', 'ss-base-b'] })

        expect(invoke_alt).toHaveBeenCalledWith('VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis', { stack_ids: ['ss-base-a', 'ss-base-b'] })
    })

    it('listDesktopImages and buildDesktopImage invoke their namespaces', async () => {
        const invoke_alt = vi.fn().mockResolvedValue({ listing: [], record: { status: 'building' } })
        const client = Object.create(VirtualDesktopAdminClient.prototype) as VirtualDesktopAdminClient
        ;(client as any).apiInvoker = { invoke_alt }

        await client.listDesktopImages({})
        await client.buildDesktopImage({ base_os: 'rocky9', architecture: 'x86_64', update_stack: true })

        expect(invoke_alt).toHaveBeenCalledWith('VirtualDesktopAdmin.ListDesktopImages', {})
        expect(invoke_alt).toHaveBeenCalledWith('VirtualDesktopAdmin.BuildDesktopImage', { base_os: 'rocky9', architecture: 'x86_64', update_stack: true })
    })

    it('useBuiltDesktopImages invokes its namespace', async () => {
        const invoke_alt = vi.fn().mockResolvedValue({ results: [] })
        const client = Object.create(VirtualDesktopAdminClient.prototype) as VirtualDesktopAdminClient
        ;(client as any).apiInvoker = { invoke_alt }

        await client.useBuiltDesktopImages({ stack_ids: ['ss-base-a'] })

        expect(invoke_alt).toHaveBeenCalledWith('VirtualDesktopAdmin.UseBuiltDesktopImages', { stack_ids: ['ss-base-a'] })
    })

    it.each([
        ['listImageRows', 'ListImageRows', { filter: { base_os_family: 'rocky' } }],
        ['refreshImages', 'RefreshImages', { all: true }],
        ['rollbackImage', 'RollbackImage', { row: { base_os: 'rocky9', architecture: 'x86_64', variant: 'cpu' } }],
        ['setImagePinned', 'SetImagePinned', { row: { base_os: 'rocky9', architecture: 'x86_64' }, pinned: true }],
        ['getImageSchedule', 'GetImageSchedule', {}],
        ['updateImageSchedule', 'UpdateImageSchedule', { schedule: { enabled: true, day: 'first sunday', hour: 2 } }],
    ])('%s invokes VirtualDesktopAdmin.%s', async (method, api, req) => {
        const invoke_alt = vi.fn().mockResolvedValue({})
        const client = Object.create(VirtualDesktopAdminClient.prototype) as any
        client.apiInvoker = { invoke_alt }

        await client[method](req)

        expect(invoke_alt).toHaveBeenCalledWith(`VirtualDesktopAdmin.${api}`, req)
    })
})
