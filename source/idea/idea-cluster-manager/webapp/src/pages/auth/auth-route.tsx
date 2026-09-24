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

class IdeaAuthenticatedRoute extends Component<IdeaAuthRouteProps> {

    render() {
        const currentUrl = new URL(window.location.href)
        const currentPath = currentUrl.hash.substring(1)
        const isAuthRoute = currentPath.startsWith('/auth/')
        if (this.props.isLoggedIn) {
            if (isAuthRoute) {
                return <Navigate to='/'/>
            } else {
                const resolved = resolveTask(this.props.location.pathname, this.props.location.search)
                const viewAccess = resolved && (Array.isArray(resolved.view.access) ? resolved.view.access : [resolved.view.access]);
                if (resolved && !(resolved.task.id === 'settings' ? permittedViews(resolved.task, AppContext.get()).length : viewAccess?.some(access => hasAccess(AppContext.get(), access)))) {
                    return <Alert type="warning" header="Destination unavailable">This destination requires module access and a deployed module.</Alert>
                }
                return this.props.children
            }
        } else {
            if (isAuthRoute) {
                return this.props.children
            } else {
                return <Navigate to='/auth/login'/>
            }
        }
    }
}

export default withRouter(IdeaAuthenticatedRoute)
