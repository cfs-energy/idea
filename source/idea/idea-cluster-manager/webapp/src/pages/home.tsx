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

import {
    ColumnLayout,
    Header,
    Box,
    Link,
    Grid,
    Container,
    SpaceBetween,
    Icon
} from "@cloudscape-design/components";

import '../styles/home.scss'
import {IdeaSideNavigationProps} from "../components/side-navigation";
import {OnToolsChangeEvent} from "../App";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../components/app-layout";
import {withRouter} from "../navigation/navigation-utils";

export interface CounterProps {
    content?: string
}

export const externalLinkProps = {
    external: true,
    externalIconAriaLabel: 'Opens in a new tab'
}

export function Counter(props: CounterProps) {
    return (
        <Box variant="div" fontSize="display-l" fontWeight="normal">
            {props.content}
        </Box>
    )
}

export interface HomePageProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
    toolsOpen: boolean
    tools: React.ReactNode
    onToolsChange: (event: OnToolsChangeEvent) => void
}

export interface HomePageState {
    selectedOption: string
}

class Home extends Component<HomePageProps, HomePageState> {

    constructor(props: HomePageProps) {
        super(props)
        this.state = {
            selectedOption: ''
        }
    }

    content() {
        return <Grid
            gridDefinition={[
                {colspan: {xl: 6, l: 5, s: 6, xxs: 10}, offset: {l: 2, xxs: 1}},
                {colspan: {xl: 2, l: 3, s: 4, xxs: 10}, offset: {s: 0, xxs: 1}}
            ]}
        >
            <SpaceBetween size="l">
                <Container>
                        <SpaceBetween size="l">
                            <Box variant="h1" tagOverride="h2">
                                Benefits and features
                            </Box>
                            <ColumnLayout columns={2} variant="text-grid">
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        Detailed Cluster Analytics
                                    </Box>
                                    <Box variant="p">
                                        Integrated Digital Engineering on AWS includes OpenSearch (formerly Elasticsearch) and automatically
                                        ingest job and hosts data in real-time for accurate visualization of your cluster activity.
                                    </Box>
                                </div>
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        100% Customizable
                                    </Box>
                                    <Box variant="p">
                                        Integrated Digital Engineering on AWS is built entirely on top of AWS and can be customized by users as
                                        needed.
                                        More importantly, the entire Integrated Digital Engineering on AWS codebase is open-source and
                                        available on Github.
                                    </Box>
                                </div>
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        Centralized user-management
                                    </Box>
                                    <Box variant="p">
                                        Customers can create unlimited LDAP users and groups.
                                        By default Integrated Digital Engineering on AWS includes a default LDAP account provisioned during
                                        installation as well as a "Sudoers" LDAP group which manage SUDO permission on the cluster.
                                    </Box>
                                </div>
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        Support for network licenses
                                    </Box>
                                    <Box variant="p">
                                        Integrated Digital Engineering on AWS includes a FlexLM-enabled script which calculate the number of
                                        licenses for a given features and only start the job/provision the capacity when enough
                                        licenses are available.
                                    </Box>
                                </div>
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        Automatic backup
                                    </Box>
                                    <Box variant="p">
                                        Integrated Digital Engineering on AWS automatically backup your data with no additional effort required
                                        on your side.
                                    </Box>
                                </div>
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        Budgets and Cost Management
                                    </Box>
                                    <Box variant="p">
                                        Create, monitor, and easily manage your HPC Workload Costs all in once place.
                                    </Box>
                                </div>
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        Web User Interface
                                    </Box>
                                    <Box variant="p">
                                        Integrated Digital Engineering on AWS includes a simple web ui designed to simplify user interactions
                                    </Box>
                                </div>
                                <div>
                                    <Box variant="h3" padding={{top: 'n'}}>
                                        HTTP Rest API
                                    </Box>
                                    <Box variant="p">
                                        Users can submit/retrieve/delete jobs remotely via an HTTP REST API
                                    </Box>
                                </div>
                            </ColumnLayout>
                        </SpaceBetween>
                </Container>
                <Container>
                    <SpaceBetween size="l">
                        <Box variant="h1" tagOverride="h2">
                            Use cases
                        </Box>
                        <ColumnLayout columns={2} variant="text-grid">
                            <div>
                                <Box variant="h3" padding={{top: 'n'}}>
                                    Remote Desktop Sessions
                                </Box>
                                <Box variant="p">
                                    With IDEA you get a fully operational Virtual Desktop management solution, integrated into
                                    your existing Active Directory.
                                    Enable your employees with the latest and greatest EC2 instances to use the full power
                                    of AWS.
                                </Box>
                                <Link href="https://docs.idea-hpc.com/modules/virtual-desktop-interfaces/user-documentation" {...externalLinkProps}>
                                    Learn more
                                </Link>
                            </div>
                            <div>
                                <Box variant="h3" padding={{top: 'n'}}>
                                    HPC Workload Manager
                                </Box>
                                <Box variant="p">
                                    Run a powerful workload manager of your choice to run your simulation workloads at scale
                                    with the scalability and flexibility of Amazon AWS.
                                </Box>
                                <Link href="https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation" {...externalLinkProps}>
                                    Learn more
                                </Link>
                            </div>
                        </ColumnLayout>
                    </SpaceBetween>
                </Container>
            </SpaceBetween>

            <div className="custom-home__sidebar">
                <SpaceBetween size="xxl">
                    <Container
                        header={
                            <Header variant="h2">
                                Getting started{' '}
                                <span role="img" aria-label="Icon external Link">
                      <Icon name="external" size="inherit"/>
                    </span>
                            </Header>
                        }
                    >
                        <ul aria-label="Getting started documentation" className="custom-list-separator">
                            <li>
                                <Link
                                    href="https://docs.idea-hpc.com/"
                                    external={true}
                                >What is Integrated Digital Engineering on AWS?</Link>
                            </li>
                            <li>
                                <Link
                                    href="https://docs.idea-hpc.com/first-time-users/lets-get-started"
                                >Getting started with IDEA</Link>
                            </li>
                            <li>
                                <Link
                                    href="https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation"
                                >Submit your first HPC Job</Link>
                            </li>
                        </ul>
                    </Container>

                    <Container
                        header={
                            <Header variant="h2">
                                More resources{' '}
                                <span role="img" aria-label="Icon external Link">
                      <Icon name="external" size="inherit"/>
                    </span>
                            </Header>
                        }
                    >
                        <ul aria-label="Additional resource links" className="custom-list-separator">
                            <li>
                                <Link href="https://docs.idea-hpc.com/">Documentation</Link>
                            </li>
                            <li>
                                <Link href="https://docs.idea-hpc.com/help-and-support/faq">FAQ</Link>
                            </li>
                            <li>
                                <Link href="https://github.com/cfs-energy/idea/issues">Report an Issue</Link>
                            </li>
                        </ul>
                    </Container>
                </SpaceBetween>
            </div>
        </Grid>
    }

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
                header={<Header variant={"h1"}>
                    <div className="custom-home__header">
                        <Box padding={{vertical: 'xxl', horizontal: 's'}}>
                            <Grid
                                gridDefinition={[
                                    {colspan: {xxs: 12}},
                                    {colspan: {xxs: 12}}
                                ]}
                            >
                                <Box fontWeight="light" padding={{top: 'xs'}}>
                                    <span className="custom-home__category">Industry Solutions for CAE/CAD Simulations and HPC Workloads</span>
                                </Box>
                                <div className="custom-home__header-title">
                                    <Box variant="h1" fontWeight="bold" padding="n" fontSize="display-l" color="inherit">
                                        Integrated Digital Engineering on AWS
                                    </Box>
                                    <Box fontWeight="light" padding={{bottom: 's'}} fontSize="display-l" color="inherit">
                                        Deploy and operate computationally intensive workloads.
                                    </Box>
                                    <Box variant="p" fontWeight="normal">
                <span className="custom-home__header-sub-title">
                  The solution features a large selection of compute resources; fast network backbone; unlimited storage; and budget and cost management directly integrated within AWS.
                </span>
                                    </Box>
                                </div>
                            </Grid>
                        </Box>
                    </div>
                </Header>}
                contentType={"default"}
                content={this.content()}
            />
        )
    }
}

export default withRouter(Home)
