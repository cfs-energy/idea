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

import {SideNavigationProps} from "@cloudscape-design/components";
import {AppContext} from "../common";
import {Constants} from "../common/constants";

export const IdeaSideNavHeader = (context: AppContext): SideNavigationProps.Header => {
    return {
        text: context.getSubtitle(),
        href: '#/'
    }
}

export const IdeaSideNavItems = (context: AppContext): SideNavigationProps.Item[] => {
    const result: SideNavigationProps.Item[] = []
    const adminNavItems: SideNavigationProps.Item[] = []

    const userNav: any = {
        type: 'section',
        text: 'Home',
        defaultExpanded: true,
        items: []
    }
    result.push(userNav)

    if (context.getClusterSettingsService().isVirtualDesktopDeployed() && context.auth().hasModuleAccess(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)) {
        userNav.items.push({
            type: 'link',
            text: 'Virtual Desktops',
            href: '#/home/virtual-desktops'
        })
        userNav.items.push({
            type: 'link',
            text: 'Shared Desktops',
            href: '#/home/shared-desktops'
        })
    }
    if (context.getClusterSettingsService().isSchedulerDeployed() && context.auth().hasModuleAccess(Constants.MODULE_SCHEDULER)) {
        userNav.items.push({
            type: 'link',
            text: 'Active Jobs',
            href: '#/home/active-jobs'
        })
        userNav.items.push({
            type: 'link',
            text: 'Completed Jobs',
            href: '#/home/completed-jobs'
        })
    }

    if (context.auth().hasModuleAccess(Constants.MODULE_CLUSTER_MANAGER)) {
        userNav.items.push({
            type: 'link',
            text: 'File Browser',
            href: '#/home/file-browser'
        })
        if (context.getClusterSettingsService().isBastionHostDeployed()) {
            userNav.items.push({
                type: 'link',
                text: 'SSH Access',
                href: '#/home/ssh-access'
            })
        }
    }

    // start admin section

    adminNavItems.push({
        type: 'divider'
    })

    adminNavItems.push({
        type: 'link',
        text: Constants.ADMIN_ZONE_LINK_TEXT,
        href: '#'
    })

    if (context.getClusterSettingsService().isVirtualDesktopDeployed() && context.auth().isModuleAdmin(Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)) {
        adminNavItems.push({
            type: 'section',
            text: 'eVDI',
            defaultExpanded: true,
            items: [
                {
                    type: 'link',
                    text: 'Dashboard',
                    href: '#/virtual-desktop/dashboard'
                },
                {
                    type: 'link',
                    text: 'Sessions',
                    href: '#/virtual-desktop/sessions'
                },
                {
                    type: 'link',
                    text: 'Software Stacks (AMIs)',
                    href: '#/virtual-desktop/software-stacks'
                },
                {
                    type: 'link',
                    text: 'Permission Profiles',
                    href: '#/virtual-desktop/permission-profiles'
                },
                {
                    type: 'link',
                    text: 'Debug',
                    href: '#/virtual-desktop/debug'
                },
                {
                    type: 'link',
                    text: 'Settings',
                    href: '#/virtual-desktop/settings'
                }
            ]
        })
    }

    if (context.getClusterSettingsService().isSchedulerDeployed() && context.auth().isModuleAdmin(Constants.MODULE_SCHEDULER)) {
        const schedulerAdminSection: any = {
            type: 'section',
            text: 'Scale-Out Computing',
            defaultExpanded: true,
            items: [
                {
                    type: 'link',
                    text: 'Queue Profiles',
                    href: '#/soca/queues'
                },
                {
                    type: 'link',
                    text: 'Applications',
                    href: '#/soca/applications'
                },
                {
                    type: 'link',
                    text: 'Licenses',
                    href: '#/soca/licenses'
                },
                {
                    type: 'link',
                    text: 'Active Jobs',
                    href: '#/soca/active-jobs'
                },
                {
                    type: 'link',
                    text: 'Completed Jobs',
                    href: '#/soca/completed-jobs'
                },
                {
                    type: 'link',
                    text: 'Settings',
                    href: '#/soca/settings'
                }
            ]
        }
        adminNavItems.push(schedulerAdminSection)
    }

    if (context.auth().isModuleAdmin(Constants.MODULE_CLUSTER_MANAGER)) {
        adminNavItems.push({
            type: 'section',
            text: 'Cluster Management',
            defaultExpanded: false,
            items: [
                {
                    type: 'link',
                    text: 'Projects',
                    href: '#/cluster/projects'
                },
                {
                    type: 'link',
                    text: 'Users',
                    href: '#/cluster/users'
                },
                {
                    type: 'link',
                    text: 'Groups',
                    href: '#/cluster/groups'
                },
                {
                    type: 'link',
                    text: 'Cluster Status',
                    href: '#/cluster/status'
                },
                {
                    type: 'link',
                    text: 'Email Templates',
                    href: '#/cluster/email-templates'
                },
                {
                    type: 'link',
                    text: 'Settings',
                    href: '#/cluster/settings'
                }
            ]
        })
    }

    // ignore divider and admin-zone text
    if (adminNavItems.length > 2) {
        adminNavItems.forEach((item) => {
            result.push(item)
        })
    }

    return result
}
