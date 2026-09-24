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
import {PORTAL_TASKS, permittedViews} from './task-navigation';

export const IdeaSideNavItems = (context: AppContext): SideNavigationProps.Item[] => {
    const user: SideNavigationProps.Item[] = [];
    const admin: SideNavigationProps.Item[] = [];
    for (const task of PORTAL_TASKS) {
        const views = permittedViews(task, context);
        if (!views.length) continue;
        if (task.id === 'ssh-access' && !context.getClusterSettingsService().isBastionHostDeployed()) continue;
        if (task.id === 'reports' && !context.getClusterSettingsService().isCustomDashboardEnabled()) continue;
        (task.admin ? admin : user).push({type: 'link', text: task.title, href: `#${views[0].path}`});
    }
    if (admin.length) user.push({type: 'section', text: 'Administration', defaultExpanded: true, items: admin});
    return user;
};

export const IdeaSideNavHeader = (context: AppContext): SideNavigationProps.Header => ({
    text: context.getSubtitle(),
    href: IdeaSideNavItems(context).find(item => item.type === 'link')?.href ?? '#/'
});
