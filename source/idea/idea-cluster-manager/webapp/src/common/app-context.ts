/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

import {v4 as uuid} from "uuid"
import {AuthService, LocalStorageService} from "../service";
import IdeaException from "../common/exceptions";
import JobTemplatesService from "../service/job-templates-service";
import ClusterSettingsService from "../service/cluster-settings-service";
import Utils from "./utils";
import {Constants} from "./constants";
import {IdeaAuthenticationContext} from "./authentication-context";
import {IdeaClients} from "../client";
import AppLogger from "./app-logger";

export interface AppContextProps {
    httpEndpoint: string
    albEndpoint: string
    releaseVersion: string
    app: AppData
    serviceWorkerRegistration?: ServiceWorkerRegistration
}

export interface AppData {
    sso: boolean
    version: string
    title: string
    subtitle?: string
    logo?: string
    copyright_text?: string
    module_set: string
    modules: any
    session_management: 'local-storage' | 'in-memory',
    default_log_level: number
}

const DARK_MODE_KEY = 'theme.dark-mode'
const COMPACT_MODE_KEY = 'theme.compact-mode'

let IS_LOGGED_IN_INTERVAL: any = null

class AppContext {

    private props: AppContextProps

    private readonly clients: IdeaClients
    private readonly localStorageService: LocalStorageService
    private readonly authContext?: IdeaAuthenticationContext
    private readonly authService: AuthService
    private readonly jobTemplatesService: JobTemplatesService
    private readonly clusterSettingsService: ClusterSettingsService

    private static onRouteCallback?: any

    private isLoggedIn: boolean = false
    private onLogin?: () => Promise<boolean>
    private onLogout?: () => Promise<boolean>

    private logger: AppLogger

    constructor(props: AppContextProps) {
        this.props = props

        const authEndpoint = `${props.httpEndpoint}${Utils.getApiContextPath(Constants.MODULE_CLUSTER_MANAGER)}`

        this.logger = new AppLogger({
            name: 'app-context.ts'
        })

        const initializeServiceWorker = () => {
            if (this.props.serviceWorkerRegistration) {
                this.props.serviceWorkerRegistration.active!.postMessage({
                    type: Constants.ServiceWorker.IDEA_AUTH_INIT,
                    options: {
                        authEndpoint: authEndpoint,
                        defaultLogLevel: props.app.default_log_level
                    }
                })
            }
        }

        if (this.props.serviceWorkerRegistration) {
            initializeServiceWorker()
        } else {
            this.authContext = new IdeaAuthenticationContext({
                sessionManagement: 'local-storage',
                authEndpoint: authEndpoint
            })
        }

        this.localStorageService = new LocalStorageService({
            prefix: 'idea'
        })

        this.clients = new IdeaClients({
            appId: 'web-portal',
            baseUrl: props.httpEndpoint,
            authContext: this.authContext,
            serviceWorkerRegistration: props.serviceWorkerRegistration
        })

        this.authService = new AuthService({
            localStorage: this.localStorageService,
            clients: this.clients
        })

        this.clusterSettingsService = new ClusterSettingsService({
            clusterSettings: this.clients.clusterSettings()
        })

        this.jobTemplatesService = new JobTemplatesService({})

        // the purpose of below interval is:
        // 1. ensure we send a periodic heart-beat to service-worker so that service worker remains active.
        //    this is only applicable when service worker is initialized. on FireFox, service worker becomes inactive
        //    after 30-seconds of no activity to service worker and session expires prematurely.
        // 2. check for login status changes and take respective actions

        if (IS_LOGGED_IN_INTERVAL != null) {
            this.logger.debug('Clearing existing IS_LOGGED_IN_INTERVAL.')
            clearInterval(IS_LOGGED_IN_INTERVAL)
        }

        IS_LOGGED_IN_INTERVAL = setInterval(() => {
            this.logger.debug('Executing IS_LOGGED_IN_INTERVAL callback.')

            // initializing service worker in this interval ensures that in the event the service worker was stopped and
            // started, the service worker knows the authentication endpoints
            this.logger.debug('Initializing service worker.')
            initializeServiceWorker()

            // check if the user is logged in. this may query the service worker or authentication context based on current session management mode.
            this.logger.debug('Checking if the user is logged in.')
            this.authService.isLoggedIn().then((status) => {
                this.logger.debug(`User logged in status: ${status}`)
                if (this.isLoggedIn && !status) {
                    this.logger.debug('User was logged in but now is not. Initiating logout process.')
                    this.authService.logout().finally(() => {
                        this.logger.debug('Logout process completed.')
                    })
                }
                this.isLoggedIn = status
            }).catch(error => {
                // Optionally handle errors
                this.logger.error('Error while checking login status:', error)
            })
        }, 10000)

        // Add another heartbeat specifically for when the tab is backgrounded
        // This uses a different technique to stay active during background state
        if (typeof window !== 'undefined') {
            // Create a dedicated worker for ping assistance if possible
            let pingWorker: any = null;

            try {
                // Create a simple worker that will help ping the service worker
                const workerCode = `
                    // Simple ping worker to keep service worker alive
                    let pingInterval = null;

                    self.onmessage = function(e) {
                        if (e.data.action === 'start') {
                            if (pingInterval) clearInterval(pingInterval);

                            // Send pings at specified interval
                            pingInterval = setInterval(() => {
                                self.postMessage('ping');
                            }, e.data.interval || 2000);
                        } else if (e.data.action === 'stop') {
                            if (pingInterval) clearInterval(pingInterval);
                            pingInterval = null;
                        }
                    };
                `;

                // Create a blob from the worker code
                const blob = new Blob([workerCode], { type: 'application/javascript' });
                const workerUrl = URL.createObjectURL(blob);

                // Create the worker
                pingWorker = new Worker(workerUrl);

                // Listen for pings from the worker
                pingWorker.onmessage = () => {
                    // Forward ping to the service worker
                    initializeServiceWorker();

                    // Also check login status occasionally
                    if (Math.random() < 0.2) { // 20% chance
                        this.authService.isLoggedIn().catch(e => {
                            this.logger.error('Background ping worker error:', e);
                        });
                    }
                };

                // Clean up
                URL.revokeObjectURL(workerUrl);
            } catch (e) {
                this.logger.warn('Failed to create ping worker', e);
            }

            // Add visibility change listener to increase heartbeat frequency when backgrounded
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'hidden') {
                    // Tab is hidden/backgrounded - increase heartbeat frequency
                    if (IS_LOGGED_IN_INTERVAL) {
                        clearInterval(IS_LOGGED_IN_INTERVAL);
                    }

                    // Start the ping worker if available
                    if (pingWorker) {
                        pingWorker.postMessage({ action: 'start', interval: 2000 });
                    }

                    // When backgrounded, ping every 2 seconds
                    IS_LOGGED_IN_INTERVAL = setInterval(() => {
                        this.logger.debug('Background heartbeat to service worker');
                        initializeServiceWorker();
                        this.authService.isLoggedIn().catch(e => {
                            this.logger.error('Background heartbeat error:', e);
                        });
                    }, 2000);
                } else if (document.visibilityState === 'visible') {
                    // Tab is visible again - restore normal heartbeat frequency
                    if (IS_LOGGED_IN_INTERVAL) {
                        clearInterval(IS_LOGGED_IN_INTERVAL);
                    }

                    // Stop the ping worker
                    if (pingWorker) {
                        pingWorker.postMessage({ action: 'stop' });
                    }

                    IS_LOGGED_IN_INTERVAL = setInterval(() => {
                        this.logger.debug('Executing IS_LOGGED_IN_INTERVAL callback.');
                        initializeServiceWorker();
                        this.authService.isLoggedIn().then((status) => {
                            this.logger.debug(`User logged in status: ${status}`);
                            if (this.isLoggedIn && !status) {
                                this.logger.debug('User was logged in but now is not. Initiating logout process.');
                                this.authService.logout();
                            }
                            this.isLoggedIn = status;
                        }).catch(error => {
                            this.logger.error('Error while checking login status:', error);
                        });
                    }, 5000);
                }
            });
        }
    }

    static setOnRoute(onRoute: any) {
        this.onRouteCallback = onRoute
    }

    static get(): AppContext {
        if (window.idea.context == null) {
            throw new IdeaException({
                errorCode: 'APP_CONTEXT_NOT_INITIALIZED',
                message: 'AppContext not initialized'
            })
        }
        return window.idea.context
    }

    setHooks(onLogin: () => Promise<boolean>, onLogout: () => Promise<boolean>) {
        this.onLogin = onLogin
        this.onLogout = onLogout
        this.authService.setHooks(onLogin, onLogout)
        this.clients.getClients().forEach(client => {
            client.setHooks(onLogin, onLogout)
        })
    }

    setDarkMode(darkMode: boolean) {
        return this.localStorage().setItem(DARK_MODE_KEY, `${darkMode}`)
    }

    isDarkMode(): boolean {
        return Utils.asBoolean(AppContext.get().localStorage().getItem(DARK_MODE_KEY), true)
    }

    setCompactMode(compactMode: boolean) {
        return this.localStorage().setItem(COMPACT_MODE_KEY, `${compactMode}`)
    }

    isCompactMode(): boolean {
        return Utils.asBoolean(AppContext.get().localStorage().getItem(COMPACT_MODE_KEY), false)
    }

    getHttpEndpoint(): string {
        return this.props.httpEndpoint
    }

    getAlbEndpoint(): string {
        return this.props.albEndpoint
    }

    releaseVersion(): string {
        return this.props.releaseVersion
    }

    getTitle(): string {
        return this.props.app.title
    }

    getSubtitle(): string {
        if (this.props.app.subtitle != null) {
            return this.props.app.subtitle
        }
        const clusterName = this.auth().getClusterName()
        if (Utils.isNotEmpty(clusterName)) {
            return `${this.auth().getClusterName()} (${this.auth().getAwsRegion()})`
        } else {
            return ''
        }
    }

    getLogoUrl(): string | undefined {
        return this.props.app.logo
    }

    getCopyRightText(): string {
        if (this.props.app.copyright_text) {
            return this.props.app.copyright_text
        } else {
            return `Copyright ${new Date().getFullYear()} Amazon.com, Inc. or its affiliates. All Rights Reserved.`
        }
    }

    uuid(): string {
        return uuid()
    }

    client(): IdeaClients {
        return this.clients
    }

    auth(): AuthService {
        return this.authService
    }

    localStorage(): LocalStorageService {
        return this.localStorageService
    }

    jobTemplates(): JobTemplatesService {
        return this.jobTemplatesService
    }

    getClusterSettingsService(): ClusterSettingsService {
        return this.clusterSettingsService
    }

    routeTo(path: string) {
        if (AppContext.onRouteCallback) {
            AppContext.onRouteCallback(path)
        }
    }

}

export default AppContext
