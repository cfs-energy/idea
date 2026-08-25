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

import {Box, Container, Header, Link, StatusIndicator} from "@cloudscape-design/components";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {AppContext} from "../../common";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";
import {parseDashboardUrl} from "../../service/cluster-settings-service";

export interface CustomDashboardProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface CustomDashboardState {

}

class CustomDashboard extends Component<CustomDashboardProps, CustomDashboardState> {

    render() {

        const clusterSettings = AppContext.get().getClusterSettingsService()
        const enabled = clusterSettings.isCustomDashboardEnabled()
        const title = clusterSettings.getCustomDashboardTitle()
        const dashboardUrl = parseDashboardUrl(clusterSettings.getCustomDashboard().url)

        const buildContent = () => {
            if (!enabled || dashboardUrl == null) {
                return (
                    <Container variant="default">
                        <Box textAlign="center" padding="xxl">
                            <StatusIndicator type="info">
                                No dashboard is configured. Set cluster-manager.web_portal.custom_dashboard.enabled
                                and cluster-manager.web_portal.custom_dashboard.url (http or https) to enable it.
                            </StatusIndicator>
                        </Box>
                    </Container>
                )
            }
            // sandbox drops form submission, popups, downloads and top-level navigation.
            // allow-same-origin lets a cross-origin dashboard use its own storage and grants nothing
            // against the portal; on the portal's own origin it would void the sandbox, so it is dropped.
            const sameOrigin = dashboardUrl.origin === window.location.origin
            return (
                <Container variant="default" disableContentPaddings={true}>
                    <iframe
                        src={dashboardUrl.href}
                        title={title}
                        sandbox={sameOrigin ? 'allow-scripts' : 'allow-scripts allow-same-origin'}
                        referrerPolicy="no-referrer"
                        style={{
                            border: 'none',
                            display: 'block',
                            width: '100%',
                            height: 'calc(100vh - 220px)',
                            minHeight: '400px'
                        }}
                    />
                </Container>
            )
        }

        // a dashboard that refuses framing renders an empty frame with no error event,
        // so the page always offers a way to open it directly.
        const buildHeader = () => {
            if (!enabled || dashboardUrl == null) {
                return <Header variant={"h1"}>{title}</Header>
            }
            return (
                <Header
                    variant={"h1"}
                    actions={<Link external={true} href={dashboardUrl.href}>Open in a new tab</Link>}>
                    {title}
                </Header>
            )
        }

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
                        text: 'Home',
                        href: '#/'
                    },
                    {
                        text: title,
                        href: ''
                    }
                ]}
                header={buildHeader()}
                contentType={"default"}
                content={buildContent()}/>
        )
    }
}

export default withRouter(CustomDashboard)
