import {Alert} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {hasAccess, permittedViews, resolveTask} from '../../navigation/task-navigation';
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

import React, {Component} from "react";
import {Navigate} from "react-router-dom";
import {IdeaAppNavigationProps} from "../../navigation/navigation-utils";
import {withRouter} from "../../navigation/navigation-utils";

export interface IdeaAuthRouteProps extends IdeaAppNavigationProps {
    isLoggedIn: boolean
    children: React.ReactNode
}

class IdeaAuthenticatedRoute extends Component<IdeaAuthRouteProps, {sessionExpired: boolean}> {
    state = {sessionExpired: false}
    private authorizedPath?: string
    private mounted = false
    private checking = false
    private retryTimer?: ReturnType<typeof setTimeout>
    private retryAttempt = 0

    componentDidMount() {
        this.mounted = true
        document.addEventListener('visibilitychange', this.onVisibilityChange)
        window.addEventListener('online', this.retryAccess)
        this.checkAccess()
    }

    componentDidUpdate() {
        if (this.state.sessionExpired && AppContext.get().auth().isAccessLoaded()) {
            this.setState({sessionExpired: false})
        } else {
            this.checkAccess()
        }
    }

    componentWillUnmount() {
        this.mounted = false
        clearTimeout(this.retryTimer)
        document.removeEventListener('visibilitychange', this.onVisibilityChange)
        window.removeEventListener('online', this.retryAccess)
    }

    private onVisibilityChange = () => {
        if (document.visibilityState === 'visible') this.retryAccess()
    }

    private retryAccess = () => {
        clearTimeout(this.retryTimer)
        this.retryTimer = undefined
        this.checkAccess()
    }

    private checkAccess = () => {
        const auth = AppContext.get().auth()
        if (!this.mounted || !this.props.isLoggedIn || this.state.sessionExpired || auth.isAccessLoaded() || this.checking || this.retryTimer != null) return
        this.checking = true
        auth.isLoggedIn().then(loggedIn => {
            this.checking = false
            if (!this.mounted) return
            this.retryAttempt = 0
            this.setState({sessionExpired: !loggedIn})
        }).catch(() => {
            this.checking = false
            if (!this.mounted) return
            const delay = [2000, 5000, 10000, 30000][Math.min(this.retryAttempt++, 3)]
            this.retryTimer = setTimeout(this.retryAccess, delay)
            this.forceUpdate()
        })
    }


    render() {
        const isAuthRoute = this.props.location.pathname.startsWith('/auth/')
        if (this.props.isLoggedIn && !this.state.sessionExpired) {
            // A failed renewal check can clear claims while App still considers us logged in.
            // Wait for the recovery check even on auth routes, so an expired session stays at login.
            const loading = !AppContext.get().auth().isAccessLoaded()
            const path = this.props.location.pathname + this.props.location.search
            const spinner = <div className="spinner-container" data-testid="access-loading"><div className="spinner"><div/><div/></div></div>
            if (loading && this.authorizedPath !== path) return spinner
            if (isAuthRoute) {
                return <Navigate to='/'/>
            } else {
                const resolved = resolveTask(this.props.location.pathname, this.props.location.search)
                const viewAccess = resolved && (Array.isArray(resolved.view.access) ? resolved.view.access : [resolved.view.access]);
                if (!loading && resolved && !(resolved.task.id === 'settings' ? permittedViews(resolved.task, AppContext.get()).length : viewAccess?.some(access => hasAccess(AppContext.get(), access)))) {
                    this.authorizedPath = undefined
                    return <Alert type="warning" header="Destination unavailable">This destination requires module access and a deployed module.</Alert>
                }
                if (!loading) this.authorizedPath = path
                // Keep the same tree during retries so form state and uploads survive.
                return <div style={{position: 'relative'}} aria-busy={loading}>
                    <div inert={loading ? true : undefined}>{this.props.children}</div>
                    {loading && <div role="status" aria-label="Reloading session" style={{position: 'absolute', inset: 0, zIndex: 1, display: 'grid', placeItems: 'center', background: 'rgba(255, 255, 255, 0.6)'}}>{spinner}</div>}
                </div>
            }
        } else {
            this.authorizedPath = undefined
            if (isAuthRoute) {
                return this.props.children
            } else {
                return <Navigate to='/auth/login'/>
            }
        }
    }
}

export default withRouter(IdeaAuthenticatedRoute)
