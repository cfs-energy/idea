import { AppContext } from './common';
import Utils from './common/utils';

declare global {
    interface Window {
        idea: any;
    }
}

/** Initialize window.idea app data for tests, mirroring the local-development defaults in index.tsx. */
export function initTestAppData() {
    window.idea = window.idea || {};
    window.idea.app = {
        sso: false,
        version: '26.08.0',
        title: 'Integrated Digital Engineering on AWS',
        logo: '/logo.png',
        module_set: 'default',
        modules: Utils.getDefaultModuleSettings(),
        session_management: 'local-storage',
        default_log_level: 3
    };
}

/** Initialize window.idea and a real AppContext for tests. Local-storage session management, so no
 * service worker is required and no network calls are made while logged out. */
export function initTestAppContext(): AppContext {
    initTestAppData();
    // index.html renders this element; components hide it once mounted.
    if (document.getElementById('app-loading') == null) {
        const appLoading = document.createElement('div');
        appLoading.id = 'app-loading';
        document.body.appendChild(appLoading);
    }
    const context = new AppContext({
        httpEndpoint: 'http://localhost:8080',
        albEndpoint: 'http://localhost:8080',
        releaseVersion: '26.08.0',
        app: window.idea.app
    });
    window.idea.context = context;
    return context;
}
