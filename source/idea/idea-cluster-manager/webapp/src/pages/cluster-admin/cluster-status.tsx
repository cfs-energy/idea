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

import {Box, Button, ColumnLayout, Container, Header, Link, Popover, SpaceBetween, StatusIndicator, Table} from "@cloudscape-design/components";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {AppContext} from "../../common";
import Utils from "../../common/utils";
import {ClusterSettingsClient} from "../../client";
import {GetModuleInfoResult} from "../../client/data-model";
import IdeaException from "../../common/exceptions";
import {Constants} from "../../common/constants";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";

export interface ClusterStatusProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface ClusterStatusState {
    instances: any
    instances_loading: boolean

    modules: any
    modules_loading: boolean

    // Sorting state for modules table
    modulesSortingColumn: string
    modulesSortingDescending: boolean

    // Sorting state for instances table
    instancesSortingColumn: string
    instancesSortingDescending: boolean
}

function getModuleInfo(module: string): Promise<GetModuleInfoResult> {
    if (module === Constants.MODULE_SCHEDULER) {
        return AppContext.get().client().scheduler().getModuleInfo().then(result => {
            return result
        })
    } else if (module === Constants.MODULE_CLUSTER_MANAGER) {
        return AppContext.get().client().clusterSettings().getModuleInfo().then(result => {
            return result
        })
    } else if (module === Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER) {
        return AppContext.get().client().virtualDesktop().getModuleInfo().then(result => {
            return result
        })
    }
    return Promise.reject(new IdeaException({
        errorCode: 'MODULE_NOT_FOUND',
        message: `Module not supported: ${module}`
    }))
}

interface ModuleHealthCheckProps {
    module: string
}

interface ModuleHeathCheckState {
    status: string
}

class ModuleHealthCheck extends Component<ModuleHealthCheckProps, ModuleHeathCheckState> {

    intervalRef: any

    constructor(props: ModuleHealthCheckProps) {
        super(props);
        this.state = {
            status: 'initial'
        }
    }

    componentDidMount() {
        this.performHealthCheck()
        this.intervalRef = setInterval(() => {
            this.performHealthCheck()
        }, 60000)
    }

    componentWillUnmount() {
        clearInterval(this.intervalRef)
    }

    performHealthCheck() {
        getModuleInfo(this.props.module).then(_ => {
            this.setState({
                status: 'success'
            })
        }).catch(_ => {
            this.setState({
                status: 'failed'
            })
        })
    }

    render() {
        if (this.state.status === 'initial') {
            return <StatusIndicator type="loading"/>
        } else if (this.state.status === 'success') {
            return <StatusIndicator type="success">Healthy</StatusIndicator>
        } else {
            return <StatusIndicator type="error">Failed</StatusIndicator>
        }
    }
}

interface ModuleVersionProps {
    module: string
}

interface ModuleVersionState {
    version: string
}

class ModuleVersion extends Component<ModuleVersionProps, ModuleVersionState> {

    constructor(props: ModuleVersionProps) {
        super(props);
        this.state = {
            version: '...'
        }
    }

    componentDidMount() {
        getModuleInfo(this.props.module).then(result => {
            this.setState({
                version: result.module?.module_version!
            })
        })
    }

    render() {
        return this.state.version
    }
}

interface ModuleTypeProps {
    module: any
}

function ModuleType(props: ModuleTypeProps) {
    let title = ''
    let description = ''
    switch (props.module.type) {
        case Constants.MODULE_TYPE_CONFIG:
            title = 'Config'
            description = 'Configuration modules exist only in form of configuration in cluster config DynamoDB table'
            break
        case Constants.MODULE_TYPE_STACK:
            title = 'Stack'
            description = 'Module of type stack provision AWS resources but do not expose any additional application specific APIs or business logic.'
            break
        case Constants.MODULE_TYPE_APP:
            title = 'App'
            description = 'Application modules provision applicable AWS resources and expose additional application specific functionality via APIs and/or web interface.'
            break
    }

    return <Box color="text-status-info">
        <Popover
            dismissAriaLabel="Close"
            header={`Module Type: ${title}`}
            content={description}
        >
            <StatusIndicator type="info" wrapText={false}>
                {title}
            </StatusIndicator>
        </Popover>
    </Box>
}

interface NodeTypeProps {
    node: any
}

function NodeType(props: NodeTypeProps) {
    let title = ''
    let description = ''
    switch (props.node.node_type) {
        case Constants.NODE_TYPE_APP:
            title = 'App'
            description = 'Application node serving IDEA APIs and related functionality'
            break
        case Constants.NODE_TYPE_INFRA:
            title = 'Infra'
            description = 'Infrastructure node serving complementary functionality for a module.'
            break
    }

    return <Box color="text-status-info">
        <Popover
            dismissAriaLabel="Close"
            header={`Node Type: ${title}`}
            content={description}
        >
            <StatusIndicator type="info" wrapText={false}>
                {title}
            </StatusIndicator>
        </Popover>
    </Box>
}

class ClusterStatus extends Component<ClusterStatusProps, ClusterStatusState> {
    constructor(props: ClusterStatusProps) {
        super(props);
        this.state = {
            instances: [],
            instances_loading: true,
            modules: [],
            modules_loading: true,
            modulesSortingColumn: 'type',
            modulesSortingDescending: false,
            instancesSortingColumn: 'instance_name',
            instancesSortingDescending: false
        }
    }

    getClusterSettingsClient(): ClusterSettingsClient {
        return AppContext.get().client().clusterSettings()
    }

    componentDidMount() {
        this.loadClusterHosts()
        this.loadClusterModules()
    }

    loadClusterModules() {
        this.setState({
            modules_loading: true
        }, () => {
            let modules: any = []
            this.getClusterSettingsClient().listClusterModules({}).then(result => {
                result.listing?.forEach(module => {
                    modules.push(module)
                })
                modules.sort((m1: any, m2: any) => m1.deployment_priority - m2.deployment_priority)

                // Apply initial sorting
                modules = this.sortModules(
                    modules,
                    this.state.modulesSortingColumn,
                    this.state.modulesSortingDescending
                );
            }).finally(() => {
                this.setState({
                    modules: modules,
                    modules_loading: false
                })
            })
        })
    }

    loadClusterHosts() {
        this.setState({
            instances_loading: true
        }, () => {
            const awsRegion = AppContext.get().auth().getAwsRegion()
            const instances: any = []
            this.getClusterSettingsClient().listClusterHosts({}).then(result => {

                result.listing?.forEach((host: any) => {

                    let instance: any = {}
                    host.Tags?.forEach((tag: any) => {
                        switch (tag.Key) {
                            case 'idea:ModuleName':
                                instance.module_name = tag.Value
                                break
                            case 'idea:ModuleVersion':
                                instance.module_version = tag.Value
                                break
                            case 'idea:ClusterName':
                                instance.cluster_name = tag.Value
                                break
                            case 'idea:ModuleId':
                                instance.module_id = tag.Value
                                break
                            case 'idea:NodeType':
                                instance.node_type = tag.Value
                                break
                            case 'Name':
                                instance.instance_name = tag.Value
                                break
                        }
                    })

                    instance.instance_id = host.InstanceId
                    instance.instance_type = host.InstanceType
                    instance.instance_state = host.State.Name
                    instance.availability_zone = host.Placement.AvailabilityZone
                    instance.subnet_id = host.SubnetId
                    instance.private_ip = host.PrivateIpAddress
                    instance.public_ip = host.PublicIpAddress
                    instance.ami_id = host.ImageId
                    instance.session_manager_url = Utils.getSessionManagerConnectionUrl(
                        awsRegion,
                        host.InstanceId
                    )

                    instances.push(instance)
                })

                // Apply initial sorting
                const sortedInstances = this.sortInstances(
                    instances,
                    this.state.instancesSortingColumn,
                    this.state.instancesSortingDescending
                );

                this.setState({
                    instances: sortedInstances,
                    instances_loading: false
                })
            }).catch(error => {
                this.setState({
                    instances_loading: false
                });
                console.error("Error loading cluster hosts:", error);
            })
        })
    }

    buildModuleInfo() {

        const getModuleSetIds = (moduleName: string): string[] => {
            return AppContext.get().getClusterSettingsService().getModuleSetIds(moduleName)
        }

        return (
            <ColumnLayout columns={1}>
                <Header variant="h2"
                        description="Cluster modules and status"
                        actions={<SpaceBetween size="xs" direction="horizontal">
                            <Button variant="normal"
                                    iconName="refresh"
                                    onClick={() => {
                                        this.loadClusterModules()
                                    }}
                            />
                        </SpaceBetween>}
                >
                    Modules
                </Header>
                <Table
                    items={this.state.modules}
                    loading={this.state.modules_loading}
                    loadingText="Retrieving cluster module information ..."
                    columnDefinitions={[
                        {
                            id: 'module-title',
                            header: 'Module',
                            cell: (item: any) => item.title,
                            sortingField: 'title'
                        },
                        {
                            id: 'module-id',
                            header: 'Module ID',
                            cell: (item: any) => item.module_id,
                            sortingField: 'module_id'
                        },
                        {
                            id: 'module-version',
                            header: 'Version',
                            cell: (item: any) => {
                                return Utils.asString(item.version, '-')
                            },
                            sortingField: 'version'
                        },
                        {
                            id: 'module-type',
                            header: 'Type',
                            cell: (item: any) => <ModuleType module={item}/>,
                            sortingField: 'type'
                        },
                        {
                            id: 'status',
                            header: 'Status',
                            cell: (item: any) => {
                                if (item.status === 'deployed') {
                                    return <StatusIndicator type="success">Deployed</StatusIndicator>
                                } else if (item.status === 'not-deployed') {
                                    return <StatusIndicator type="stopped">Not Deployed</StatusIndicator>
                                }
                            },
                            sortingField: 'status'
                        },
                        {
                            id: 'health_check',
                            header: 'API Health Check',
                            cell: (item: any) => {
                                if (item.type === Constants.MODULE_TYPE_APP) {
                                    /* If the module is not deployed by admin-choice - don't alarm the admin with red status */
                                    if (item.status === 'not-deployed') {
                                        return <StatusIndicator type={"stopped"}>Not Applicable</StatusIndicator>
                                    } else {
                                        return <ModuleHealthCheck module={item.name}/>
                                    }
                                } else {
                                    return <StatusIndicator type={"stopped"}>Not Applicable</StatusIndicator>
                                }
                            }
                        },
                        {
                            id: 'module-sets',
                            header: 'Module Sets',
                            cell: (item: any) => {
                                if (getModuleSetIds(item.name).length > 0) {
                                    return (
                                        <div>
                                            {
                                                getModuleSetIds(item.name).map((moduleSetId, index) => {
                                                    return <li key={index}>{moduleSetId}</li>
                                                })
                                            }
                                        </div>
                                    )
                                } else {
                                    return '-'
                                }
                            }
                        },
                    ]}
                    sortingColumn={{
                        sortingField: this.state.modulesSortingColumn
                    }}
                    sortingDescending={this.state.modulesSortingDescending}
                    onSortingChange={({detail}) => {
                        const newSortingColumn = detail.sortingColumn.sortingField as string;
                        const newSortingDescending = detail.isDescending ?? false;

                        // Sort the modules with the new sorting state
                        const sortedModules = this.sortModules(
                            this.state.modules,
                            newSortingColumn,
                            newSortingDescending
                        );

                        this.setState({
                            modules: sortedModules,
                            modulesSortingColumn: newSortingColumn,
                            modulesSortingDescending: newSortingDescending
                        });
                    }}
                />
            </ColumnLayout>
        )
    }

    buildHostInfo() {
        return (
            <ColumnLayout columns={1}>
                <Header variant="h2"
                        description="Cluster hosts and status"
                        actions={<SpaceBetween size="xs" direction="horizontal">
                            <Button variant="normal"
                                    iconName="refresh"
                                    onClick={() => {
                                        this.loadClusterHosts()
                                    }}
                            />
                        </SpaceBetween>}
                >
                    Infrastructure Hosts
                </Header>
                <Table
                    items={this.state.instances}
                    loading={this.state.instances_loading}
                    loadingText="Retrieving cluster host information ..."
                    columnDefinitions={[
                        {
                            id: 'instance-name',
                            header: 'Instance Name',
                            cell: (item: any) => item.instance_name,
                            sortingField: 'instance_name'
                        },
                        {
                            id: 'module-id',
                            header: 'Module ID',
                            cell: (item: any) => item.module_id,
                            sortingField: 'module_id'
                        },
                        {
                            id: 'node-type',
                            header: 'Node Type',
                            cell: (item: any) => <NodeType node={item}/>,
                            sortingField: 'node_type'
                        },
                        {
                            id: 'module-version',
                            header: 'Version',
                            cell: (item: any) => {
                                if (item.node_type === Constants.NODE_TYPE_INFRA) {
                                    return item.module_version
                                } else {
                                    return <ModuleVersion module={item.module_name}/>
                                }
                            },
                            sortingField: 'module_version'
                        },
                        {
                            id: 'instance-type',
                            header: 'Instance Type',
                            cell: (item: any) => item.instance_type,
                            sortingField: 'instance_type'
                        },
                        {
                            id: 'availability-zone',
                            header: 'Availability Zone',
                            cell: (item: any) => item.availability_zone,
                            sortingField: 'availability_zone'
                        },
                        {
                            id: 'instance-state',
                            header: 'Instance State',
                            cell: (item: any) => {
                                if (item.instance_state === 'running') {
                                    return <StatusIndicator type="success">Running</StatusIndicator>
                                } else if (item.instance_state === 'stopped') {
                                    return <StatusIndicator type="stopped">Stopped</StatusIndicator>
                                } else if (item.instance_state === 'terminated') {
                                    return <StatusIndicator type="stopped">Terminated</StatusIndicator>
                                } else {
                                    return <StatusIndicator type="in-progress" colorOverride="grey">{item.instance_state}</StatusIndicator>
                                }
                            },
                            sortingField: 'instance_state'
                        },
                        {
                            id: 'private-ip',
                            header: 'Private IP',
                            cell: (item: any) => (Utils.isEmpty(item.private_ip) ? '-' : item.private_ip),
                            sortingField: 'private_ip'
                        },
                        {
                            id: 'public-ip',
                            header: 'Public IP',
                            cell: (item: any) => (Utils.isEmpty(item.public_ip) ? '-' : item.public_ip),
                            sortingField: 'public_ip'
                        },
                        {
                            id: 'connect',
                            header: 'Connect',
                            cell: (item: any) => {
                                if (item.instance_state === 'running') {
                                    return <Link external={true} href={item.session_manager_url}>Connect</Link>
                                } else {
                                    return '--'
                                }
                            }
                        }
                    ]}
                    sortingColumn={{
                        sortingField: this.state.instancesSortingColumn
                    }}
                    sortingDescending={this.state.instancesSortingDescending}
                    onSortingChange={({detail}) => {
                        const newSortingColumn = detail.sortingColumn.sortingField as string;
                        const newSortingDescending = detail.isDescending ?? false;

                        // Sort the instances with the new sorting state
                        const sortedInstances = this.sortInstances(
                            this.state.instances,
                            newSortingColumn,
                            newSortingDescending
                        );

                        this.setState({
                            instances: sortedInstances,
                            instancesSortingColumn: newSortingColumn,
                            instancesSortingDescending: newSortingDescending
                        });
                    }}
                />
            </ColumnLayout>
        )
    }

    // Sort modules based on current sorting state
    sortModules(modules: any[], sortingColumn: string, isDescending: boolean): any[] {
        if (!modules || modules.length === 0) return [];

        // Create a copy to avoid mutating the original array
        const sortedModules = [...modules];

        sortedModules.sort((a, b) => {
            let valueA, valueB;

            // Get values based on sorting column
            switch (sortingColumn) {
                case 'title':
                case 'module_id':
                case 'version':
                    valueA = (a[sortingColumn] || '').toString().toLowerCase();
                    valueB = (b[sortingColumn] || '').toString().toLowerCase();
                    return isDescending
                        ? valueB.localeCompare(valueA)
                        : valueA.localeCompare(valueB);

                case 'type':
                    valueA = (a.type || '').toString().toLowerCase();
                    valueB = (b.type || '').toString().toLowerCase();
                    return isDescending
                        ? valueB.localeCompare(valueA)
                        : valueA.localeCompare(valueB);

                case 'status':
                    // Sort "deployed" before "not-deployed"
                    const statusOrder: Record<string, number> = {
                        'deployed': 1,
                        'not-deployed': 0
                    };
                    const statusA = a.status as string || '';
                    const statusB = b.status as string || '';
                    const result = (statusOrder[statusA] || 0) - (statusOrder[statusB] || 0);
                    return isDescending ? -result : result;

                default:
                    return 0;
            }
        });

        return sortedModules;
    }

    // Sort instances based on current sorting state
    sortInstances(instances: any[], sortingColumn: string, isDescending: boolean): any[] {
        if (!instances || instances.length === 0) return [];

        // Create a copy to avoid mutating the original array
        const sortedInstances = [...instances];

        sortedInstances.sort((a, b) => {
            let valueA, valueB;

            // Get values based on sorting column
            switch (sortingColumn) {
                case 'instance_name':
                case 'module_id':
                case 'module_version':
                case 'instance_type':
                case 'availability_zone':
                case 'private_ip':
                case 'public_ip':
                    valueA = (a[sortingColumn] || '').toString().toLowerCase();
                    valueB = (b[sortingColumn] || '').toString().toLowerCase();
                    return isDescending
                        ? valueB.localeCompare(valueA)
                        : valueA.localeCompare(valueB);

                case 'node_type':
                    valueA = (a.node_type || '').toString().toLowerCase();
                    valueB = (b.node_type || '').toString().toLowerCase();
                    return isDescending
                        ? valueB.localeCompare(valueA)
                        : valueA.localeCompare(valueB);

                case 'instance_state':
                    // Order: running > others > stopped > terminated
                    const stateOrder: Record<string, number> = {
                        'running': 3,
                        'stopped': 1,
                        'terminated': 0
                    };
                    const stateA = a.instance_state as string || '';
                    const stateB = b.instance_state as string || '';
                    // Default to 2 for other states to position them between running and stopped
                    const result = (stateOrder[stateA] ?? 2) - (stateOrder[stateB] ?? 2);
                    return isDescending ? -result : result;

                default:
                    return 0;
            }
        });

        return sortedInstances;
    }

    render() {
        return (
            <IdeaAppLayout
                ideaPageId={this.props.ideaPageId}
                tools={this.props.tools}
                toolsOpen={this.props.toolsOpen}
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
                        text: 'Cluster Management',
                        href: '#/cluster/status'
                    },
                    {
                        text: 'Status',
                        href: ''
                    }
                ]}
                header={(
                    <Header variant={"h1"}
                            actions={(<SpaceBetween size={"s"}>
                                <Button variant={"primary"} onClick={() => this.props.navigate('/cluster/settings')}>View Cluster Settings</Button>
                            </SpaceBetween>)}>
                        Cluster Status
                    </Header>
                )}
                contentType={"default"}
                content={
                    <SpaceBetween size="xxl">
                        <Container>
                            {this.buildModuleInfo()}
                        </Container>
                        <Container>
                            {this.buildHostInfo()}
                        </Container>
                    </SpaceBetween>
                }/>
        )
    }
}

export default withRouter(ClusterStatus)
