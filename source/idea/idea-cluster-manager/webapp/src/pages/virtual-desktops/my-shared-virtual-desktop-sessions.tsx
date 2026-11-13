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

import {
    Link
} from "@cloudscape-design/components";
import {AppContext} from "../../common";
import {
    VirtualDesktopSessionPermission
} from '../../client/data-model'
import {VirtualDesktopClient} from '../../client';
import Utils from "../../common/utils";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import 'moment-timezone';
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {TableProps} from "@cloudscape-design/components/table";
import IdeaListView from "../../components/list-view";
import VirtualDesktopSessionStatusIndicator from "./components/virtual-desktop-session-status-indicator";
import {withRouter} from "../../navigation/navigation-utils";
import {downloadDcvFile, getConnectionInfo, openInNewTab, POPUP_BLOCKED_MESSAGE} from "./desktop-connection";

export interface MySharedVirtualDesktopProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface MySharedVirtualDesktopsState {

}

class MySharedVirtualDesktopSessions extends Component<MySharedVirtualDesktopProps, MySharedVirtualDesktopsState> {
    VIRTUAL_DESKTOP_SHARED_SESSIONS_TABLE_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<VirtualDesktopSessionPermission>[] = [
        {
            id: 'name',
            header: 'Name',
            cell: e => e.idea_session_name,
            sortingField: 'idea_session_name'
        },
        {
            id: 'owner',
            header: 'Owner',
            cell: e => e.idea_session_owner,
            sortingField: 'idea_session_owner'
        },
        {
            id: 'base_os',
            header: 'Operating system',
            cell: e => Utils.getOsTitle(e.idea_session_base_os),
            sortingComparator: (a, b) => {
                const valueA = a.idea_session_base_os || '';
                const valueB = b.idea_session_base_os || '';
                return valueA.localeCompare(valueB);
            }
        },
        {
            id: 'instance_type',
            header: 'Size',
            cell: e => e.idea_session_instance_type,
            sortingField: 'idea_session_instance_type'
        },
        {
            id: 'state',
            header: 'State',
            cell: e => <VirtualDesktopSessionStatusIndicator state={e.idea_session_state!} hibernation_enabled={e.idea_session_hibernation_enabled!}/>,
            sortingField: 'idea_session_state'
        },
        {
            id: 'expiry',
            header: 'Access ends',
            cell: e => new Date(e.expiry_date!).toLocaleString(),
            sortingComparator: (a, b) => {
                const dateA = a.expiry_date ? new Date(a.expiry_date).getTime() : 0;
                const dateB = b.expiry_date ? new Date(b.expiry_date).getTime() : 0;
                return dateA - dateB;
            }
        },
        {
            id: 'connect-session',
            header: 'Connect',
            cell: e => {
                if (e.idea_session_state !== 'READY') {
                    return `Not running. Ask ${e.idea_session_owner} to start it.`
                }
                return <Link external onFollow={() => this.onConnect(e)}>Connect</Link>
            }
        },
        {
            id: 'download-dcv-file',
            header: 'DCV client file',
            cell: e => e.idea_session_state === 'READY' && <Link onFollow={() => this.onDownloadDcvSessionFile(e)}>Download</Link>
        }
    ]
    listing: RefObject<IdeaListView | null>

    constructor(props: MySharedVirtualDesktopProps) {
        super(props);
        this.listing = React.createRef()
    }

    private getInfo(permission: VirtualDesktopSessionPermission) {
        return getConnectionInfo(this.getVirtualDesktopClient(), {
            idea_session_id: permission.idea_session_id,
            idea_session_owner: permission.idea_session_owner,
            username: AppContext.get().auth().getUsername()
        })
    }

    private showError(content: string) {
        this.props.onFlashbarChange({
            items: [{content, type: 'error', dismissible: true}]
        })
    }

    onConnect = (permission: VirtualDesktopSessionPermission) => {
        this.getInfo(permission).then(info => {
            if (!openInNewTab(info)) {
                this.showError(POPUP_BLOCKED_MESSAGE)
            }
        }).catch(error => {
            this.showError(`Could not connect to ${permission.idea_session_name}: ${error?.message ?? 'Try again, or ask the owner.'}`)
        })
    }

    onDownloadDcvSessionFile = (permission: VirtualDesktopSessionPermission) => {
        this.getInfo(permission).then(info => {
            downloadDcvFile(info, `Shared_Desktop_${permission.idea_session_name}`)
        }).catch(error => {
            this.showError(`Could not download the DCV client file for ${permission.idea_session_name}: ${error?.message ?? 'Try again, or ask the owner.'}`)
        })
    }

    getListing(): IdeaListView {
        return this.listing.current!
    }

    getVirtualDesktopClient(): VirtualDesktopClient {
        return AppContext.get().client().virtualDesktop()
    }

    buildListing() {
        return (
            <IdeaListView
                ref={this.listing}
                title="My desktops"
                showPreferences={true}
                preferencesKey={'shared-desktops'}
                description="Desktops shared with you"
                showPaginator={true}
                onRefresh={() => {
                    this.getListing().fetchRecords()
                }}
                showFilters={true}
                filterType="select"
                selectFilters={[
                    {
                        name: '$all',
                    },
                    {
                        name: 'idea_session_state',
                        choices: [
                            {
                                title: 'All States',
                                value: ''
                            },
                            {
                                title: 'Ready',
                                value: 'READY'
                            },
                            {
                                title: 'Setting up: provisioning',
                                value: 'PROVISIONING'
                            },
                            {
                                title: 'Setting up: creating',
                                value: 'CREATING'
                            },
                            {
                                title: 'Setting up: initializing',
                                value: 'INITIALIZING'
                            },
                            {
                                title: 'Starting',
                                value: 'RESUMING'
                            },
                            {
                                title: 'Stopping',
                                value: 'STOPPING'
                            },
                            {
                                title: 'Stopped',
                                value: 'STOPPED'
                            },
                            {
                                title: 'Terminating',
                                value: 'DELETING'
                            },
                            {
                                title: 'Failed',
                                value: 'ERROR'
                            }
                        ]
                    },
                    {
                        name: 'idea_session_base_os',
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
                                title: 'RHEL 8',
                                value: 'rhel8'
                            },
                            {
                                title: 'RHEL 9',
                                value: 'rhel9'
                            },
                            {
                                title: 'RHEL 10',
                                value: 'rhel10'
                            },
                            {
                                title: 'Rocky 8',
                                value: 'rocky8'
                            },
                            {
                                title: 'Rocky 9',
                                value: 'rocky9'
                            },
                            {
                                title: 'Rocky 10',
                                value: 'rocky10'
                            },
                            {
                                title: 'Ubuntu 22.04',
                                value: 'ubuntu2204'
                            },
                            {
                                title: 'Ubuntu 24.04',
                                value: 'ubuntu2404'
                            },
                            {
                                title: 'Ubuntu 26.04',
                                value: 'ubuntu2604'
                            }
                        ]
                    }
                ]}
                onFilter={(filters) => {
                    return filters
                }}
                onFetchRecords={() => {
                    return this.getVirtualDesktopClient()
                        .listSharedPermissions({
                            filters: this.getListing().getFilters(),
                            paginator: this.getListing().getPaginator()
                        })
                }}
                columnDefinitions={this.VIRTUAL_DESKTOP_SHARED_SESSIONS_TABLE_COLUMN_DEFINITIONS}
                defaultSortingColumn="expiry"
                defaultSortingDescending={false}
            />
        )
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
                        text: 'My desktops',
                        href: ''
                    }
                ]}
                content={
                    <div>
                        {this.buildListing()}
                    </div>
                }/>
        )
    }
}

export default withRouter(MySharedVirtualDesktopSessions)
