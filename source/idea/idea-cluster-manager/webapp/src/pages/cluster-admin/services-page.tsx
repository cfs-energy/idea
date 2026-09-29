import React, {useEffect, useState} from 'react';
import {Alert, Header, Link, SpaceBetween} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {hasAccess} from '../../navigation/task-navigation';
import DesktopSettings from '../virtual-desktops/virtual-desktop-settings';
import {SettingsSource} from './settings-sections';
import ClusterServices, {hasContainerControlPlane} from './cluster-services';

export function SettingsServiceDetails(props: IdeaAppLayoutProps) {
    const context = AppContext.get();
    const modules = [
        {name: 'cluster-manager', title: 'Control plane', visible: hasAccess(context, 'cluster-admin')},
        {name: 'virtual-desktop-controller', title: 'Desktop services', visible: hasAccess(context, 'desktop-admin')},
        {name: 'scheduler', title: 'Job service', visible: hasAccess(context, 'jobs-admin')},
    ].filter(module => module.visible);
    return <SpaceBetween size="m">
        <Alert type="info">These are deployment settings, not a live service inventory. Host autoscaling settings may not describe container capacity. Use Operations runbooks to change capacity or restart services.</Alert>
        <Link external href="https://docs.idea-hpc.com/first-time-users/cluster-operations">CLI runbooks</Link>
        {modules.map(module => {
            const info = context.getClusterSettingsService().getModuleInfo(module.name);
            return <section key={module.name}>
                <Header variant="h2">{module.title}</Header>
                <dl><dt>Module</dt><dd>{info?.name}</dd><dt>Module ID</dt><dd>{info?.module_id}</dd><dt>Version</dt><dd>{info?.version}</dd></dl>
                {module.name === 'virtual-desktop-controller' && <DesktopSettings {...props} renderSections={(source: SettingsSource) => <SpaceBetween size="m">
                    {source.sections.filter(section => ['controller', 'broker', 'connection-gateway'].includes(section.id)).map(section => {
                        const content = section.content;
                        return <div key={section.id}>{section.id === 'controller' ? content : React.isValidElement<{children: React.ReactNode}>(content) ? React.Children.toArray(content.props.children).slice(0, 2) : null}</div>;
                    })}
                </SpaceBetween>}/>}
            </section>;
        })}
    </SpaceBetween>;
}

export function ServicesPage(props: IdeaAppLayoutProps) {
    const context = AppContext.get();
    const authorized = (['cluster-admin', 'desktop-admin', 'jobs-admin'] as const).some(access => hasAccess(context, access));
    const [container, setContainer] = useState<boolean>();
    const [error, setError] = useState(false);
    useEffect(() => {
        if (!authorized) return;
        let cancelled = false;
        hasContainerControlPlane().then(value => { if (!cancelled) setContainer(value); }).catch(() => { if (!cancelled) setError(true); });
        return () => { cancelled = true; };
    }, [authorized]);
    return <IdeaAppLayout {...props} header={<Header variant="h1">Services</Header>} content={
        !authorized ? <Alert type="info">No services available.</Alert> : error ? <Alert type="error">Could not read service settings. Reload the page to try again.</Alert>
            : container === undefined ? <p>Loading service settings</p> : container ? <ClusterServices/> : <SettingsServiceDetails {...props}/>
    }/>;
}
