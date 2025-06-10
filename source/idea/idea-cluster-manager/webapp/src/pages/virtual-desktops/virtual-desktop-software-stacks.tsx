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

import IdeaListView from "../../components/list-view";
import {ProjectsClient, VirtualDesktopAdminClient} from '../../client'
import {AppContext} from "../../common";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import IdeaForm from "../../components/form";
import {Project, SocaUserInputChoice, VirtualDesktopBaseOS, VirtualDesktopSoftwareStack, VirtualDesktopTenancy} from '../../client/data-model'
import Utils from "../../common/utils";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {Link, StatusIndicator} from "@cloudscape-design/components";
import VirtualDesktopSoftwareStackEditForm from "./forms/virtual-desktop-software-stack-edit-form";
import {withRouter} from "../../navigation/navigation-utils";
import VirtualDesktopUtilsClient from "../../client/virtual-desktop-utils-client";
import IdeaConfirm from "../../components/modals";

export interface VirtualDesktopSoftwareStacksProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface SoftwareStackConfirmModalActionProps {
    actionTitle: string
    onConfirm: () => void
    actionText: string | React.ReactNode
    onCancel: () => void
}

export interface VirtualDesktopSoftwareStacksState {
    softwareStackSelected: boolean
    supportedOsChoices: SocaUserInputChoice[]
    supportedGPUChoices: SocaUserInputChoice[]
    projectChoices: SocaUserInputChoice[]
    tenancyChoices: SocaUserInputChoice[]
    showCreateSoftwareStackForm: boolean
    showEditSoftwareStackForm: boolean
    availableInstanceTypes: SocaUserInputChoice[]
    confirmAction: SoftwareStackConfirmModalActionProps
    selectedSoftwareStack: VirtualDesktopSoftwareStack | undefined
    clonedSoftwareStack: VirtualDesktopSoftwareStack | undefined
}

const VIRTUAL_DESKTOP_SOFTWARE_STACKS_TABLE_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<VirtualDesktopSoftwareStack>[] = [
    {
        id: 'name',
        header: 'Name',
        cell: e => <Link href={`/#/virtual-desktop/software-stacks/${e.stack_id}`}>{e.name}</Link>,
        sortingField: 'name'
    },
    {
        id: 'enabled',
        header: 'Enabled',
        cell: e => {
            return (e.enabled) ? <StatusIndicator type="success">Enabled</StatusIndicator> :
                <StatusIndicator type="stopped">Disabled</StatusIndicator>
        },
        sortingComparator: (a, b) => {
            const valueA = a.enabled ? 1 : 0;
            const valueB = b.enabled ? 1 : 0;
            return valueA - valueB;
        }
    },
    {
        id: 'description',
        header: 'Description',
        cell: e => e.description,
        sortingField: 'description'
    },
    {
        id: 'ami_id',
        header: 'AMI ID',
        cell: e => e.ami_id,
        sortingField: 'ami_id'
    },
    {
        id: 'os',
        header: 'Base OS',
        cell: e => Utils.getOsTitle(e.base_os),
        sortingComparator: (a, b) => (a.base_os || '').localeCompare(b.base_os || '')
    },
    {
        id: 'root_volume_size',
        header: 'Root Volume Size',
        cell: e => Utils.getFormattedMemory(e.min_storage),
        sortingComparator: (a, b) => {
            const valueA = a.min_storage?.value || 0;
            const valueB = b.min_storage?.value || 0;
            return valueA - valueB;
        }
    },
    {
        id: 'min_ram',
        header: 'Min RAM',
        cell: e => Utils.getFormattedMemory(e.min_ram),
        sortingComparator: (a, b) => {
            const valueA = a.min_ram?.value || 0;
            const valueB = b.min_ram?.value || 0;
            return valueA - valueB;
        }
    },
    {
        id: 'launch_tenancy',
        header: 'Launch Tenancy',
        cell: e => Utils.getFormattedTenancy(e.launch_tenancy),
        sortingComparator: (a, b) => (a.launch_tenancy || '').localeCompare(b.launch_tenancy || '')
    },
/*    {
        id: 'pool_enabled',
        header: 'Pool Enabled',
        cell: e => {
            return (e.pool_enabled) ? <StatusIndicator type="success">Enabled</StatusIndicator> :
                <StatusIndicator type="stopped">Disabled</StatusIndicator>
        }
    },*/
    {
        id: 'created_on',
        header: 'Created On',
        cell: e => new Date(e.created_on!).toLocaleString(),
        sortingField: 'created_on',
        sortingComparator: (a, b) => {
            const dateA = a.created_on ? new Date(a.created_on).getTime() : 0;
            const dateB = b.created_on ? new Date(b.created_on).getTime() : 0;
            return dateA - dateB;
        }
    }
]

class VirtualDesktopSoftwareStacks extends Component<VirtualDesktopSoftwareStacksProps, VirtualDesktopSoftwareStacksState> {

    listing: RefObject<IdeaListView>
    createSoftwareStackForm: RefObject<IdeaForm>
    editSoftwareStackForm: RefObject<VirtualDesktopSoftwareStackEditForm>
    softwareStackActionConfirmModal: RefObject<IdeaConfirm>

    constructor(props: VirtualDesktopSoftwareStacksProps) {
        super(props);
        this.listing = React.createRef()
        this.createSoftwareStackForm = React.createRef()
        this.editSoftwareStackForm = React.createRef()
        this.softwareStackActionConfirmModal = React.createRef()
        this.state = {
            softwareStackSelected: false,
            supportedOsChoices: [],
            supportedGPUChoices: [],
            projectChoices: [],
            tenancyChoices: [],
            showCreateSoftwareStackForm: false,
            showEditSoftwareStackForm: false,
            availableInstanceTypes: [],
            selectedSoftwareStack: undefined,
            clonedSoftwareStack: undefined,
            confirmAction: {
                actionTitle: '',
                actionText: '',
                onConfirm: () => {},
                onCancel: () => {}
            }
        }
    }

    componentDidMount() {
        this.getVirtualDesktopUtilsClient().listSupportedOS({}).then(result => {
            this.setState({
                supportedOsChoices: Utils.getSupportedOSChoices(result.listing!)
            })
        })
        this.setState({
            tenancyChoices: Utils.getTenancyChoices()
        })

        this.getVirtualDesktopUtilsClient().listSupportedGPUs({}).then(result => {
            this.setState({
                supportedGPUChoices: Utils.getSupportedGPUChoices(result.listing!)
            })
        })

        this.getProjectsClient().listProjects({}).then(result => {
            let projectChoices: SocaUserInputChoice[] = []
            result.listing?.forEach(project => {
                projectChoices.push({
                    title: project.title,
                    value: project.project_id,
                    description: project.description
                })
            })
            this.setState({
                projectChoices: projectChoices
            }, () => {
                this.getCreateSoftwareStackForm()?.getFormField('projects')?.setOptions({
                    listing: this.state.projectChoices
                })
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
            });
        }).catch((error: any) => {
            console.error("Failed to fetch instance types:", error);
        });
    }

    getListing(): IdeaListView {
        return this.listing.current!
    }

    isSelected(): boolean {
        return this.state.softwareStackSelected
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getVirtualDesktopAdminClient(): VirtualDesktopAdminClient {
        return AppContext.get().client().virtualDesktopAdmin()
    }

    getVirtualDesktopUtilsClient(): VirtualDesktopUtilsClient {
        return AppContext.get().client().virtualDesktopUtils()
    }

    getCreateSoftwareStackForm(): IdeaForm {
        return this.createSoftwareStackForm.current!
    }

    showCreateSoftwareStackForm() {
        this.setState({
            showCreateSoftwareStackForm: true
        }, () => {
            this.getCreateSoftwareStackForm().showModal()
        })
    }

    hideCreateSoftwareStackForm() {
        this.setState({
            showCreateSoftwareStackForm: false,
            clonedSoftwareStack: undefined
        })
    }

    cloneSoftwareStack = () => {
        const selectedStack = this.getSelectedSoftwareStack();
        if (!selectedStack) {
            return;
        }

        // Create a cloned version of the stack with a new name
        this.setState({
            clonedSoftwareStack: selectedStack
        }, () => {
            this.showCreateSoftwareStackForm();

            // Use setTimeout to ensure the form is rendered before we set values
            setTimeout(() => {
                const form = this.getCreateSoftwareStackForm();
                if (form && selectedStack) {
                    // Set default values for the create form from the selected stack
                    form.getFormField('name')?.setValue(`${selectedStack.name} (Clone)`);
                    form.getFormField('description')?.setValue(selectedStack.description);
                    form.getFormField('ami_id')?.setValue(selectedStack.ami_id);
                    form.getFormField('base_os')?.setValue(selectedStack.base_os);
                    form.getFormField('root_storage_size')?.setValue(selectedStack.min_storage?.value);
                    form.getFormField('ram_size')?.setValue(selectedStack.min_ram?.value);
                    form.getFormField('projects')?.setValue(selectedStack.projects?.map(p => p.project_id) || []);
                    form.getFormField('launch_tenancy')?.setValue(selectedStack.launch_tenancy);
                    form.getFormField('allowed_instance_types')?.setValue(selectedStack.allowed_instance_types || []);
                }
            }, 100);
        });
    }

    buildCreateSoftwareStackForm() {
        return <IdeaForm
            ref={this.createSoftwareStackForm}
            name="create-software-stack"
            modal={true}
            title={this.state.clonedSoftwareStack ? "Clone Software Stack" : "Register new Software Stack"}
            modalSize="medium"
            onSubmit={() => {
                this.getCreateSoftwareStackForm().clearError()
                if (!this.getCreateSoftwareStackForm().validate()) {
                    return
                }
                const values = this.getCreateSoftwareStackForm().getValues()
                let projects: Project[] = []
                values.projects.forEach((project_id: string) => {
                    projects.push({
                        project_id: project_id
                    })
                })

                const allowedInstanceTypes = values.allowed_instance_types || [];

                this.getVirtualDesktopAdminClient().createSoftwareStack({
                        software_stack: {
                            name: values.name,
                            description: values.description,
                            ami_id: values.ami_id.toLowerCase().trim(),
                            base_os: values.base_os,
                            gpu: 'NO_GPU',
                            min_storage: {
                                value: values.root_storage_size,
                                unit: 'gb'
                            },
                            min_ram: {
                                value: values.ram_size,
                                unit: 'gb'
                            },
                            projects: projects,
                            pool_enabled: values.pool_enabled,
                            pool_asg_name: values.pool_asg_name,
                            launch_tenancy: values.launch_tenancy,
                            allowed_instance_types: allowedInstanceTypes
                        }
                    }
                ).then(() => {
                    this.hideCreateSoftwareStackForm()
                    this.setFlashMessage(
                        <>Software Stack: {values.name}, Create request submitted</>, "success"
                    )
                    // Reset selection and refresh the listing after a short delay
                    this.setState({
                        softwareStackSelected: false,
                        clonedSoftwareStack: undefined
                    }, () => {
                        setTimeout(() => {
                            this.getListing().fetchRecords();
                        }, 3000); // Wait 3 seconds before refreshing
                    });
                }).catch(error => {
                    this.getCreateSoftwareStackForm().setError(error.errorCode, error.message)
                })
            }}
            onCancel={() => {
                this.hideCreateSoftwareStackForm()
            }}
            onFetchOptions={(request) => {
                // Nothing needed for allowed_instance_types now that it's a text field
                return Promise.resolve({
                    listing: []
                });
            }}
            params={[
                {
                    name: 'name',
                    title: 'Name',
                    description: 'Enter a name for the software stack',
                    data_type: 'str',
                    param_type: 'text',
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
                    validate: {
                        required: true
                    }
                },
                {
                    name: 'ami_id',
                    title: 'AMI ID',
                    description: 'Enter the AMI ID',
                    help_text: 'AMI ID must start with ami-xxx',
                    data_type: 'str',
                    param_type: 'text',
                    validate: {
                        required: true
                    }
                },
                {
                    name: 'base_os',
                    title: 'Operating System',
                    description: 'Select the operating system for the software stack',
                    data_type: 'str',
                    param_type: 'select',
                    validate: {
                        required: true
                    },
                    default: 'amazonlinux2023',
                    choices: this.state.supportedOsChoices
                },
                {
                    name: 'root_storage_size',
                    title: 'Min. Storage Size (GB)',
                    description: 'Enter the min. storage size for your virtual desktop in GBs',
                    data_type: 'int',
                    param_type: 'text',
                    default: 10,
                    validate: {
                        required: true
                    }
                },
                {
                    name: 'ram_size',
                    title: 'Min. RAM (GB)',
                    description: 'Enter the min. ram for your virtual desktop in GBs',
                    data_type: 'int',
                    param_type: 'text',
                    default: 4,
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
                    title: 'Launch Tenancy',
                    description: 'Select Launch Tenancy',
                    data_type: 'str',
                    param_type: 'select',
                    multiple: false,
                    choices: this.state.tenancyChoices,
                    default: 'default',
                    validate: {
                        required: true
                    }
                },
/*                {
                    name: 'pool_enabled',
                    title: 'Pool Enabled',
                    description: 'Select to activate warming pool',
                    data_type: 'boolean',
                    param_type: 'confirm',
                    default: false,
                    validate: {
                        required: true
                    }
                },
                {
                    name: 'pool_asg_name',
                    title: 'Pool ASG',
                    description: 'Pool ASG',
                    data_type: 'str',
                    param_type: 'text',
                    validate: {
                        // TODO Regex
                        required: false
                    },
                    when: {
                        param: 'pool_enabled',
                        eq: true
                    }
                }*/
                {
                    name: 'allowed_instance_types',
                    title: 'Allowed Instance Families/Types',
                    description: 'Enter the instance families or specific types to allow with this software stack',
                    data_type: 'str',
                    param_type: 'text',
                    multiple: true,
                    help_text: 'You can enter specific instance types (e.g., t3.xlarge) or families (e.g., g4dn, t3). Leave empty to use global settings.',
                    validate: {
                        required: false
                    }
                }
            ]}
        />
    }

    hideEditSoftwareStackForm() {
        this.setState({
            showEditSoftwareStackForm: false
        })
    }

    getActionConfirmModal(): IdeaConfirm {
        return this.softwareStackActionConfirmModal.current!
    }

    getSelectedSoftwareStacks(): VirtualDesktopSoftwareStack[] {
        if (this.getListing() == null) {
            return []
        }
        return this.getListing().getSelectedItems() as VirtualDesktopSoftwareStack[]
    }

    getSelectedSoftwareStack(): VirtualDesktopSoftwareStack | undefined {
        const selectedStacks = this.getSelectedSoftwareStacks();
        return selectedStacks.length > 0 ? selectedStacks[0] : undefined
    }

    deleteSoftwareStack = () => {
        const selectedSoftwareStacks = this.getSelectedSoftwareStacks();
        if (selectedSoftwareStacks.length === 0) {
            return;
        }

        const plural = selectedSoftwareStacks.length > 1;
        const stackNames = selectedSoftwareStacks.map(stack => stack.name).join(", ");

        this.setState({
            selectedSoftwareStack: selectedSoftwareStacks.length === 1 ? selectedSoftwareStacks[0] : undefined,
            confirmAction: {
                actionTitle: `Delete Software Stack${plural ? 's' : ''}`,
                actionText: (
                    <div>
                        Are you sure you want to delete the following software stack{plural ? 's' : ''}: <strong>{stackNames}</strong>?
                        <br /><br />
                        This will <strong>permanently delete</strong> the software stack{plural ? 's' : ''} and {plural ? 'they' : 'it'} will no longer be available for launching virtual desktops.
                    </div>
                ),
                onConfirm: () => {
                    // Create an array of software stacks with just the necessary fields for deletion
                    const stacksToDelete = selectedSoftwareStacks.map(stack => ({
                        stack_id: stack.stack_id,
                        base_os: stack.base_os
                    }));

                    let requestPayload;
                    if (stacksToDelete.length === 1) {
                        // For a single stack, use software_stack to increase compatibility
                        requestPayload = {
                            software_stack: stacksToDelete[0]
                        };
                    } else {
                        // For multiple stacks, use software_stacks array
                        requestPayload = {
                            software_stacks: stacksToDelete
                        };
                    }

                    this.getVirtualDesktopAdminClient().deleteSoftwareStack(requestPayload)
                        .then(response => {
                            // Determine success count from the response
                            let successCount = 0;
                            if (response.success_list && response.success_list.length > 0) {
                                successCount = response.success_list.length;
                            } else if (response.software_stacks && response.software_stacks.length > 0) {
                                successCount = response.software_stacks.length;
                            } else if (response.software_stack) {
                                successCount = 1;
                            } else if (response.success) {
                                // If we know it was successful but don't have details, use original selection count
                                successCount = stacksToDelete.length;
                            }

                            // Determine failure count
                            const failedCount = response.failed_list ? response.failed_list.length : 0;

                            if (response.success) {
                                if (failedCount === 0) {
                                    // All stacks deleted successfully
                                    this.setFlashMessage(
                                        <>Successfully deleted {successCount} software stack{successCount !== 1 ? 's' : ''}</>,
                                        'success'
                                    );
                                } else if (successCount === 0) {
                                    // All stacks failed to delete
                                    this.setFlashMessage(
                                        <>Failed to delete any software stacks.</>,
                                        'error'
                                    );
                                } else {
                                    // Some stacks deleted, some failed
                                    this.setFlashMessage(
                                        <>Deleted {successCount} stack{successCount !== 1 ? 's' : ''}, but failed to delete {failedCount} stack{failedCount !== 1 ? 's' : ''}</>,
                                        'warning'
                                    );
                                }

                                // Reset selection and refresh the listing after a short delay
                                this.setState({
                                    softwareStackSelected: false
                                }, () => {
                                    setTimeout(() => {
                                        this.getListing().fetchRecords();
                                    }, 3000); // Wait 3 seconds before refreshing
                                });
                            } else {
                                this.setFlashMessage(
                                    <>Failed to delete software stack{plural ? 's' : ''}: {response.software_stack?.failure_reason || 'Unknown error'}</>,
                                    'error'
                                );
                            }
                        })
                        .catch(error => {
                            this.setFlashMessage(
                                <>Error deleting software stack{plural ? 's' : ''}: {error.message}</>,
                                'error'
                            );
                        });
                },
                onCancel: () => {
                    this.setState({
                        selectedSoftwareStack: undefined
                    });
                }
            }
        }, () => {
            this.getActionConfirmModal().show();
        });
    }

    setFlashMessage = (content: React.ReactNode, type: 'success' | 'info' | 'error' | 'warning') => {
        this.props.onFlashbarChange({
            items: [
                {
                    content: content,
                    dismissible: true,
                    type: type
                }
            ]
        })
    }

    showEditSoftwareStackForm() {
        this.setState({
            showEditSoftwareStackForm: true
        }, () => {
            this.getEditSoftwareStackForm().showModal()
        })
    }

    getEditSoftwareStackForm() {
        return this.editSoftwareStackForm.current!
    }

    buildEditSoftwareStackForm() {
        return (
            <VirtualDesktopSoftwareStackEditForm
                ref={this.editSoftwareStackForm}
                softwareStack={this.getSelectedSoftwareStack()!}
                onSubmit={
                (
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
                ) => {
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
                                } : undefined,
                                min_storage: min_storage ? {
                                    value: min_storage,
                                    unit: 'gb'
                                } : undefined
                            }
                        }
                    ).then(_ => {
                        this.setFlashMessage(
                            <>Software Stack: {name}, Edit request submitted</>, "success"
                        )
                        // Clear selection and properly refresh the list
                        this.hideEditSoftwareStackForm();
                        this.setState({
                            softwareStackSelected: false
                        }, () => {
                            setTimeout(() => {
                                this.getListing().fetchRecords();
                            }, 3000); // Wait 3 seconds before refreshing
                        });
                        return Promise.resolve(true)
                    }).catch(error => {
                        this.props.onFlashbarChange({
                            items: [
                                {
                                    type: 'error',
                                    content: `Failed to update software stack: ${error.message}`,
                                    dismissible: true
                                }
                            ]
                        })
                        return Promise.resolve(false)
                    })
                }}
                onDismiss={() => {
                    this.hideEditSoftwareStackForm()
                    this.setState({
                        softwareStackSelected: false
                    }, () => {
                        setTimeout(() => {
                            this.getListing().fetchRecords();
                        }, 3000); // Wait 3 seconds before refreshing
                    });
                }}
            />
        )
    }

    buildListing() {
        return (
            <IdeaListView
                ref={this.listing}
                title="Software Stacks"
                preferencesKey={'software-stack'}
                showPreferences={true}
                description="Manage your Virtual Desktop Software Stacks"
                selectionType="multi"
                primaryAction={{
                    id: 'create-software-stack',
                    text: 'Create Software Stack',
                    onClick: () => {
                        this.showCreateSoftwareStackForm()
                    }
                }}
                secondaryActionsDisabled={!this.isSelected()}
                secondaryActions={[
                    {
                        id: 'edit-software-stack',
                        text: 'Edit Stack',
                        disabled: !this.isSelected() || this.getSelectedSoftwareStacks().length > 1,
                        disabledReason: this.getSelectedSoftwareStacks().length > 1 ? 'Select only one stack to edit' : undefined,
                        onClick: () => {
                            this.setState({
                                    showEditSoftwareStackForm: true
                                }, () => {
                                    this.showEditSoftwareStackForm()
                                }
                            )
                        }
                    },
                    {
                        id: 'clone-software-stack',
                        text: 'Clone Stack',
                        disabled: !this.isSelected() || this.getSelectedSoftwareStacks().length > 1,
                        disabledReason: this.getSelectedSoftwareStacks().length > 1 ? 'Select only one stack to clone' : undefined,
                        onClick: this.cloneSoftwareStack
                    },
                    {
                        id: 'toggle-software-stack',
                        text: this.getToggleActionText(),
                        disabled: !this.isSelected(),
                        onClick: this.toggleSoftwareStackEnabled
                    },
                    {
                        id: 'delete-software-stack',
                        text: this.getDeleteActionText(),
                        disabled: !this.isSelected() || (this.getSelectedSoftwareStack()?.stack_id?.startsWith('base-') ?? false),
                        onClick: this.deleteSoftwareStack
                    }
                ]}
                showPaginator={true}
                showFilters={true}
                filterType="select"
                defaultSortingColumn="name"
                defaultSortingDescending={false}
                selectFilters={[
                    {
                        name: '$all',
                    },
                    {
                        name: 'base_os',
                        choices: [
                            {
                                title: 'All Operating Systems',
                                value: ''
                            },
                            {
                                title: 'Amazon Linux 2023',
                                value: 'amazonlinux2023'
                            },
                            {
                                title: 'Amazon Linux 2',
                                value: 'amazonlinux2'
                            },
                            {
                                title: 'Windows 2019',
                                value: 'windows2019'
                            },
                            {
                                title: 'Windows 2022',
                                value: 'windows2022'
                            },
                            {
                                title: 'Windows 2025',
                                value: 'windows2025'
                            },
                            {
                                title: 'Red Hat Enterprise Linux 8',
                                value: 'rhel8'
                            },
                            {
                                title: 'Red Hat Enterprise Linux 9',
                                value: 'rhel9'
                            },
                            {
                                title: 'Rocky Linux 8',
                                value: 'rocky8'
                            },
                            {
                                title: 'Rocky Linux 9',
                                value: 'rocky9'
                            },
                            {
                                title: 'Ubuntu 22.04',
                                value: 'ubuntu2204'
                            },
                            {
                                title: 'Ubuntu 24.04',
                                value: 'ubuntu2404'
                            }
                        ]
                    }
                ]}
                onFilter={(filters) => {
                    return filters
                }}
                onRefresh={() => {
                    this.setState({
                        softwareStackSelected: false
                    }, () => {
                        this.getListing().fetchRecords()
                    })
                }}
                onSelectionChange={() => {
                    this.setState({
                        softwareStackSelected: true
                    })
                }}
                onFetchRecords={() => {
                    return this.getVirtualDesktopAdminClient().listSoftwareStacks({
                        disabled_also: true,
                        filters: this.getListing().getFilters(),
                        paginator: this.getListing().getPaginator()
                    }).catch(error => {
                        this.props.onFlashbarChange({
                            items: [
                                {
                                    content: error.message,
                                    type: 'error',
                                    dismissible: true
                                }
                            ]
                        })
                        throw error
                    })
                }}
                columnDefinitions={VIRTUAL_DESKTOP_SOFTWARE_STACKS_TABLE_COLUMN_DEFINITIONS}
            />
        )
    }

    buildActionConfirmModal() {
        return (
            <IdeaConfirm
                ref={this.softwareStackActionConfirmModal}
                title={this.state.confirmAction.actionTitle}
                onConfirm={this.state.confirmAction.onConfirm}
                onCancel={this.state.confirmAction.onCancel}
                dangerConfirm={true}
            >
                {this.state.confirmAction.actionText}
            </IdeaConfirm>
        );
    }

    toggleSoftwareStackEnabled = () => {
        const selectedSoftwareStacks = this.getSelectedSoftwareStacks();
        if (selectedSoftwareStacks.length === 0) {
            return;
        }

        // Determine if we're enabling or disabling based on the first selected stack
        // Default to disabling (setting enabled=false) if stacks have mixed states
        const allEnabled = selectedSoftwareStacks.every(stack => stack.enabled);
        const newEnabledState = !allEnabled;
        const action = newEnabledState ? 'enable' : 'disable';
        const plural = selectedSoftwareStacks.length > 1;
        const stackNames = selectedSoftwareStacks.map(stack => stack.name).join(", ");

        this.setState({
            confirmAction: {
                actionTitle: `${action.charAt(0).toUpperCase() + action.slice(1)} Software Stack${plural ? 's' : ''}`,
                actionText: (
                    <div>
                        Are you sure you want to {action} the following software stack{plural ? 's' : ''}: <strong>{stackNames}</strong>?
                    </div>
                ),
                onConfirm: () => {
                    // Process each stack individually
                    const promises = selectedSoftwareStacks.map(softwareStack => {
                        return this.getVirtualDesktopAdminClient().updateSoftwareStack({
                            software_stack: {
                                ...softwareStack,
                                enabled: newEnabledState
                            }
                        });
                    });

                    Promise.all(promises)
                        .then(() => {
                            this.setFlashMessage(
                                <>Successfully {action}d {selectedSoftwareStacks.length} software stack{plural ? 's' : ''}</>,
                                "success"
                            );
                            // Reset selection and refresh the listing after a short delay
                            this.setState({
                                softwareStackSelected: false
                            }, () => {
                                setTimeout(() => {
                                    this.getListing().fetchRecords();
                                }, 3000); // Wait 3 seconds before refreshing
                            });
                        })
                        .catch(error => {
                            this.setFlashMessage(
                                <>Error {action}ing software stack{plural ? 's' : ''}: {error.message}</>,
                                "error"
                            );
                        });
                },
                onCancel: () => {
                    // No action needed
                }
            }
        }, () => {
            this.getActionConfirmModal().show();
        });
    }

    getToggleActionText(): string {
        const selectedStacks = this.getSelectedSoftwareStacks();
        if (selectedStacks.length === 0) {
            return 'Enable/Disable Stack';
        }

        const allEnabled = selectedStacks.every(stack => stack.enabled);
        const action = allEnabled ? 'Disable' : 'Enable';
        const plural = selectedStacks.length > 1 ? 's' : '';

        return `${action} Stack${plural}`;
    }

    getDeleteActionText(): string {
        const selectedStacks = this.getSelectedSoftwareStacks();
        if (selectedStacks.length === 0) {
            return 'Delete Stack';
        }

        const plural = selectedStacks.length > 1 ? 's' : '';
        return `Delete Stack${plural}`;
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
                        href: '#/virtual-desktop/sessions'
                    },
                    {
                        text: 'Software Stacks (AMIs)',
                        href: ''
                    }
                ]}
                content={
                    <div>
                        {this.state.showCreateSoftwareStackForm && this.buildCreateSoftwareStackForm()}
                        {this.state.showEditSoftwareStackForm && this.buildEditSoftwareStackForm()}
                        {this.buildActionConfirmModal()}
                        {this.buildListing()}
                    </div>
                }/>
        )
    }
}

export default withRouter(VirtualDesktopSoftwareStacks)
