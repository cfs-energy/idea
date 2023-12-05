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
import IdeaForm from "../../../components/form";
import {Project, SocaUserInputChoice, VirtualDesktopBaseOS, VirtualDesktopSoftwareStack, VirtualDesktopTenancy} from "../../../client/data-model";
import {ProjectsClient} from "../../../client";
import {AppContext} from "../../../common";
import Utils from "../../../common/utils";

export interface VirtualDesktopSoftwareStackEditFormProps {
    softwareStack: VirtualDesktopSoftwareStack
    onDismiss: () => void
    onSubmit: (
        stack_id: string,
        base_os: VirtualDesktopBaseOS,
        name: string,
        description: string,
        projects: Project[],
        pool_enabled: boolean,
        pool_asg_name: string,
        launch_tenancy: VirtualDesktopTenancy
    ) => Promise<boolean>
}

export interface VirtualDesktopSoftwareStackEditFormState {
    showModal: boolean
    projectChoices: SocaUserInputChoice[]
    tenancyChoices: SocaUserInputChoice[]
}

class VirtualDesktopSoftwareStackEditForm extends Component<VirtualDesktopSoftwareStackEditFormProps, VirtualDesktopSoftwareStackEditFormState> {
    form: RefObject<IdeaForm>

    constructor(props: VirtualDesktopSoftwareStackEditFormProps) {
        super(props);
        this.form = React.createRef()
        this.state = {
            showModal: false,
            projectChoices: [],
            tenancyChoices: []
        }
    }

    hideForm() {
        this.setState({
            showModal: false
        }, () => {
            this.props.onDismiss()
        })
    }

    showModal() {
        this.setState({
            showModal: true
        }, () => {
            this.getForm().showModal()
        })
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getForm() {
        return this.form.current!
    }

    setError(errorCode: string, errorMessage: string) {
        this.getForm().setError(errorCode, errorMessage)
    }

    getCurrentProjectsChoices(): string[] {
        let choices: string[] = []
        this.props.softwareStack.projects?.forEach(project => {
            choices.push(project.project_id!)
        })
        return choices
    }



    componentDidMount() {
        this.getProjectsClient().getUserProjects({}).then(result => {
            let projectChoices: SocaUserInputChoice[] = []
            result.projects?.forEach(project => {
                projectChoices.push({
                    title: project.title,
                    value: project.project_id,
                    description: project.description
                })
            })
            this.setState({
                projectChoices: projectChoices
            }, () => {
                this.getForm()?.getFormField('projects')?.setOptions({
                    listing: this.state.projectChoices
                })
            })
        })

        this.setState({
            tenancyChoices: Utils.getTenancyChoices()
        }, () => {
            this.getForm()?.getFormField('tenancy')?.setOptions({
                listing: this.state.tenancyChoices
            })
        })
    }

    render() {
        return (
            this.state.showModal &&
            <IdeaForm
                ref={this.form}
                name={"update-software-stack"}
                modal={true}
                title={"Update Software Stack: " + this.props.softwareStack.name}
                modalSize={"medium"}
                onCancel={() => {
                    this.hideForm()
                }}
                onSubmit={() => {
                    this.getForm().clearError()
                    if (!this.getForm().validate()) {
                        return
                    }

                    if (this.props.softwareStack === undefined) {
                        return
                    }

                    const values = this.getForm().getValues()

                    let projects: Project[] = []
                    values.projects.forEach((project_id: string) => {
                        projects.push({
                            project_id: project_id
                        })
                    })

                    const stack_id = this.props.softwareStack?.stack_id!
                    const base_os = this.props.softwareStack?.base_os!
                    const name = values.name
                    const description = values.description
                    const pool_enabled = values.pool_enabled
                    const pool_asg_name = values.pool_asg_name
                    const launch_tenancy = values.launch_tenancy

                    return this.props.onSubmit(stack_id, base_os, name, description, projects, pool_enabled, pool_asg_name, launch_tenancy).then(result => {
                        this.hideForm()
                        return Promise.resolve(result)
                    }).catch(error => {
                        this.getForm().setError(error.errorCode, error.message)
                        return Promise.resolve(false)
                    })
                }}
                params={[
                    {
                        name: 'name',
                        title: 'Stack Name',
                        description: 'Enter a name for the Software Stack',
                        data_type: 'str',
                        param_type: 'text',
                        default: this.props.softwareStack?.name,
                        validate: {
                            required: true,
                            regex: '^.{3,24}$'
                        }
                    },
                    {
                        name: 'description',
                        title: 'Description',
                        description: 'Enter a user friendly description for the software stack',
                        data_type: 'str',
                        param_type: 'text',
                        default: this.props.softwareStack?.description,
                        validate: {
                            required: true
                        }
                    },
                    {
                        name: 'projects',
                        title: 'Projects',
                        description: 'Select applicable projects for the software stack',
                        data_type: 'str',
                        param_type: 'select',
                        multiple: true,
                        choices: this.state.projectChoices,
                        default: this.getCurrentProjectsChoices(),
                        validate: {
                            required: true
                        }
                    },
                    {
                        name: 'launch_tenancy',
                        title: 'Instance Launch Tenancy',
                        description: 'Instance Launch Tenancy',
                        data_type: 'str',
                        param_type: 'select',
                        choices: this.state.tenancyChoices,
                        default: this.props.softwareStack?.launch_tenancy,
                        validate: {
                            required: true
                        }
                    },
/*                    {
                        name: 'pool_enabled',
                        title: 'Warming Pool Enabled',
                        description: 'Enable Warming Pool Behavior',
                        data_type: 'bool',
                        param_type: 'checkbox',
                        default: this.props.softwareStack?.pool_enabled,
                        validate: {
                            required: true
                        }
                    },
                    {
                        name: 'pool_asg_name',
                        title: 'Warming Pool ASG Name',
                        description: 'Warming Pool ASG Name',
                        data_type: 'str',
                        param_type: 'text',
                        default: this.props.softwareStack?.pool_asg_name,
                        validate: {
                            required: true
                        },
                        when: {
                            param: 'pool_enabled',
                            eq: true
                        }
                    },*/
                ]}/>
        )
    }
}

export default VirtualDesktopSoftwareStackEditForm
