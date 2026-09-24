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

import {Component} from 'react';
import {SideNavigation, SideNavigationProps} from '@cloudscape-design/components'
import {AppContext} from "../../common";
import {permittedViews, resolveTask} from "../../navigation/task-navigation";
import Utils from "../../common/utils";
import {IdeaAppNavigationProps} from "../../navigation/navigation-utils";


export interface IdeaSideNavigationProps extends IdeaAppNavigationProps {
    sideNavHeader: SideNavigationProps.Header
    sideNavItems: SideNavigationProps.Item[]
    onSideNavChange: NonNullable<SideNavigationProps['onChange']>
    activePath?: string
}

class IdeaSideNavigation extends Component<IdeaSideNavigationProps> {

    onFollowHandler(event: CustomEvent<SideNavigationProps.FollowDetail>) {
        event.preventDefault();
        if (event.detail.href) {
            this.props.navigate(event.detail.href.substring(1));
        }
    }

    getActivePath(): string {
        const resolved = resolveTask(this.props.location.pathname, this.props.location.search);
        const first = resolved && permittedViews(resolved.task, AppContext.get())[0];
        if (first) return `#${first.path}`;
        if(Utils.isNotEmpty(this.props.activePath)) {
            return `#${this.props.activePath}`
        } else {
            return `#${this.props.location.pathname}`
        }
    }

    render() {
        return (
            <SideNavigation
                className="idea-side-nav"
                header={this.props.sideNavHeader}
                items={this.props.sideNavItems}
                activeHref={this.getActivePath()}
                onFollow={this.onFollowHandler.bind(this)}
                onChange={(event) => {
                    if(this.props.onSideNavChange) {
                        this.props.onSideNavChange(event)
                    }
                }}
            />
        );
    }
}

export default IdeaSideNavigation
