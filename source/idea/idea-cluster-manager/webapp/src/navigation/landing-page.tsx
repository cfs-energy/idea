import React, {useEffect, useState} from 'react';
import {Navigate} from 'react-router-dom';
import {AppContext} from '../common';
import {Constants} from '../common/constants';
import {LANDING_PATHS} from './task-navigation';

export function resolveLandingPath(userChoice?: string, clusterChoice?: string): string {
    return LANDING_PATHS[userChoice || ''] ?? LANDING_PATHS[clusterChoice || ''] ?? LANDING_PATHS.home;
}

export const LandingPage: React.FC = () => {
    const [path, setPath] = useState<string | null>(null);
    useEffect(() => {
        const context = AppContext.get();
        Promise.all([
            context.auth().getUser(),
            context.getClusterSettingsService().getModuleSettings(Constants.MODULE_CLUSTER_MANAGER)
        ]).then(([user, settings]) => {
            setPath(resolveLandingPath(user.landing_page, settings?.web_portal?.default_landing_page));
        }).catch(() => setPath(LANDING_PATHS.home));
    }, []);
    return path ? <Navigate to={path} replace/> : null;
};
