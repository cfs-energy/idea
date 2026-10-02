import { describe, expect, it, vi } from 'vitest'
import SchedulerAdminClient from './scheduler-admin-client'

describe('SchedulerAdminClient', () => {
    it('listComputeImages invokes its namespace', async () => {
        const invoke_alt = vi.fn().mockResolvedValue({ listing: [] })
        const client = Object.create(SchedulerAdminClient.prototype) as SchedulerAdminClient
        ;(client as any).apiInvoker = { invoke_alt }

        const response = await client.listComputeImages({})

        expect(invoke_alt).toHaveBeenCalledWith('SchedulerAdmin.ListComputeImages', {})
        expect(response.listing).toEqual([])
    })

    it('buildComputeImage carries the request through', async () => {
        const invoke_alt = vi.fn().mockResolvedValue({ record: { status: 'building' } })
        const client = Object.create(SchedulerAdminClient.prototype) as SchedulerAdminClient
        ;(client as any).apiInvoker = { invoke_alt }

        const response = await client.buildComputeImage({ base_os: 'rocky9', enable_drivers: ['efa'] })

        expect(invoke_alt).toHaveBeenCalledWith('SchedulerAdmin.BuildComputeImage', { base_os: 'rocky9', enable_drivers: ['efa'] })
        expect(response.record?.status).toEqual('building')
    })

    it.each([
        ['listImageRows', 'ListImageRows', { filter: { base_os_family: 'rocky' } }],
        ['refreshImages', 'RefreshImages', { all: true }],
        ['rollbackImage', 'RollbackImage', { row: { base_os: 'rocky9', architecture: 'x86_64', variant: 'cpu' } }],
        ['setImagePinned', 'SetImagePinned', { row: { base_os: 'rocky9', architecture: 'x86_64' }, pinned: true }],
        ['getImageSchedule', 'GetImageSchedule', {}],
    ])('%s invokes SchedulerAdmin.%s', async (method, api, req) => {
        const invoke_alt = vi.fn().mockResolvedValue({})
        const client = Object.create(SchedulerAdminClient.prototype) as any
        client.apiInvoker = { invoke_alt }

        await client[method](req)

        expect(invoke_alt).toHaveBeenCalledWith(`SchedulerAdmin.${api}`, req)
    })
})
