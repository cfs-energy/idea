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
import {Project, SocaMemory, SocaUserInputChoice, SocaUserInputParamMetadata, User, VirtualDesktopArchitecture, VirtualDesktopBaseOS, VirtualDesktopGPU, VirtualDesktopSessionType, VirtualDesktopSoftwareStack} from "../../../client/data-model";
import Utils from "../../../common/utils";
import {AuthClient, ProjectsClient, VirtualDesktopClient} from "../../../client";
import {AppContext} from "../../../common";
import {Constants} from "../../../common/constants";
import VirtualDesktopUtilsClient from "../../../client/virtual-desktop-utils-client";

export interface VirtualDesktopCreateSessionFormProps {
    projects?: Project[]
    defaultName?: string
    maxRootVolumeMemory: number
    isAdminView?: boolean
    onSubmit: (session_name: string, username: string, project_id: string, base_os: VirtualDesktopBaseOS, software_stack_id: string, session_type: VirtualDesktopSessionType, instance_type: string, storage_size: number, hibernation_enabled: boolean, vpc_subnet_id: string, admin_custom_instance_type?: boolean) => Promise<boolean>
    onDismiss: () => void
}

export interface DCVSessionTypeChoice {
    choices: SocaUserInputChoice[]
    defaultChoice: 'CONSOLE' | 'VIRTUAL'
    disabled: boolean
}

export interface VirtualDesktopCreateSessionFormState {
    showModal: boolean
    softwareStacks: { [k: string]: VirtualDesktopSoftwareStack }
    defaultProject: Project | undefined
    supportedOsChoices: SocaUserInputChoice[]
    dcvSessionTypeChoice: DCVSessionTypeChoice
    eVDIUsers: User[]
    allowCustomInstanceType: boolean
    softwareStacksByOS: { [os: string]: VirtualDesktopSoftwareStack[] }
}

class VirtualDesktopCreateSessionForm extends Component<VirtualDesktopCreateSessionFormProps, VirtualDesktopCreateSessionFormState> {
    form: RefObject<IdeaForm>
    instanceTypesInfo: any
    defaultInstanceTypeChoices: SocaUserInputChoice[]
    // Flag to prevent duplicate API calls when hibernation is changed programmatically
    private hibernationChangedByStackSelection: boolean = false;

    constructor(props: VirtualDesktopCreateSessionFormProps) {
        super(props);
        this.form = React.createRef()
        this.state = {
            showModal: false,
            softwareStacks: {},
            defaultProject: {},
            supportedOsChoices: [],
            eVDIUsers: [],
            dcvSessionTypeChoice: {
                choices: Utils.getDCVSessionTypes(),
                defaultChoice: 'VIRTUAL',
                disabled: false
            },
            allowCustomInstanceType: false,
            softwareStacksByOS: {}
        }
        this.instanceTypesInfo = {}
        this.defaultInstanceTypeChoices = []
    }

    getAuthClient(): AuthClient {
        return AppContext.get().client().auth()
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getVirtualDesktopUtilsClient(): VirtualDesktopUtilsClient {
        return AppContext.get().client().virtualDesktopUtils()
    }

    getVirtualDesktopClient(): VirtualDesktopClient {
        return AppContext.get().client().virtualDesktop()
    }

    hideForm() {
        this.setState({
            showModal: false
        }, () => {
            this.props.onDismiss()
        })
    }

    setError(errorCode: string, message: string) {
        this.getForm().setError(errorCode, message)
    }

    getForm() {
        return this.form.current!
    }

    showModal() {
        this.setState({
            showModal: true
        }, () => {
            this.getForm().showModal()

            // Make sure hibernation is disabled initially when form opens
            setTimeout(() => {
                const hibernation = this.getForm()?.getFormField('hibernate_instance');
                if (hibernation) {
                    hibernation.disable(true);
                }
            }, 100);
        })
    }

    componentDidMount() {
        // We're not loading global instance types here anymore
        // We'll only load them after both OS and software stack are selected

        this.getProjectsClient().getProject({
            project_name: 'default'
        }).then(result => {
            this.setState({
                defaultProject: result.project
            })
            // Removed OS choices initialization - will be empty until project selected
        })

        let groups: string[] = [Utils.getUserGroupName(Utils.getModuleId(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)), Utils.getAdministratorGroup(Utils.getModuleId(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER))]
        this.getAuthClient().listUsersInGroup({
            group_names: groups,
            paginator: {
                page_size: 1000
            }
        }).then(group_response => {
            this.setState({
                eVDIUsers: group_response.listing!
            }, () => {
                this.getForm()?.getFormField('user_name')?.setOptions({
                    listing: Utils.generateUserSelectionChoices(this.state.eVDIUsers)
                })
            })
        })

        // Initially disable hibernation until software stack is selected
        setTimeout(() => {
            const hibernation = this.getForm()?.getFormField('hibernate_instance');
            if (hibernation) {
                hibernation.disable(true);
            }
        }, 100);
    }

    generateInstanceTypeReverseIndex(instanceTypeList: any[]): { [k: string]: any } {
        let reverseIndex: { [k: string]: any } = {}
        if (instanceTypeList !== undefined) {
            instanceTypeList.forEach((instance_type: any) => {
                reverseIndex[instance_type.InstanceType as string] = instance_type
            })
        }
        return reverseIndex
    }

    compare_software_stacks = (a: VirtualDesktopSoftwareStack, b: VirtualDesktopSoftwareStack): number => {
        if (a === undefined && b === undefined) {
            return 0
        }

        if (a === undefined || a.architecture === undefined) {
            return -1
        }

        if (b === undefined || b.architecture === undefined) {
            return 1
        }

        if (a.architecture === b.architecture) {
            // these two are the same architecture. Return GPU
            if (a.gpu === b.gpu) {
                // these two are the same architecture and GPU. Return alphabetical
                if (a.name === undefined) {
                    return 1
                }
                if (b.name === undefined) {
                    return -1
                }
                return a.name.toLowerCase().localeCompare(b.name.toLowerCase(), undefined, {numeric: true});
            } else {
                if (a.gpu === "NO_GPU") {
                    return -1
                }
                if (b.gpu === "NO_GPU") {
                    return 1
                }
                if (a.gpu === "NVIDIA") {
                    return -1
                }
                if (b.gpu === "NVIDIA") {
                    return 1
                }
                if (a.gpu === "AMD") {
                    return -1
                }
                return 1
            }
        }

        if (a.architecture === "x86_64") {
            return -1
        }
        if (b.architecture === "x86_64") {
            return 1
        }
        if (a.architecture === 'arm64') {
            return -1
        }
        return 1
    }

    generateSoftwareStackListing(softwareStacks: VirtualDesktopSoftwareStack[] | undefined): SocaUserInputChoice[] {
        let softwareStackChoices: SocaUserInputChoice[] = []

        // Only show explicitly enabled stacks
        const enabledStacks = softwareStacks?.filter(stack => stack.enabled === true);

        enabledStacks?.sort(this.compare_software_stacks)

        enabledStacks?.forEach((stack) => {
            softwareStackChoices.push({
                title: stack.description,
                description: `Name: ${stack.name}, AMI ID: ${stack.ami_id}, OS: ${stack.base_os}`,
                value: stack.stack_id
            })
        })
        return softwareStackChoices
    }

    getInstanceArch(instance_type: string): VirtualDesktopArchitecture | undefined {
        if (this.instanceTypesInfo === undefined) {
            return undefined
        }
        let instanceTypeInfo: any = this.instanceTypesInfo[instance_type]
        if (instanceTypeInfo === undefined) {
            return undefined
        }
        let isARM = false
        let isX86 = false
        instanceTypeInfo?.ProcessorInfo?.SupportedArchitectures?.forEach((arch: any) => {
            if (arch === 'arm64') {
                isARM = true
            }
            if (arch === 'x86_64') {
                isX86 = true
            }
        })
        if (isX86) {
            return 'x86_64'
        }
        if (isARM) {
            return 'arm64'
        }
        return undefined
    }

    getInstanceGPU(instance_type: string): VirtualDesktopGPU {
        if (this.instanceTypesInfo === undefined) {
            return "NO_GPU"
        }
        let instanceTypeInfo: any = this.instanceTypesInfo[instance_type]
        if (instanceTypeInfo === undefined) {
            return "NO_GPU"
        }

        let isAMD = false
        let isNVIDIA = false
        instanceTypeInfo?.GpuInfo?.Gpus?.forEach((gpuInfo: any) => {
            if (gpuInfo.Manufacturer === 'AMD') {
                isAMD = true
            }

            if (gpuInfo.Manufacturer === 'NVIDIA') {
                isNVIDIA = true
            }
        })

        if (isAMD)
            return "AMD"
        if (isNVIDIA)
            return "NVIDIA"
        return "NO_GPU"
    }

    getInstanceRAM(instance_type: string): SocaMemory {
        if (this.instanceTypesInfo === undefined) {
            return {
                value: 0,
                unit: "gb"
            }
        }

        let instanceTypeInfo: any = this.instanceTypesInfo[instance_type]
        if (instanceTypeInfo === undefined) {
            return {
                value: 0,
                unit: "gb"
            }
        }
        return {
            unit: "gb",
            value: instanceTypeInfo.MemoryInfo.SizeInMiB / 1024
        }
    }

    getMinRootVolumeSizeInGB(softwareStack: VirtualDesktopSoftwareStack, isHibernationEnabled: boolean, instanceTypeName: string): SocaMemory {
        let min_storage_gb: SocaMemory = {
            unit: 'gb',
            value: 0
        }
        if (softwareStack && softwareStack.min_storage) {
            min_storage_gb = {
                unit: softwareStack.min_storage.unit,
                value: softwareStack.min_storage.value
            }
        }
        if (isHibernationEnabled && instanceTypeName) {
            min_storage_gb.value += this.getInstanceRAM(instanceTypeName).value * 1.3;
        }
        return min_storage_gb
    }

    buildProjectChoices(projects: Project[]): SocaUserInputChoice[] {
        let projectChoices: SocaUserInputChoice[] = []
        projects?.forEach(project => {
            projectChoices.push({
                title: project.title,
                value: project.project_id,
                description: project.description
            })
        })
        return projectChoices
    }

    updateSoftwareStackOptions() {
        let project_id = this.getForm()?.getFormField('project_id')?.getValueAsString()
        let base_os = this.getForm()?.getFormField('base_os')?.getValueAsString()
        if (Utils.isEmpty(project_id) || Utils.isEmpty(base_os)) {
            return
        }

        // Check if we have cached software stacks for this OS
        if (base_os && this.state.softwareStacksByOS[base_os] && this.state.softwareStacksByOS[base_os].length > 0) {
            const cachedStacks = this.state.softwareStacksByOS[base_os];
            // Only show explicitly enabled stacks
            const enabledCachedStacks = cachedStacks.filter(stack => stack.enabled === true);

            // Use cached data instead of making another API call
            const softwareStack = this.getForm()?.getFormField('software_stack')
            softwareStack?.setOptions({
                listing: this.generateSoftwareStackListing(enabledCachedStacks)
            })

            let softwareStacks: { [k: string]: VirtualDesktopSoftwareStack } = {}
            enabledCachedStacks.forEach((stack: VirtualDesktopSoftwareStack) => {
                if (stack.stack_id !== undefined) {
                    softwareStacks[stack.stack_id] = stack
                }
            })
            this.setState({
                softwareStacks: softwareStacks
            })
            return;
        }

        // If no cached data, make the API call
        this.getVirtualDesktopClient().listSoftwareStacks({
            disabled_also: false, // Only get enabled stacks
            project_id: project_id,
            filters: [{
                key: 'base_os',
                value: base_os
            }]
        }).then(result => {
            // Only include stacks that are explicitly enabled
            const enabledApiStacks = result.listing?.filter(stack => stack.enabled === true);

            const softwareStack = this.getForm()?.getFormField('software_stack')
            softwareStack?.setOptions({
                listing: this.generateSoftwareStackListing(enabledApiStacks)
            })

            let softwareStacks: { [k: string]: VirtualDesktopSoftwareStack } = {}
            enabledApiStacks?.forEach((stack) => {
                if (stack.stack_id !== undefined) {
                    softwareStacks[stack.stack_id] = stack
                }
            })
            this.setState({
                softwareStacks: softwareStacks
            })
        })
    }

    updateRootVolumeSizeIfRequired() {
        let softwareStack = this.state.softwareStacks[this.getForm()?.getValue('software_stack')]
        let isHibernationSupported = Utils.asBoolean(this.getForm()?.getValue('hibernate_instance'))
        let instanceTypeName = this.getForm()?.getValue('instance_type')
        let min_storage_gb = this.getMinRootVolumeSizeInGB(softwareStack, isHibernationSupported, instanceTypeName)
        let root_storage_size = this.getForm()?.getFormField('root_storage_size')
        let current_storage_size = Utils.asNumber(root_storage_size?.getValueAsString());
        if (current_storage_size < min_storage_gb.value!) {
            root_storage_size?.setValue(min_storage_gb.value!)
        }
    }

    updateSessionTypeChoicesIfRequired() {
        const dcvSessionType = this.getForm()?.getFormField('dcv_session_type')
        let dcvSessionTypeChoices: SocaUserInputChoice[] = []
        const base_os = this.getForm()?.getValue('base_os')
        let instanceTypeName = this.getForm()?.getValue('instance_type')

        // Return early if the required fields are not set
        if (!base_os || !instanceTypeName) {
            return;
        }

        let gpu = this.getInstanceGPU(instanceTypeName)
        let arch = this.getInstanceArch(instanceTypeName)
        let dcvSessionTypeDefaultChoice: 'VIRTUAL' | 'CONSOLE' = 'VIRTUAL'
        let disableSessionTypeChoice = false

        const console_choice = {
            title: 'Console',
            value: 'CONSOLE'
        }
        const virtual_choice = {
            title: 'Virtual',
            value: 'VIRTUAL'
        }
        if (arch === 'arm64') {
            dcvSessionTypeDefaultChoice = 'VIRTUAL'
            dcvSessionTypeChoices.push(virtual_choice)
            disableSessionTypeChoice = true
        } else if ((base_os && base_os.includes('windows')) || gpu === 'AMD') {
            // https://docs.aws.amazon.com/dcv/latest/adminguide/servers.html - AMD GPU, Windows support Console sessions only
            dcvSessionTypeDefaultChoice = 'CONSOLE'
            dcvSessionTypeChoices.push(console_choice)
            disableSessionTypeChoice = true
        } else {
            dcvSessionTypeChoices.push(console_choice)
            dcvSessionTypeChoices.push(virtual_choice)
        }
        this.setState({
            ...this.state,
            dcvSessionTypeChoice: {
                choices: dcvSessionTypeChoices,
                defaultChoice: dcvSessionTypeDefaultChoice,
                disabled: disableSessionTypeChoice
            }
        })
        dcvSessionType?.setOptions({
            listing: dcvSessionTypeChoices
        })
        dcvSessionType?.setValue(dcvSessionTypeDefaultChoice)
        dcvSessionType?.disable(disableSessionTypeChoice)
    }

    buildFormParams(): SocaUserInputParamMetadata[] {
        let formParams: SocaUserInputParamMetadata[] = []

        formParams.push({
            name: 'session_name',
            title: 'Session Name',
            description: 'Enter a name for the virtual desktop',
            data_type: 'str',
            param_type: 'text',
            help_text: 'Session Name is required and cannot exceed more than 24 characters',
            default: this.props.defaultName!,
            auto_focus: true,
            validate: {
                required: true,
                regex: '^.{3,24}$'
            }
        })

        if (this.props.isAdminView) {
            formParams.push({
                name: 'user_name',
                title: 'User',
                description: 'Select the user to create the session for',
                data_type: 'str',
                param_type: 'select_or_text',
                validate: {
                    required: true
                },
                choices: Utils.generateUserSelectionChoices(this.state.eVDIUsers)
            })
        }
        formParams.push({
            name: 'project_id',
            title: 'Project',
            description: 'Select the project under which the session will get created',
            data_type: 'str',
            param_type: 'select',
            validate: {
                required: true
            },
            default: this.state.defaultProject?.project_id,
            choices: this.buildProjectChoices(this.props.projects!)
        })
        formParams.push({
            name: 'base_os',
            title: 'Operating System',
            description: 'Select the operating system for the virtual desktop',
            data_type: 'str',
            param_type: 'select',
            validate: {
                required: true
            },
            choices: this.state.supportedOsChoices // Use the filtered choices from state
        })
        formParams.push({
            name: 'software_stack',
            title: 'Software Stack',
            description: 'Select the software stack for your virtual desktop',
            data_type: 'str',
            param_type: 'select',
            validate: {
                required: true
            },
            choices: []
        })
        formParams.push({
            name: 'hibernate_instance',
            title: 'Enable Instance Hibernation',
            description: 'Hibernation saves the contents from the instance memory (RAM) to your Amazon Elastic Block Store (Amazon EBS) root volume. You can not change instance type if you enable this option.',
            data_type: 'bool',
            param_type: 'confirm',
            default: false,
            validate: {
                required: true
            }
        })

        // Check if both base_os and software_stack are selected
        const softwareStackSelected = !!this.getForm()?.getValue('software_stack');
        const baseOsSelected = !!this.getForm()?.getValue('base_os');
        const showInstanceType = softwareStackSelected && baseOsSelected;

        formParams.push({
            name: 'instance_type',
            title: 'Virtual Desktop Size',
            description: this.state.allowCustomInstanceType ? 'Enter any valid EC2 instance type' : 'Select a virtual desktop instance type',
            data_type: 'str',
            param_type: this.state.allowCustomInstanceType ? 'text' : 'select',
            validate: {
                required: showInstanceType // Only required if OS and stack are selected
            },
            choices: (showInstanceType && !this.state.allowCustomInstanceType) ? this.defaultInstanceTypeChoices : []
        })
        formParams.push({
            name: 'root_storage_size',
            title: 'Storage Size (GB)',
            description: 'Enter the storage size for your virtual desktop in GBs',
            data_type: 'int',
            param_type: 'text',
            default: 10,
            validate: {
                required: true,
                max: this.props.maxRootVolumeMemory
            }
        })
        formParams.push({
            name: 'advanced_options',
            title: 'Show Advanced Options',
            data_type: 'bool',
            param_type: 'confirm',
            validate: {
                required: true
            },
            default: false
        })

        if (this.props.isAdminView) {
            formParams.push({
                name: 'allow_custom_instance_type',
                title: 'Enable Custom Instance Types',
                description: 'Allow entering any valid EC2 instance type',
                data_type: 'bool',
                param_type: 'confirm',
                default: this.state.allowCustomInstanceType,
                validate: {
                    required: true
                },
                when: {
                    param: 'advanced_options',
                    eq: true
                }
            })
        }

        formParams.push({
            name: 'dcv_session_type',
            title: 'DCV Session Type',
            description: 'Select the DCV Session Type',
            data_type: 'str',
            param_type: 'select',
            readonly: this.state.dcvSessionTypeChoice.disabled,
            default: this.state.dcvSessionTypeChoice.defaultChoice,
            choices: this.state.dcvSessionTypeChoice.choices,
            validate: {
                required: true
            },
            when: {
                param: 'advanced_options',
                eq: true
            }
        })
        formParams.push({
            name: 'vpc_subnet_id',
            title: 'VPC Subnet Id',
            description: 'Launch your virtual desktop in a specific subnet',
            data_type: 'str',
            param_type: 'text',
            when: {
                param: 'advanced_options',
                eq: true
            }
        })

        return formParams
    }

    getSoftwareStack(stack_id: string): VirtualDesktopSoftwareStack | undefined {
        if (!stack_id || !this.state.softwareStacks || !this.state.softwareStacks[stack_id]) {
            return undefined;
        }

        // Return a clean object with only the properties that are defined
        const stack = this.state.softwareStacks[stack_id];
        return {
            stack_id: stack.stack_id,
            base_os: stack.base_os,
            name: stack.name,
            description: stack.description,
            ami_id: stack.ami_id,
            architecture: stack.architecture,
            gpu: stack.gpu,
            min_storage: stack.min_storage,
            min_ram: stack.min_ram,
            allowed_instance_types: stack.allowed_instance_types,
        };
    }

    // Filter instance types based on software stack allowed_instance_types
    filterInstanceTypesByAllowedInstanceTypes(instanceTypes: any[], allowedInstanceTypes: string[] | undefined): any[] {
        if (!allowedInstanceTypes || allowedInstanceTypes.length === 0) {
            return instanceTypes;
        }

        return instanceTypes.filter(instance => {
            const instanceType = instance.InstanceType;
            const instanceFamily = instanceType.split('.')[0];

            // Check if instance type or family is in the allowed list
            return allowedInstanceTypes.some(allowedType => {
                if (allowedType.includes('.')) {
                    // Exact instance type match
                    return allowedType === instanceType;
                } else {
                    // Instance family match
                    return allowedType === instanceFamily;
                }
            });
        });
    }

    // Filter g4ad instance types based on OS compatibility
    filterG4adInstanceTypes(instanceTypes: any[], baseOs: string): any[] {
        const g4adCompatibleOS = ['windows2019', 'windows2022', 'rocky8', 'rocky9'];

        return instanceTypes.filter(instance => {
            const instanceType = instance.InstanceType;
            const instanceFamily = instanceType.split('.')[0];

            // If it's a g4ad instance type, only allow it for compatible OS
            if (instanceFamily === 'g4ad') {
                return g4adCompatibleOS.includes(baseOs);
            }

            // For non-g4ad instance types, allow them for all OS
            return true;
        });
    }

    // Method to update OS choices based on project's accessible software stacks
    updateSupportedOSChoices(project_id?: string) {
        if (!project_id) {
            return;
        }

        // First, get all supported OS options
        this.getVirtualDesktopUtilsClient().listSupportedOS({}).then(osResult => {
            // Now get all enabled software stacks for the project to determine which OS options have stacks
            // Use a large page size to get all stacks in one request
            this.getVirtualDesktopClient().listSoftwareStacks({
                disabled_also: false, // Only get enabled stacks
                project_id: project_id,
                paginator: {
                    page_size: 100 // Ensure we get all stacks in one request
                }
            }).then(stackResult => {
                // Only include stacks that are explicitly enabled
                const projectEnabledStacks = stackResult.listing?.filter(stack => stack.enabled === true);

                // Extract unique base OS values from available software stacks
                const osWithStacksSet = new Set<string>();

                // Create a map to store software stacks by OS
                const softwareStacksByOS: { [os: string]: VirtualDesktopSoftwareStack[] } = {};

                projectEnabledStacks?.forEach(stack => {
                    if (stack.base_os) {
                        osWithStacksSet.add(stack.base_os);

                        // Group stacks by OS
                        if (!softwareStacksByOS[stack.base_os]) {
                            softwareStacksByOS[stack.base_os] = [];
                        }
                        softwareStacksByOS[stack.base_os].push(stack);
                    }
                });

                // Create choices array with only OS options that have stacks
                const supportedOsChoices: SocaUserInputChoice[] = [];
                osResult.listing?.forEach(os => {
                    // Only add OS options that have software stacks
                    if (osWithStacksSet.has(os)) {
                        supportedOsChoices.push({
                            title: Utils.getOsTitle(os),
                            value: os
                        });
                    }
                });

                this.setState({
                    supportedOsChoices,
                    softwareStacksByOS // Cache the software stacks by OS
                }, () => {
                    this.getForm()?.getFormField('base_os')?.setOptions({
                        listing: supportedOsChoices
                    });
                });
            });
        });
    }

    render() {
        return (
            this.state.showModal &&
            <IdeaForm
                ref={this.form}
                name="create-session"
                modal={true}
                title="Launch New Virtual Desktop"
                modalSize="medium"
                onStateChange={(event) => {
                    if (event.param.name === 'base_os') {
                        const hibernation = this.getForm()?.getFormField('hibernate_instance')

                        // Set hibernation to disabled by default
                        if (hibernation) {
                            hibernation.setValue(false)
                        }

                        // Reset flag since this is a user-initiated OS change
                        this.hibernationChangedByStackSelection = false;

                        // Only consider enabling hibernation if we have both OS and software stack
                        const softwareStackValue = this.getForm()?.getValue('software_stack');
                        const osSelected = !!event.value;
                        const stackSelected = !!softwareStackValue;

                        if (osSelected && stackSelected) {
                            if (event.value === 'windows2019' || event.value === 'windows2022' || event.value === 'windows2025' ||
                                event.value === 'amazonlinux2' || event.value === 'amazonlinux2023' ||
                                event.value === 'rhel8' || event.value === 'rhel9' ||
                                event.value === 'rocky8' || event.value === 'rocky9' ||
                                event.value === 'ubuntu2204') {
                                // Hibernation is supported for these OS types
                                hibernation?.disable(false)
                            } else {
                                // Hibernation is not supported for other OS types
                                hibernation?.disable(true)
                            }
                        } else {
                            // If no stack is selected, keep hibernation disabled regardless of OS
                            hibernation?.disable(true)
                        }

                        // Clear any previously selected software stack when OS changes
                        const softwareStack = this.getForm()?.getFormField('software_stack');
                        if (softwareStack) {
                            softwareStack.setValue('');
                        }

                        // Clear any previously selected instance type when OS changes
                        const instanceType = this.getForm()?.getFormField('instance_type');
                        if (instanceType) {
                            instanceType.setValue('');
                            instanceType.setOptions({ listing: [] });
                        }

                        // Refresh instance types if software stack is already selected
                        // This ensures g4ad filtering is applied when OS changes
                        if (softwareStackValue && event.value && !this.state.allowCustomInstanceType) {
                            const selectedSoftwareStack = this.getSoftwareStack(softwareStackValue);
                            if (selectedSoftwareStack) {
                                this.getVirtualDesktopUtilsClient().listAllowedInstanceTypes({
                                    hibernation_support: this.getForm()?.getValue('hibernate_instance'),
                                    software_stack: selectedSoftwareStack
                                }).then(result => {
                                    // Filter by allowed_instance_types if present in software stack
                                    const filteredResults = selectedSoftwareStack?.allowed_instance_types
                                        ? this.filterInstanceTypesByAllowedInstanceTypes(result.listing, selectedSoftwareStack.allowed_instance_types)
                                        : result.listing;

                                    // Filter g4ad instance types based on OS compatibility
                                    const g4adFilteredResults = this.filterG4adInstanceTypes(filteredResults, event.value);

                                    // Store instance types info for future reference
                                    this.instanceTypesInfo = this.generateInstanceTypeReverseIndex(result.listing)
                                    this.defaultInstanceTypeChoices = Utils.generateInstanceTypeListing(g4adFilteredResults)

                                    let instance_type = this.getForm()?.getFormField('instance_type')
                                    instance_type?.setOptions({
                                        listing: Utils.generateInstanceTypeListing(g4adFilteredResults)
                                    })

                                    // Make instance_type field required now that we have selected both OS and software stack
                                    instance_type?.validate();
                                });
                            }
                        }

                        // Only update session type choices if instance type is selected
                        const instanceTypeName = this.getForm()?.getValue('instance_type');
                        if (instanceTypeName && event.value) {
                            this.updateSessionTypeChoicesIfRequired();
                        }

                        this.updateSoftwareStackOptions()
                    } else if (event.param.name === 'software_stack') {
                        // Only fetch and update instance types if software stack and base OS are selected
                        // Enable hibernation option when software stack is selected
                        const hibernation = this.getForm()?.getFormField('hibernate_instance');
                        const baseOs = this.getForm()?.getValue('base_os');

                        // Only enable hibernation if we have both stack and OS selected
                        if (hibernation && baseOs) {
                            // Enable or disable based on OS compatibility with hibernation
                            if (baseOs === 'windows2019' || baseOs === 'windows2022' || baseOs === 'windows2025' ||
                                baseOs === 'amazonlinux2' || baseOs === 'amazonlinux2023' ||
                                baseOs === 'rhel8' || baseOs === 'rhel9' ||
                                baseOs === 'rocky8' || baseOs === 'rocky9' ||
                                baseOs === 'ubuntu2204') {
                                hibernation.disable(!event.value); // Enable if we have a value
                            } else {
                                // For other OS types that don't support hibernation
                                hibernation.disable(true);
                            }
                        } else {
                            // If OS isn't selected yet, keep hibernation disabled
                            hibernation?.disable(true);
                        }

                        if (!this.state.allowCustomInstanceType && event.value) {
                            const softwareStack = this.getSoftwareStack(event.value);
                            const baseOs = this.getForm()?.getValue('base_os');

                            if (baseOs) {
                                // Set a flag to indicate this hibernation change was triggered by software stack selection
                                // to prevent duplicate API calls
                                this.hibernationChangedByStackSelection = true;

                                this.getVirtualDesktopUtilsClient().listAllowedInstanceTypes({
                                    hibernation_support: this.getForm()?.getValue('hibernate_instance'),
                                    software_stack: softwareStack
                                }).then(result => {
                                    // Filter by allowed_instance_types if present in software stack
                                    const filteredResults = softwareStack?.allowed_instance_types
                                        ? this.filterInstanceTypesByAllowedInstanceTypes(result.listing, softwareStack.allowed_instance_types)
                                        : result.listing;

                                    // Filter g4ad instance types based on OS compatibility
                                    const g4adFilteredResults = baseOs ? this.filterG4adInstanceTypes(filteredResults, baseOs) : filteredResults;

                                    // Store instance types info for future reference
                                    this.instanceTypesInfo = this.generateInstanceTypeReverseIndex(result.listing)
                                    this.defaultInstanceTypeChoices = Utils.generateInstanceTypeListing(g4adFilteredResults)

                                    let instance_type = this.getForm()?.getFormField('instance_type')
                                    instance_type?.setOptions({
                                        listing: Utils.generateInstanceTypeListing(g4adFilteredResults)
                                    })

                                    // Make instance_type field required now that we have selected both OS and software stack
                                    instance_type?.validate();

                                    this.updateRootVolumeSizeIfRequired()

                                    // Only update session type choices if an instance type is actually selected
                                    if (instance_type?.getValueAsString()) {
                                        this.updateSessionTypeChoicesIfRequired();
                                    }
                                })
                            }
                        } else {
                            this.updateRootVolumeSizeIfRequired()
                        }
                    } else if (event.param.name === 'project_id') {
                        // Reset flag since this is a user-initiated project change
                        this.hibernationChangedByStackSelection = false;

                        // Update OS choices when project changes
                        this.updateSupportedOSChoices(event.value);

                        // Clear the base_os selection when project changes
                        const baseOs = this.getForm()?.getFormField('base_os');
                        if (baseOs) {
                            baseOs.setValue('');
                        }

                        // Clear the software stack selection when project changes
                        const softwareStack = this.getForm()?.getFormField('software_stack');
                        if (softwareStack) {
                            softwareStack.setValue('');
                            softwareStack.setOptions({ listing: [] });
                        }

                        // Clear the instance type selection as well
                        const instanceType = this.getForm()?.getFormField('instance_type');
                        if (instanceType) {
                            instanceType.setValue('');
                            instanceType.setOptions({ listing: [] });
                        }

                        // Disable hibernation again since software stack was cleared
                        const hibernation = this.getForm()?.getFormField('hibernate_instance');
                        if (hibernation) {
                            hibernation.disable(true);
                        }
                    } else if (event.param.name === 'instance_type') {
                        this.updateRootVolumeSizeIfRequired()
                        // Only update session type choices if instance type and base_os are selected
                        const baseOs = this.getForm()?.getValue('base_os');
                        if (baseOs && event.value) {
                            this.updateSessionTypeChoicesIfRequired();
                        }
                    } else if (event.param.name === 'root_storage_size') {
                        let min_storage_gb = this.getMinRootVolumeSizeInGB(this.state.softwareStacks[this.getForm()?.getValue('software_stack')], this.getForm()?.getValue('hibernate_instance'), this.getForm()?.getValue('instance_type'))
                        if (event.value < min_storage_gb.value) {
                            event.ref.setState({
                                errorMessage: `Storage size must be greater than or equal to: ${Utils.getFormattedMemory(min_storage_gb)}`
                            })
                        } else {
                            event.ref.setState({
                                errorMessage: ''
                            })
                        }
                    } else if (event.param.name === 'hibernate_instance') {
                        // Skip the API call if this change was triggered by software stack selection
                        if (this.hibernationChangedByStackSelection) {
                            // Reset the flag and skip the duplicate API call
                            this.hibernationChangedByStackSelection = false;
                            return;
                        }

                        // Only fetch and update instance types if not using custom instance type
                        if (!this.state.allowCustomInstanceType) {
                            const softwareStack = this.getSoftwareStack(this.getForm()?.getValue('software_stack'));
                            const baseOs = this.getForm()?.getValue('base_os');

                            // Only update instance types if both software stack and base OS are selected
                            if (softwareStack && baseOs) {
                                this.getVirtualDesktopUtilsClient().listAllowedInstanceTypes({
                                    hibernation_support: event.value,
                                    software_stack: softwareStack
                                }).then(result => {
                                    // Filter by allowed_instance_types if present in software stack
                                    const filteredResults = softwareStack?.allowed_instance_types
                                        ? this.filterInstanceTypesByAllowedInstanceTypes(result.listing, softwareStack.allowed_instance_types)
                                        : result.listing;

                                    // Filter g4ad instance types based on OS compatibility
                                    const g4adFilteredResults = baseOs ? this.filterG4adInstanceTypes(filteredResults, baseOs) : filteredResults;

                                    // Update instance types info
                                    this.instanceTypesInfo = this.generateInstanceTypeReverseIndex(result.listing)
                                    this.defaultInstanceTypeChoices = Utils.generateInstanceTypeListing(g4adFilteredResults)

                                    let instance_type = this.getForm()?.getFormField('instance_type')
                                    instance_type?.setOptions({
                                        listing: Utils.generateInstanceTypeListing(g4adFilteredResults)
                                    })

                                    this.updateRootVolumeSizeIfRequired()
                                })
                            } else {
                                // If either software stack or base OS is not selected yet, just clear instance type
                                const instanceType = this.getForm()?.getFormField('instance_type');
                                if (instanceType) {
                                    instanceType.setValue('');
                                    instanceType.setOptions({ listing: [] });
                                }
                            }
                        } else {
                            this.updateRootVolumeSizeIfRequired()
                        }
                    } else if (event.param.name === 'user_name') {
                        this.getProjectsClient().getUserProjects({
                            username: event.value || AppContext.get().auth().getUsername()
                        }).then(result => {
                            this.getForm()?.getFormField('project_id')?.setOptions({
                                listing: this.buildProjectChoices(result.projects!)
                            })
                        })
                    } else if (event.param.name === 'allow_custom_instance_type') {
                        // Just update the state - the field will be updated according to buildFormParams()
                        this.setState({
                            allowCustomInstanceType: event.value
                        })
                    }
                }}
                onSubmit={() => {
                    this.getForm()?.clearError()
                    if (!this.getForm()?.validate()) {
                        return
                    }
                    const values = this.getForm()?.getValues()
                    const storage_size = Utils.asNumber(values.root_storage_size, 10)
                    const session_name = values.session_name
                    const hibernation_enabled = Utils.asBoolean(values.hibernate_instance, false)
                    const software_stack_id = values.software_stack
                    const base_os = values.base_os
                    const vpc_subnet_id = values.vpc_subnet_id
                    const project_id = values.project_id
                    const session_type = values.dcv_session_type
                    const instance_type = values.instance_type
                    let username = values.user_name

                    // Pass admin_custom_instance_type flag to backend when admin has enabled custom instance types
                    const admin_custom_instance_type = this.props.isAdminView && this.state.allowCustomInstanceType

                    if (this.props.onSubmit) {
                        return this.props.onSubmit(session_name, username, project_id, base_os, software_stack_id, session_type, instance_type, storage_size, hibernation_enabled, vpc_subnet_id, admin_custom_instance_type)
                    } else {
                        return Promise.resolve(true)
                    }
                }}
                onCancel={() => {
                    this.hideForm()
                }}
                params={this.buildFormParams()}
            />
        )
    }
}

export default VirtualDesktopCreateSessionForm
