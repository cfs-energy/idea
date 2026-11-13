import React, {useEffect, useState} from 'react';
import {Navigate} from 'react-router-dom';
import {AppContext} from '../common';
import {Constants} from '../common/constants';
import {LANDING_PATHS} from './task-navigation';

export function resolveLandingPath(userChoice?: string, clusterChoice?: string, dashboardEnabled = true): string {
    const usable = (choice?: string) => choice === 'reports' && !dashboardEnabled ? undefined : LANDING_PATHS[choice || ''];
    return usable(userChoice) ?? usable(clusterChoice) ?? LANDING_PATHS['my-costs'];
}

export const LandingPage: React.FC = () => {
    const [path, setPath] = useState<string | null>(null);
    useEffect(() => {
        const context = AppContext.get();
        Promise.all([
            context.auth().getUser(),
            context.getClusterSettingsService().getModuleSettings(Constants.MODULE_CLUSTER_MANAGER)
        ]).then(([user, settings]) => {
            setPath(resolveLandingPath(user.landing_page, settings?.web_portal?.default_landing_page, context.getClusterSettingsService().isCustomDashboardEnabled()));
        }).catch(() => setPath(LANDING_PATHS['my-costs']));
    }, []);
    return path ? <Navigate to={path} replace/> : null;
};
