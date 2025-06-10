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

import React, {Component, RefObject} from "react";
import {IdeaAppLayoutProps} from "../../components/app-layout";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {Button, ColumnLayout, Container, Grid, Header, SpaceBetween} from "@cloudscape-design/components";
import IdeaAppLayout from "../../components/app-layout/app-layout";
import {KeyValue} from "../../components/key-value";
import {AppContext} from "../../common";
import {Project, VirtualDesktopBaseOS, VirtualDesktopSoftwareStack, VirtualDesktopTenancy} from "../../client/data-model";
import Tabs from "../../components/tabs/tabs";
import Utils from "../../common/utils";
import VirtualDesktopSoftwareStackEditForm from "./forms/virtual-desktop-software-stack-edit-form";
import {VirtualDesktopAdminClient} from "../../client";
import {withRouter} from "../../navigation/navigation-utils";

export interface VirtualDesktopSoftwareStackDetailProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
}

interface VirtualDesktopSoftwareStackDetailState {
    softwareStack: VirtualDesktopSoftwareStack
    showEditSoftwareStackForm: boolean
}

class VirtualDesktopSoftwareStackDetail extends Component<VirtualDesktopSoftwareStackDetailProps, VirtualDesktopSoftwareStackDetailState> {

    editStackForm: RefObject<VirtualDesktopSoftwareStackEditForm>

    constructor(props: VirtualDesktopSoftwareStackDetailProps) {
        super(props);
        this.editStackForm = React.createRef()
        this.state = {
            softwareStack: {},
            showEditSoftwareStackForm: false
        }

        // Bind methods
        this.handleEditClick = this.handleEditClick.bind(this);
    }

    getSoftwareStackId(): string {
        return this.props.params.software_stack_id
    }

    getVirtualDesktopAdminClient(): VirtualDesktopAdminClient {
        return AppContext.get().client().virtualDesktopAdmin()
    }

    componentDidMount() {
        AppContext.get().client().virtualDesktopAdmin().getSoftwareStackInfo({
            stack_id: this.getSoftwareStackId()
        }).then(result => {
            this.setState({
                softwareStack: result.software_stack!
            })
        })
    }

    buildProjectsDetails() {
        return (
            <ul>
                {
                    this.state.softwareStack.projects?.map(project => {
                        return <li key={project.name}>{project.title} | {project.name}</li>
                    })
                }
            </ul>
        )
    }

    // Handler for edit button click
    handleEditClick() {
        console.log("Edit button clicked directly through bound handler");

        // First ensure form is visible
        this.setState({
            showEditSoftwareStackForm: true
        }, () => {
            console.log("State updated, showEditSoftwareStackForm:", this.state.showEditSoftwareStackForm);

            // Then wait a tick for the form to render and get its ref
            setTimeout(() => {
                if (this.editStackForm.current) {
                    console.log("Calling showModal on the form ref");
                    this.editStackForm.current.showModal();
                } else {
                    console.error("Form ref is not available after state update and timeout");
                }
            }, 0);
        });
    }

    buildHeaderActions() {
        return (
            <Button variant={"primary"} onClick={this.handleEditClick}> Edit </Button>
        )
    }

    buildDetails() {
        return (<SpaceBetween size={"l"}>
            <Container header={<Header variant={"h2"}>General Information</Header>}>
                <ColumnLayout variant={"text-grid"} columns={3}>
                    <KeyValue title="Name" value={this.state.softwareStack.name}/>
                    <KeyValue title="AMI ID" value={this.state.softwareStack.ami_id} clipboard={true}/>
                    <KeyValue title="Base OS" value={Utils.getOsTitle(this.state.softwareStack.base_os)}/>

                    <KeyValue title="Instance Tenancy" value={this.state.softwareStack.launch_tenancy}/>
{/*                    <KeyValue title="Warming Pool" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(this.state.softwareStack.pool_enabled, false)}/>} type={"react-node"}/>*/}

                </ColumnLayout>
            </Container>
            <Tabs tabs={[
                {
                    label: 'Details',
                    id: 'details',
                    content: (
                        <Container header={<Header variant={"h2"}>Stack Details</Header>}>
                            <Grid gridDefinition={[{colspan: 12}]}>
                                <ColumnLayout columns={4} variant={"text-grid"}>
                                    <KeyValue title="Software Stack Id" value={this.state.softwareStack.stack_id} clipboard={true}/>
                                    <KeyValue title="Minimum Storage Size" value={this.state.softwareStack.min_storage} type="memory"/>
                                    <KeyValue title="Minimum RAM Size" value={this.state.softwareStack.min_ram} type="memory"/>
                                    <KeyValue title="Architecture" value={this.state.softwareStack.architecture}/>
                                    <KeyValue title="GPU" value={this.state.softwareStack.gpu?.replaceAll('_', ' ')}/>
                                    <KeyValue title="Instance Tenancy" value={this.state.softwareStack.launch_tenancy}/>
                                    <KeyValue title={"Projects"} value={this.buildProjectsDetails()} type={"react-node"}/>
                                    <KeyValue title={"Allowed Instance Types"} value={this.formatAllowedInstanceTypes()} type={"react-node"}/>
                                </ColumnLayout>
                            </Grid>
                        </Container>
                    )
                },
/*                {
                    label: 'Warming Pool',
                    id: 'pool',
                    content: (
                        <Container header={<Header variant={"h2"}>Warming Pool Details</Header>}>
                            <Grid gridDefinition={[{colspan: 8}, {colspan: 4}]}>
                                <ColumnLayout columns={2} variant={"text-grid"}>
                                    <KeyValue title="Warming Pool" value={<EnabledDisabledStatusIndicator enabled={Utils.asBoolean(this.state.softwareStack.pool_enabled, false)}/>} type={"react-node"}/>
                                    <KeyValue title="Warming Pool ASG" value={this.state.softwareStack.pool_asg_name} clipboard={true}/>
                                </ColumnLayout>
                            </Grid>
                        </Container>
                    )
                }*/
            ]}/>
        </SpaceBetween>)
    }

    formatAllowedInstanceTypes() {
        const { allowed_instance_types } = this.state.softwareStack;

        if (!allowed_instance_types || allowed_instance_types.length === 0) {
            return <span>Using cluster global instance type settings</span>;
        }

        return (
            <ul style={{ marginTop: 0 }}>
                {allowed_instance_types.map((instanceType, index) => (
                    <li key={index}>{instanceType}</li>
                ))}
            </ul>
        );
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
                sideNavActivePath={"/virtual-desktop/software-stacks"}
                onFlashbarChange={this.props.onFlashbarChange}
                flashbarItems={this.props.flashbarItems}
                breadcrumbItems={[
                    {
                        text: 'IDEA',
                        href: '#/'
                    },
                    {
                        text: 'Virtual Desktop',
                        href: '#/virtual-desktop/software-stacks'
                    },
                    {
                        text: 'Software Stacks',
                        href: '#/virtual-desktop/software-stacks'
                    },
                    {
                        text: this.getSoftwareStackId(),
                        href: ''
                    }
                ]}
                header={
                    <Header
                        variant={"h1"}
                        actions={this.buildHeaderActions()}
                    >
                        Stack: {this.state.softwareStack.name}
                    </Header>
                }
                contentType={"default"}
                content={
                    <div>
                        {this.buildDetails()}
                        {this.state.showEditSoftwareStackForm &&
                            <VirtualDesktopSoftwareStackEditForm
                                ref={this.editStackForm}
                                softwareStack={this.state.softwareStack}
                                onSubmit={(stack_id: string, base_os: VirtualDesktopBaseOS, name: string, description: string, projects: Project[], pool_enabled: boolean, pool_asg_name: string, launch_tenancy: VirtualDesktopTenancy, allowed_instance_types?: string[], ami_id?: string, min_ram?: number, min_storage?: number) => {
                                    return this.getVirtualDesktopAdminClient().updateSoftwareStack({
                                            software_stack: {
                                                stack_id: stack_id,
                                                base_os: base_os,
                                                name: name,
                                                description: description,
                                                projects: projects,
                                                pool_enabled: pool_enabled,
                                                pool_asg_name: pool_asg_name,
                                                launch_tenancy: launch_tenancy,
                                                allowed_instance_types: allowed_instance_types,
                                                ami_id: ami_id,
                                                min_ram: min_ram ? {
                                                    value: min_ram,
                                                    unit: 'gb'
                                                } : this.state.softwareStack.min_ram,
                                                min_storage: min_storage ? {
                                                    value: min_storage,
                                                    unit: 'gb'
                                                } : this.state.softwareStack.min_storage
                                            }
                                        }
                                    ).then(response => {
                                        this.setState({
                                            softwareStack: response.software_stack!,
                                            showEditSoftwareStackForm: false
                                        })
                                        return Promise.resolve(true)
                                    }).catch(error => {
                                        if (this.editStackForm.current) {
                                            this.editStackForm.current.setError(error.errorCode, error.message)
                                        }
                                        return Promise.resolve(false)
                                    })
                                }}
                                onDismiss={() => {
                                    this.setState({ showEditSoftwareStackForm: false })
                                }}
                            />
                        }
                    </div>
                }
            />
        )
    }
}

export default withRouter(VirtualDesktopSoftwareStackDetail)
