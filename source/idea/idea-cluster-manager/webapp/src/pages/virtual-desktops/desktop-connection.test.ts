import {afterEach, expect, it, vi} from 'vitest';
import {AppContext} from '../../common';
import {downloadDcvFile} from './desktop-connection';
import {initTestAppData} from '../../test-support';

afterEach(() => vi.restoreAllMocks());

const savedFile = async (settings: () => Promise<any>) => {
    initTestAppData();
    vi.spyOn(AppContext, 'get').mockReturnValue({
        auth: () => ({getUsername: () => 'marker-user'}),
        getClusterSettingsService: () => ({getVirtualDesktopSettings: settings}),
    } as any);
    let href = '';
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {href = this.getAttribute('href')!;});
    await downloadDcvFile({endpoint: 'https://gateway.example.org', access_token: 'marker-token', dcv_session_id: 'marker-session'}, 'desk');
    const file = decodeURIComponent(href.replace('data:text/plain;charset=utf-8,', ''));
    expect(file).toContain('host=gateway.example.org\n');
    expect(file).toContain('authtoken=marker-token\n');
    return file;
};

it.each([
    ['the operator provided the gateway certificate', () => Promise.resolve({dcv_connection_gateway: {certificate: {provided: true}}})],
    ['the setting is not in the settings read', () => Promise.resolve({})],
    ['the settings cannot be read', () => Promise.reject(new Error('marker'))],
])('leaves certificate checks to the client when %s', async (_, settings) => {
    const file = await savedFile(settings);
    expect(file).not.toMatch(/certificatevalidationpolicy/i);
});

it('tells the client to accept the gateway certificate when the cluster uses its own self-signed one', async () => {
    const file = await savedFile(() => Promise.resolve({dcv_connection_gateway: {certificate: {provided: false}}}));
    expect(file).toContain('certificatevalidationpolicy=accept-untrusted\n');
});
