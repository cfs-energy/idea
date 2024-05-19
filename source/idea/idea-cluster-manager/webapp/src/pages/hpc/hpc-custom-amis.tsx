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

import {Component} from "react";

import {PathRouteProps} from "react-router-dom";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";

export interface HpcCustomAmisProps extends PathRouteProps, IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface HpcCustomAmisState {

}

class HpcCustomAmis extends Component<HpcCustomAmisProps, HpcCustomAmisState> {

    render() {
        return (
            <IdeaAppLayout
                ideaPageId={this.props.ideaPageId}
                toolsOpen={this.props.toolsOpen}
                tools={this.props.tools}
                onToolsChange={this.props.onToolsChange}
                onPageChange={this.props.onPageChange}
                sideNavHeader={this.props.sideNavHeader}
                sideNavItems={this.props.sideNavItems}
                onSideNavChange={this.props.onSideNavChange}
                onFlashbarChange={this.props.onFlashbarChange}
                flashbarItems={this.props.flashbarItems}
                breadcrumbItems={[
                    {
                        text: 'IDEA',
                        href: '#/'
                    },
                    {
                        text: 'Scale-Out Computing',
                        href: '#/soca/active-jobs'
                    },
                    {
                        text: 'Custom AMIs',
                        href: ''
                    }
                ]}
                content={
                    <div><h2>Custom AMIs</h2></div>
                }/>
        )
    }
}

export default HpcCustomAmis
