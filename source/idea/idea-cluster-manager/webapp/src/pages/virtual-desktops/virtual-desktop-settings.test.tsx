import React from 'react';
import {render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, expect, it, vi} from 'vitest';
import VirtualDesktopSettings from './virtual-desktop-settings';
import {SettingsSource} from '../cluster-admin/settings-sections';
import {initTestAppContext} from '../../test-support';

afterEach(() => vi.restoreAllMocks());

it.each([false, true])('shows live connection settings without retired autoscaling panels (legacy rows: %s)', async legacy => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'getModuleInfo').mockReturnValue({});
    const autoscaling = {base_os: 'amazonlinux2', instance_type: 'obsolete-instance', min_capacity: 1, max_capacity: 3};
    vi.spyOn(context.getClusterSettingsService(), 'getClusterSettings').mockResolvedValue({});
    vi.spyOn(context.getClusterSettingsService(), 'getVirtualDesktopSettings').mockResolvedValue({
        controller: legacy ? {autoscaling, asg_arn: 'retired-controller'} : {},
        dcv_broker: {...(legacy ? {autoscaling, asg_arn: 'retired-broker'} : {}), client_communication_port: 8443},
        dcv_connection_gateway: {...(legacy ? {autoscaling, asg_arn: 'retired-gateway'} : {}), certificate: {provided: true, custom_dns_name: 'desktop.example.org'}},
    });
    render(<MemoryRouter><VirtualDesktopSettings {...{} as any} renderSections={({sections}: SettingsSource) => <>{sections.map(section => <section key={section.id} aria-label={section.label}>{section.content}</section>)}</>} /></MemoryRouter>);
    expect(await screen.findByText('desktop.example.org')).toBeInTheDocument();
    expect(screen.getByText('8443')).toBeInTheDocument();
    expect(screen.getByRole('heading', {name: 'Connection Details'})).toBeInTheDocument();
    expect(screen.getByRole('heading', {name: 'Certificate'})).toBeInTheDocument();
    for (const name of ['VDI Controller', 'Amazon DCV Broker', 'Amazon DCV Connection Gateway', 'Autoscaling']) {
        expect(screen.queryByRole('heading', {name})).not.toBeInTheDocument();
    }
    expect(screen.queryByText('obsolete-instance')).not.toBeInTheDocument();
    expect(screen.queryByText('retired-controller')).not.toBeInTheDocument();
});
