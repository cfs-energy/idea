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
import {VirtualDesktopBaseOS, VirtualDesktopSoftwareStack, Project, VirtualDesktopTenancy, SocaUserInputChoice} from '../../../client/data-model'
import IdeaForm from "../../../components/form";
import {ProjectsClient} from "../../../client";
import {AppContext} from "../../../common";
import Utils from "../../../common/utils";
import VirtualDesktopUtilsClient from "../../../client/virtual-desktop-utils-client";

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
        launch_tenancy: VirtualDesktopTenancy,
        allowed_instance_types?: string[],
        ami_id?: string,
        min_ram?: number,
        min_storage?: number
    ) => Promise<boolean>
}

export interface VirtualDesktopSoftwareStackEditFormState {
    showModal: boolean
    projectChoices: SocaUserInputChoice[]
    tenancyChoices: SocaUserInputChoice[]
    availableInstanceTypes: SocaUserInputChoice[]
}

class VirtualDesktopSoftwareStackEditForm extends Component<VirtualDesktopSoftwareStackEditFormProps, VirtualDesktopSoftwareStackEditFormState> {
    form: RefObject<IdeaForm>

    constructor(props: VirtualDesktopSoftwareStackEditFormProps) {
        super(props);
        this.form = React.createRef()
        this.state = {
            showModal: false,
            projectChoices: [],
            tenancyChoices: [],
            availableInstanceTypes: []
        }
    }
    getForm() {
        return this.form.current!
    }

    hideForm() {
        this.setState({
            showModal: false
        }, () => {
            this.props.onDismiss()
        })
    }

    showModal() {
        console.log('VirtualDesktopSoftwareStackEditForm.showModal called');
        this.setState({
            showModal: true
        }, () => {
            console.log('Form modal state updated to:', this.state.showModal);
        });
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getVirtualDesktopUtilsClient(): VirtualDesktopUtilsClient {
        return AppContext.get().client().virtualDesktopUtils()
    }

    componentDidMount() {
        console.log('VirtualDesktopSoftwareStackEditForm.componentDidMount');
        // Automatically show the modal when component mounts
        this.setState({
            showModal: true
        }, () => {
            // Call showModal on the IdeaForm ref after our own state is updated
            if (this.form.current) {
                console.log('Calling showModal on IdeaForm ref');
                this.form.current.showModal();

                // Explicitly set field values after form has mounted
                setTimeout(() => {
                    if (this.form.current) {
                        if (this.props.softwareStack?.min_ram?.value) {
                            this.form.current.getFormField('min_ram')?.setValue(this.props.softwareStack.min_ram.value);
                        }
                        if (this.props.softwareStack?.min_storage?.value) {
                            this.form.current.getFormField('min_storage')?.setValue(this.props.softwareStack.min_storage.value);
                        }
                    }
                }, 100); // Short delay to ensure form is ready
            } else {
                console.error('Form ref is not available');
            }
        });

        // Use listProjects instead of getUserProjects to show all projects to admin users
        this.getProjectsClient().listProjects({}).then(result => {
            let projectChoices: SocaUserInputChoice[] = []
            if (result.listing) {
                result.listing.forEach((project: Project) => {
                    projectChoices.push({
                        title: project.title,
                        value: project.project_id,
                        description: project.description
                    })
                })
            }
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
            this.getForm()?.getFormField('launch_tenancy')?.setOptions({
                listing: this.state.tenancyChoices
            })
        })

        // Fetch available instance types
        this.getVirtualDesktopUtilsClient().listAllowedInstanceTypes({}).then((result) => {
            const instanceTypeOptions: SocaUserInputChoice[] = [];
            if (result.listing) {
                // Simple array transformation without any complex filtering
                result.listing.forEach((instance: any) => {
                    if (instance.instance_type) {
                        instanceTypeOptions.push({
                            title: instance.instance_type,
                            value: instance.instance_type
                        });
                    }
                });
            }

            this.setState({
                availableInstanceTypes: instanceTypeOptions
            }, () => {
                this.getForm()?.getFormField('allowed_instance_types')?.setOptions({
                    listing: this.state.availableInstanceTypes
                });
            });
        }).catch((error: any) => {
            console.error("Failed to fetch instance types:", error);
        });
    }

    render() {
        // Helper function to extract project IDs
        const getProjectIds = () => {
            let projectIds: string[] = [];
            this.props.softwareStack.projects?.forEach(project => {
                if (project.project_id) {
                    projectIds.push(project.project_id);
                }
            });
            return projectIds;
        };

        console.log('Rendering VirtualDesktopSoftwareStackEditForm, showModal:', this.state.showModal);

        return (
            <IdeaForm
                ref={this.form}
                name="edit-software-stack"
                modal={true}
                modalSize="medium"
                title={`Edit Software Stack: ${this.props.softwareStack.name}`}
                values={{
                    ...this.props.softwareStack,
                    launch_tenancy: this.props.softwareStack.launch_tenancy || 'default',
                    projects: getProjectIds()
                }}
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
                    const ami_id = values.ami_id
                    const allowedInstanceTypes = values.allowed_instance_types || [];
                    const min_ram = values.min_ram;
                    const min_storage = values.min_storage;

                    return this.props.onSubmit(
                        stack_id,
                        base_os,
                        name,
                        description,
                        projects,
                        pool_enabled,
                        pool_asg_name,
                        launch_tenancy,
                        allowedInstanceTypes,
                        ami_id,
                        min_ram,
                        min_storage
                    ).then(result => {
                        this.hideForm()
                        return Promise.resolve(result)
                    }).catch(error => {
                        this.getForm().setError(error.errorCode, error.message)
                        return Promise.resolve(false)
                    })
                }}
                onFetchOptions={(request) => {
                    if (request.param === 'allowed_instance_types') {
                        // Return the available instance types directly without any filtering
                        return Promise.resolve({
                            listing: this.state.availableInstanceTypes
                        });
                    }
                    return Promise.resolve({
                        listing: []
                    });
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
                            regex: '^.{3,50}$'
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
                        name: 'ami_id',
                        title: 'AMI ID',
                        description: 'Update the AMI ID for this software stack',
                        help_text: 'AMI ID must start with ami-xxx',
                        data_type: 'str',
                        param_type: 'text',
                        default: this.props.softwareStack?.ami_id
                    },
                    {
                        name: 'min_ram',
                        title: 'Min. RAM (GB)',
                        description: 'Enter the min. RAM for your virtual desktop in GBs',
                        data_type: 'int',
                        param_type: 'text',
                        default: this.props.softwareStack?.min_ram?.value,
                        validate: {
                            required: true
                        }
                    },
                    {
                        name: 'min_storage',
                        title: 'Min. Storage Size (GB)',
                        description: 'Enter the min. storage size for your virtual desktop in GBs. This must be greater or equal to the root volume size of the AMI.',
                        data_type: 'int',
                        param_type: 'text',
                        default: this.props.softwareStack?.min_storage?.value,
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
                    {
                        name: 'allowed_instance_types',
                        title: 'Allowed Instance Families/Types',
                        description: 'Enter the instance families or specific types to allow with this software stack',
                        data_type: 'str',
                        param_type: 'text',
                        multiple: true,
                        default: this.props.softwareStack?.allowed_instance_types || [],
                        help_text: 'You can enter specific instance types (e.g., t3.xlarge) or families (e.g., g4dn, t3). Leave empty to use global settings.',
                        validate: {
                            required: false
                        },
                        dynamic_choices: true
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
    setError(errorCode: string, message: string) {
        this.getForm().setError(errorCode, message)
    }
}

export default VirtualDesktopSoftwareStackEditForm
