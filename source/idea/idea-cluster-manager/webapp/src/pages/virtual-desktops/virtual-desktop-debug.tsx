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

import {Alert, Box, Button, Container, Header, SpaceBetween, Table} from "@cloudscape-design/components";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import ReactJson from "@microlink/react-json-view";
import {AppContext} from "../../common";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import {faSpinner} from "@fortawesome/free-solid-svg-icons/faSpinner";
import IdeaTabs from "../../components/tabs";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";
import VirtualDesktopDCVClient from "../../client/virtual-desktop-dcv-client";
import {VirtualDesktopOrphanInstance} from "../../client/data-model";

export interface VirtualDesktopDebugProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
}

export interface VirtualDesktopDebugState {
    vdHostHealth: any
    vdSessionHealth: any
    vdHostHealthLoading: boolean
    vdSessionHealthLoading: boolean
    vdHostHealthFailed: boolean
    vdSessionHealthFailed: boolean
    vdHostHealthFirstPageOnly: boolean
    vdSessionHealthFirstPageOnly: boolean
    orphans: VirtualDesktopOrphanInstance[]
    orphansLoading: boolean
    orphansFailed: boolean
}

class VirtualDesktopDebug extends Component<VirtualDesktopDebugProps, VirtualDesktopDebugState> {

    constructor(props: VirtualDesktopDebugProps) {
        super(props);
        this.state = {
            vdHostHealthLoading: true,
            vdSessionHealthLoading: true,
            vdHostHealth: {},
            vdSessionHealth: {},
            vdHostHealthFailed: false,
            vdSessionHealthFailed: false,
            vdHostHealthFirstPageOnly: false,
            vdSessionHealthFirstPageOnly: false,
            orphans: [],
            orphansLoading: true,
            orphansFailed: false
        }
    }

    getVirtualDesktopDCVClient(): VirtualDesktopDCVClient {
        return AppContext.get().client().virtualDesktopDCV()
    }

    componentWillUnmount() {
        this.setState({
            vdSessionHealth: {},
            vdHostHealth: {}
        })
    }

    loadSessionHealth() {
        this.setState({
            vdSessionHealthLoading: true,
            vdSessionHealthFailed: false
        }, () => {
            this.getVirtualDesktopDCVClient().describeSessions({}).then(response => {
                    let health = response.response;
                    const firstPageOnly = !!health?.next_token
                    delete health?.request_id
                    delete health?.next_token
                    this.setState({
                        vdSessionHealth: health,
                        vdSessionHealthFirstPageOnly: firstPageOnly,
                        vdSessionHealthLoading: false
                    })
                }
            ).catch(() => {
                this.setState({
                    vdSessionHealthFailed: true,
                    vdSessionHealthLoading: false
                })
            })
        })
    }

    loadServerHealth() {
        this.setState({
            vdHostHealthLoading: true,
            vdHostHealthFailed: false
        }, () => {
            this.getVirtualDesktopDCVClient().describeServers({}).then(response => {
                    let health = response.response;
                    const firstPageOnly = !!health?.next_token
                    delete health?.request_id
                    delete health?.next_token
                    this.setState({
                        vdHostHealth: health,
                        vdHostHealthFirstPageOnly: firstPageOnly,
                        vdHostHealthLoading: false
                    })
                }
            ).catch(() => {
                this.setState({
                    vdHostHealthFailed: true,
                    vdHostHealthLoading: false
                })
            })
        })
    }

    loadOrphans() {
        this.setState({orphansLoading: true, orphansFailed: false}, () => {
            AppContext.get().client().virtualDesktopAdmin().listOrphanInstances().then(response => {
                this.setState({orphans: response.listing ?? [], orphansLoading: false})
            }).catch(() => {
                this.setState({orphansFailed: true, orphansLoading: false})
            })
        })
    }

    loadHealth() {
        this.loadServerHealth()
        this.loadSessionHealth()
        this.loadOrphans()
    }

    buildOrphans() {
        if (this.state.orphansFailed) {
            return <Alert type="error" action={<Button onClick={() => this.loadOrphans()}>Try again</Button>}>Could not read these instances.</Alert>
        }
        return <SpaceBetween size="s">
            <Box>Instances tagged as desktops of this cluster with no desktop in the portal, so nobody can stop them from there. They may hold data. Nothing here changes them.</Box>
            <Table
                loading={this.state.orphansLoading}
                loadingText="Reading instances"
                items={this.state.orphans}
                empty="None. Every desktop instance has a desktop."
                columnDefinitions={[
                    {id: 'instance_id', header: 'Instance', cell: row => row.instance_id},
                    {id: 'name', header: 'Name', cell: row => row.name ?? '-'},
                    {id: 'instance_type', header: 'Type', cell: row => row.instance_type ?? '-'},
                    {id: 'state', header: 'State', cell: row => row.state ?? '-'},
                    {id: 'launched_on', header: 'Launched', cell: row => row.launched_on ? new Date(row.launched_on).toLocaleDateString() : '-'}
                ]}
            />
        </SpaceBetween>
    }

    componentDidMount() {
        this.loadHealth()
    }

    buildHealth(loading: boolean, failed: boolean, firstPageOnly: boolean, health: any) {
        if (loading) {
            return <FontAwesomeIcon icon={faSpinner} size={"2x"} spin={true}/>
        }
        if (failed) {
            return <Alert type="error" action={<Button onClick={() => this.loadHealth()}>Try again</Button>}>Could not reach the broker.</Alert>
        }
        return <SpaceBetween size="s">
            {firstPageOnly && <Box>Showing the first page only.</Box>}
            <ReactJson
                theme={AppContext.get().isDarkMode() ? "monokai" : "grayscale:inverted"}
                src={health} displayDataTypes={false} iconStyle={"circle"}
                onEdit={false} name={false}/>
        </SpaceBetween>
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
                breadcrumbItems={[
                    {
                        text: 'IDEA',
                        href: '#/'
                    },
                    {
                        text: 'Virtual Desktops',
                        href: '#/virtual-desktop/debug'
                    },
                    {
                        text: 'Debug',
                        href: ''
                    }
                ]}
                header={
                    <Header variant={"h1"}
                            description={"View hosts and sessions registered with Amazon DCV Broker"}
                            actions={
                                <SpaceBetween direction="horizontal" size="xs">
                                    <Button iconName="refresh" variant="normal" onClick={() => {
                                        this.loadHealth()
                                    }}/>
                                </SpaceBetween>
                            }>
                        Diagnostics
                    </Header>}
                contentType="default"
                content={
                    <Container>
                        <IdeaTabs
                            tabs={[
                                {
                                    label: 'VD Host',
                                    id: 'vdHost',
                                    content: (
                                        <Container>
                                            {this.buildHealth(this.state.vdHostHealthLoading, this.state.vdHostHealthFailed, this.state.vdHostHealthFirstPageOnly, this.state.vdHostHealth)}
                                        </Container>
                                    )
                                },
                                {
                                    label: 'VD Sessions',
                                    id: 'vdSessions',
                                    content: (

                                        <Container>
                                            {this.buildHealth(this.state.vdSessionHealthLoading, this.state.vdSessionHealthFailed, this.state.vdSessionHealthFirstPageOnly, this.state.vdSessionHealth)}
                                        </Container>

                                    )
                                },
                                {
                                    label: 'Instances with no desktop',
                                    id: 'orphans',
                                    content: (
                                        <Container>
                                            {this.buildOrphans()}
                                        </Container>
                                    )
                                }
                            ]}
                        />
                    </Container>
                }
            />
        )
    }
}

export default withRouter(VirtualDesktopDebug)
