import imageCompression from 'browser-image-compression'
import { resizeApplicationIcon } from './update-hpc-application'

vi.mock('browser-image-compression', () => ({ default: vi.fn(async (file: File) => file) }))

describe('application icon resize', () => {
    it('compresses on the page thread, so no worker script is fetched from a CDN', async () => {
        const icon = new File(['x'], 'icon.png', { type: 'image/png' })
        await expect(resizeApplicationIcon(icon)).resolves.toBe(icon)
        expect(imageCompression).toHaveBeenCalledWith(icon, { maxWidthOrHeight: 150, useWebWorker: false })
    })
})
