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

import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {Box, Button, ButtonDropdown, ColumnLayout, Container, Header, SpaceBetween, StatusIndicator, Table, Tabs} from "@cloudscape-design/components";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {KeyValue, KeyValueGroup} from "../../components/key-value";
import {CopyToClipBoard, ProjectBedrockModels} from "../../components/common";
import {AuthClient, ProjectsClient} from "../../client";
import {AppContext} from "../../common";
import {AuthService} from "../../service";
import {User, ListUsersInGroupResult, Project} from "../../client/data-model";
import IdeaForm from "../../components/form";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";
import Utils from "../../common/utils";
import dot from "dot-object";
import {Constants} from "../../common/constants";

export interface AccountSettingsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface AccountSettingsState {
    user: User | null
    usersInGroup: User[] | null
    projects: Project[] | null
    projectsError: string | null
    bedrockEnabled: boolean
}

const MY_PROJECTS_TABLE_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<Project>[] = [
    {
        id: 'title',
        header: 'Title',
        cell: project => project.title
    },
    {
        id: 'name',
        header: 'Project Code',
        cell: project => project.name
    },
    {
        id: 'description',
        header: 'Description',
        cell: project => project.description || '-'
    },
    {
        id: 'enabled',
        header: 'Status',
        cell: project => (project.enabled) ? <StatusIndicator type="success">Enabled</StatusIndicator> :
            <StatusIndicator type="stopped">Disabled</StatusIndicator>
    },
    {
        id: 'ldap-groups',
        header: 'Groups',
        cell: project => (project.ldap_groups && project.ldap_groups.length > 0) ? project.ldap_groups.join(', ') : '-'
    }
]

// shown only when bedrock.enabled. Renders the project model list with the same
// component as the script workbench.
const MY_PROJECTS_BEDROCK_COLUMN_DEFINITION: TableProps.ColumnDefinition<Project> = {
    id: 'bedrock-models',
    header: 'AI Models',
    minWidth: 260,
    cell: project => <ProjectBedrockModels project={project}/>
}

class AccountSettings extends Component<AccountSettingsProps, AccountSettingsState> {

    changePasswordForm: RefObject<IdeaForm | null>
    addUserToGroupForm: RefObject<IdeaForm | null>
    removeUserFromGroupForm: RefObject<IdeaForm | null>

    constructor(props: AccountSettingsProps) {
        super(props);
        this.changePasswordForm = React.createRef()
        this.addUserToGroupForm = React.createRef()
        this.removeUserFromGroupForm = React.createRef()
        this.state = {
            user: null,
            usersInGroup: null,
            projects: null,
            projectsError: null,
            bedrockEnabled: false
        }
    }

    componentDidMount() {
        this.fetchUser().then(() => {
            this.fetchUsersInGroup().finally()
        })
        this.fetchProjects().finally()
        AppContext.get().getClusterSettingsService().getModuleSettings(Constants.MODULE_CLUSTER_MANAGER).then(settings => {
            this.setState({
                bedrockEnabled: Utils.asBoolean(dot.pick('bedrock.enabled', settings), false)
            })
        }).catch(error => {
            console.error(error)
        })
    }

    getAuthService(): AuthService {
        return AppContext.get().auth()
    }

    getAuthClient(): AuthClient {
        return AppContext.get().client().auth()
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getChangePasswordForm(): IdeaForm {
        return this.changePasswordForm.current!
    }

    getAddUserToGroupForm(): IdeaForm {
        return this.addUserToGroupForm.current!
    }

    getRemoveUserFromGroupForm(): IdeaForm {
        return this.removeUserFromGroupForm.current!
    }

    fetchUser(): Promise<boolean> {
        return new Promise<boolean>((resolve, reject) => {
            this.getAuthService().getUser().then(user => {
                this.setState({
                    user: user
                }, () => {
                    resolve(true)
                })
            }).catch(e => {
                console.error(e)
                reject(false)
            })
        })
    }

    fetchProjects(): Promise<boolean> {
        return new Promise<boolean>((resolve) => {
            this.getProjectsClient().getUserProjects({
                username: AppContext.get().auth().getUsername()
            }).then(result => {
                this.setState({
                    projects: result.projects ?? [],
                    projectsError: null
                }, () => {
                    resolve(true)
                })
            }).catch(e => {
                // null projects would leave the tab loading forever; record the error so the empty slot
                // can tell a failed fetch apart from a user who genuinely belongs to no projects.
                this.setState({
                    projects: [],
                    projectsError: e?.message ?? `${e}`
                }, () => {
                    this.props.onFlashbarChange({
                        items: [
                            {
                                type: 'error',
                                header: 'Failed to load your projects',
                                content: e?.message ?? `${e}`,
                                dismissible: true
                            }
                        ]
                    })
                    resolve(false)
                })
            })
        })
    }

    async fetchUsersInGroup() {
        let allUsers: User[] = []
        let cursor: string | undefined = undefined

        // Fetch all pages of users using cursor-based pagination
        do {
            try {
                const response: ListUsersInGroupResult = await this.getAuthClient().listUsersInGroup({
                    group_names: [this.state.user?.group_name!],
                    paginator: {
                        page_size: 1000,
                        cursor: cursor
                    }
                })

                // Add users from this page
                if (response.listing) {
                    allUsers.push(...response.listing)
                }

                // Get cursor for next page
                cursor = response.paginator?.cursor
            } catch (error) {
                console.error('Error fetching users in group:', error)
                break
            }
        } while (cursor) // Continue while there are more pages

        this.setState({
            usersInGroup: allUsers
        })
        return true
    }

    buildAddUserToGroupForm() {
        return <IdeaForm
            ref={this.addUserToGroupForm}
            name="add-user-to-group"
            title="Add Users to your Group"
            modal={true}
            modalSize="medium"
            onSubmit={() => {
                if (!this.getAddUserToGroupForm().validate()) {
                    return Promise.resolve(false)
                }
                let values: any = this.getAddUserToGroupForm().getValues()
                return this.getAuthClient().addUserToGroup(values).then(() => {
                    return this.fetchUsersInGroup().then(() => {
                        this.getAddUserToGroupForm().hideModal()
                        return true
                    })
                }).catch(error => {
                    this.getAddUserToGroupForm().setError(error.errorCode, error.message)
                    return false
                })
            }}
            params={[
                {
                    name: 'usernames',
                    title: 'Username',
                    description: 'Enter the usernames of the user you want to add to your group',
                    multiple: true,
                    validate: {
                        required: true
                    }
                }
            ]}/>
    }

    buildRemoveUserFromGroupForm() {
        return <IdeaForm
            ref={this.removeUserFromGroupForm}
            name="remove-user-from-group"
            title="Remove Users from your Group"
            modal={true}
            modalSize="medium"
            onSubmit={() => {
                if (!this.getRemoveUserFromGroupForm().validate()) {
                    return Promise.resolve(false)
                }
                let values: any = this.getRemoveUserFromGroupForm().getValues()
                return this.getAuthClient().removeUserFromGroup(values).then(() => {
                    return this.fetchUsersInGroup().then(() => {
                        this.getRemoveUserFromGroupForm().hideModal()
                        return true
                    })
                }).catch(error => {
                    this.getRemoveUserFromGroupForm().setError(error.errorCode, error.message)
                    return false
                })
            }}
            params={[
                {
                    name: 'usernames',
                    title: 'Username',
                    description: 'Enter the usernames of the user you want to remove from your group',
                    multiple: true,
                    validate: {
                        required: true
                    }
                }
            ]}/>
    }

    buildChangePasswordForm() {
        return <IdeaForm
            ref={this.changePasswordForm}
            name="change-password"
            title="Change Password"
            modal={true}
            modalSize="medium"
            onSubmit={() => {
                if (!this.getChangePasswordForm().validate()) {
                    return Promise.resolve(false)
                }
                let values: any = this.getChangePasswordForm().getValues()

                return this.getAuthClient().changePassword(values).then(() => {
                    return this.getAuthClient().globalSignOut({}).then(() => {
                        this.getChangePasswordForm().hideModal()
                        return AppContext.get().auth().logout().then(() => {
                            return true
                        })
                    }).catch(error => {
                        this.getChangePasswordForm().setError(error.errorCode, error.message)
                        return false
                    })
                }).catch(error => {
                    this.getChangePasswordForm().setError(error.errorCode, error.message)
                    return false
                })
            }}
            params={[
                {
                    name: 'old_password',
                    title: 'Old Password',
                    description: 'Enter your current password',
                    param_type: 'password',
                    data_type: 'str',
                    validate: {
                        required: true
                    }
                },
                {
                    name: 'new_password',
                    title: 'New Password',
                    description: 'Enter your new password',
                    param_type: 'new-password',
                    data_type: 'str',
                    validate: {
                        required: true
                    }
                }
            ]}/>
    }

    render() {

        const isPasswordRotationApplicable = () => {
            return AppContext.get().auth().isPasswordExpirationApplicable()
        }

        const getPasswordExpiresIn = () => {
            const expiresIn = AppContext.get().auth().getPasswordExpiresInDays()
            if (expiresIn > 0) {
                if (expiresIn > 1) {
                    return `${expiresIn} days`
                } else {
                    return `1 day`
                }
            }
            return 'Password Expired'
        }

        const getUsersInGroup = () => {
            const result: string[] = []
            if (this.state.usersInGroup) {
                this.state.usersInGroup.forEach(user => {
                    result.push(user.username!)
                })
            }
            return result
        }

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
                        text: 'Account Settings',
                        href: '#'
                    }
                ]}
                header={
                    <Header
                        variant="h1"
                        actions={
                            <SpaceBetween size="s" direction="horizontal">
                                <ButtonDropdown
                                    onItemClick={(event) => {
                                        if (event.detail.id === 'add-user-to-group') {
                                            this.getAddUserToGroupForm().showModal()
                                        } else if (event.detail.id === 'remove-user-from-group') {
                                            this.getRemoveUserFromGroupForm().showModal()
                                        }
                                    }}
                                    items={[
                                        {
                                            id: 'add-user-to-group',
                                            text: 'Add User to my Group'
                                        },
                                        {
                                            id: 'remove-user-from-group',
                                            text: 'Remove User from my Group'
                                        }
                                    ]}>Actions</ButtonDropdown>
                                <Button variant="primary" onClick={() => {
                                    this.getChangePasswordForm().showModal()
                                }}>Change Password</Button>
                            </SpaceBetween>
                        }
                    > Account Settings</Header>}
                contentType={"default"}
                content={
                    <div>
                        {this.buildChangePasswordForm()}
                        {this.buildAddUserToGroupForm()}
                        {this.buildRemoveUserFromGroupForm()}
                        <ColumnLayout columns={1}>
                            <Container>
                                <Tabs
                                    tabs={[
                                        {
                                            id: 'profile',
                                            label: 'My Profile',
                                            content: (
                                                this.state.user &&
                                                <ColumnLayout columns={2}>
                                                    <KeyValueGroup title="My Profile">
                                                        <KeyValue title="Username" value={this.state.user?.username}/>
                                                        <KeyValue title="Email" value={this.state.user?.email}/>
                                                        <KeyValue title="Home Directory" value={this.state.user?.home_dir}/>
                                                        <KeyValue title="Login Shell" value={this.state.user?.login_shell}/>
                                                        <KeyValue title="Is Administrator?" value={this.state.user?.sudo} type="boolean"/>
                                                        <KeyValue title="Created On" value={new Date(this.state.user?.created_on!).toLocaleString()}/>
                                                        {isPasswordRotationApplicable() && <KeyValue title="Password Expires In" value={getPasswordExpiresIn()}/>}
                                                    </KeyValueGroup>
                                                    <KeyValueGroup title="LDAP Info">
                                                        <KeyValue title="UID" value={this.state.user?.uid}/>
                                                        <KeyValue title="GID" value={this.state.user?.gid}/>
                                                        <KeyValue title="Group Name" value={this.state.user.group_name}/>
                                                        <KeyValue title="Additional users in my group" value={getUsersInGroup()}/>
                                                    </KeyValueGroup>
                                                </ColumnLayout>
                                            )
                                        },
                                        {
                                            id: 'projects',
                                            label: 'My Projects',
                                            content: (
                                                <Table
                                                    variant="embedded"
                                                    items={this.state.projects ?? []}
                                                    loading={this.state.projects === null}
                                                    loadingText="Retrieving your projects ..."
                                                    columnDefinitions={(this.state.bedrockEnabled) ? [...MY_PROJECTS_TABLE_COLUMN_DEFINITIONS, MY_PROJECTS_BEDROCK_COLUMN_DEFINITION] : MY_PROJECTS_TABLE_COLUMN_DEFINITIONS}
                                                    empty={this.state.projectsError
                                                        ? <Box textAlign="center" color="inherit">
                                                            <b>Could not load your projects</b>
                                                            <Box variant="p" color="inherit">{this.state.projectsError}</Box>
                                                            <Button onClick={() => this.setState({projects: null, projectsError: null}, () => this.fetchProjects().finally())}>Retry</Button>
                                                        </Box>
                                                        : <Box textAlign="center" color="inherit">
                                                            <b>No projects</b>
                                                            <Box variant="p" color="inherit">You are not a member of any project.</Box>
                                                        </Box>}
                                                />
                                            )
                                        }
                                    ]}
                                />
                            </Container>
                        </ColumnLayout>
                    </div>
                }/>
        )
    }
}

export default withRouter(AccountSettings)
