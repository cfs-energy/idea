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

import {AppContext} from "../../common";
import IdeaForm from "../../components/form";
import IdeaListView from "../../components/list-view";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {GetParamChoicesRequest, GetParamChoicesResult, SocaFilter, SocaUserInputChoice, User} from "../../client/data-model";
import {AccountsClient} from "../../client";
import Utils from "../../common/utils";
import IdeaConfirm from "../../components/modals";
import {StatusIndicator} from "@cloudscape-design/components";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";

export interface UsersProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface UsersState {
    userSelected: boolean
    // these are added so that the dynamic options are not loaded as soon as the view is rendered.
    // dynamic options will be loaded only when the modal is displayed
    // the create user form does not have any dynamic options and hence not required.
    showAddUserToGroupForm: boolean
    showRemoveUserFromGroupForm: boolean
}

export const USER_TABLE_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<User>[] = [
    {
        id: 'username',
        header: 'Username',
        cell: e => e.username
    },
    {
        id: 'uid',
        header: 'UID',
        cell: e => e.uid
    },
    {
        id: 'gid',
        header: 'GID',
        cell: e => e.gid
    },
    {
        id: 'email',
        header: 'Email',
        cell: e => e.email
    },
    {
        id: 'sudo',
        header: 'Is Admin?',
        cell: e => (e.sudo) ? 'Yes' : 'No'
    },
    {
        id: 'enabled',
        header: 'Status',
        cell: e => (e.enabled) ? <StatusIndicator type="success">Enabled</StatusIndicator> :
            <StatusIndicator type="stopped">Disabled</StatusIndicator>
    },
    {
        id: 'groups',
        header: 'Groups',
        cell: (user) => {
            if (user.additional_groups) {
                return (
                    <div>
                        {
                            user.additional_groups.map((group, index) => {
                                return <li key={index}>{group}</li>
                            })
                        }
                    </div>
                )
            } else {
                return '-'
            }
        }
    },
    {
        id: 'created_on',
        header: 'Created On',
        cell: e => new Date(e.created_on!).toLocaleString()
    }
]

class Users extends Component<UsersProps, UsersState> {

    createUserForm: RefObject<IdeaForm>
    addUserToGroupForm: RefObject<IdeaForm>
    removeUserFromGroupForm: RefObject<IdeaForm>
    toggleAdminUserConfirmModal: RefObject<IdeaConfirm>
    toggleUserEnabledConfirmModal: RefObject<IdeaConfirm>
    resetPasswordConfirmModal: RefObject<IdeaConfirm>
    listing: RefObject<IdeaListView>

    constructor(props: UsersProps) {
        super(props);
        this.createUserForm = React.createRef()
        this.addUserToGroupForm = React.createRef()
        this.removeUserFromGroupForm = React.createRef()
        this.toggleAdminUserConfirmModal = React.createRef()
        this.toggleUserEnabledConfirmModal = React.createRef()
        this.resetPasswordConfirmModal = React.createRef()
        this.listing = React.createRef()
        this.state = {
            userSelected: false,
            showAddUserToGroupForm: false,
            showRemoveUserFromGroupForm: false
        }
    }

    authAdmin(): AccountsClient {
        return AppContext.get().client().accounts()
    }

    getCreateUserForm(): IdeaForm {
        return this.createUserForm.current!
    }

    getAddUserToGroupForm(): IdeaForm {
        return this.addUserToGroupForm.current!
    }

    getRemoveUserFromGroupForm(): IdeaForm {
        return this.removeUserFromGroupForm.current!
    }

    getToggleAdminUserConfirmModal(): IdeaConfirm {
        return this.toggleAdminUserConfirmModal.current!
    }

    getToggleUserEnabledConfirmModal(): IdeaConfirm {
        return this.toggleUserEnabledConfirmModal.current!
    }

    getResetPasswordConfirmModal(): IdeaConfirm {
        return this.resetPasswordConfirmModal.current!
    }

    getListing(): IdeaListView {
        return this.listing.current!
    }


    buildCreateUserForm() {

        const getPermissionChoices = () => {
            const result = [
                {
                    title: 'Administrator',
                    description: 'Administrators have access to all modules + root access to the file system',
                    value: 'cluster:administrators'
                },
                {
                    title: 'Manager',
                    description: 'Managers are like administrators, but without root access to file system',
                    value: 'cluster:managers'
                },
                {
                    title: 'Module Administrator (cluster-manager)',
                    description: 'Administrators of ModuleId: cluster-manager',
                    value: 'module:administrators:cluster-manager'
                },
                {
                    title: 'Module User (cluster-manager)',
                    description: 'Users of ModuleId: cluster-manager',
                    value: 'module:users:cluster-manager'
                }
            ]

            const clusterSettingsService = AppContext.get().getClusterSettingsService()
            if (clusterSettingsService.isSchedulerDeployed()) {
                result.push(...[
                    {
                        title: 'Module Administrator (Scale-Out Computing)',
                        description: 'Administrators of ModuleId: scheduler',
                        value: 'module:administrators:scheduler'
                    },
                    {
                        title: 'Module User (Scale-Out Computing)',
                        description: 'Users of ModuleId: scheduler',
                        value: 'module:users:scheduler'
                    }
                ])
            }
            if (clusterSettingsService.isVirtualDesktopDeployed()) {
                result.push(...[
                    {
                        title: 'Module Administrator (eVDI)',
                        description: 'Administrators of ModuleId: vdc',
                        value: 'module:administrators:vdc'
                    },
                    {
                        title: 'Module User (eVDI)',
                        description: 'Users of ModuleId: vdc',
                        value: 'module:users:vdc'
                    }
                ])
            }

            return result
        }

        return (
            <IdeaForm ref={this.createUserForm}
                      name="create-user"
                      modal={true}
                      title="Create New User"
                      onSubmit={() => {
                          if (!this.getCreateUserForm().validate()) {
                              return
                          }
                          const values = this.getCreateUserForm().getValues()

                          let sudo = false
                          const additional_groups: string[] = []
                          values.permissions.forEach((permission: string) => {
                              switch (permission) {
                                  case 'cluster:administrators':
                                      sudo = true
                                      break
                                  case 'cluster:managers':
                                      additional_groups.push('managers-cluster-group')
                                      break
                                  default:
                                      const tokens = permission.split(':')
                                      if (tokens.length === 3 && tokens[0] === 'module') {
                                          const moduleId = tokens[2]
                                          const moduleAdmin = tokens[1] === 'administrators'
                                          if (moduleAdmin) {
                                              additional_groups.push(`${moduleId}-administrators-module-group`)
                                          } else {
                                              additional_groups.push(`${moduleId}-users-module-group`)
                                          }
                                      }
                              }
                          })

                          this.authAdmin().createUser({
                              user: {
                                  username: values.username,
                                  email: values.email,
                                  sudo: sudo,
                                  password: values.password,
                                  login_shell: values.login_shell,
                                  additional_groups: additional_groups
                              },
                              email_verified: values.email_verified
                          }).then(_ => {
                              this.props.onFlashbarChange({
                                  items: [
                                      {
                                          type: 'success',
                                          content: 'User created successfully.',
                                          dismissible: true
                                      }
                                  ]
                              })
                              this.getListing().fetchRecords()
                              this.getCreateUserForm().hideModal()
                          }).catch(error => {
                              this.getCreateUserForm().setError(error.errorCode, error.message)
                          })
                      }}
                      onCancel={() => {
                          this.getCreateUserForm().hideModal()
                      }}
                      params={[
                          {
                              name: 'username',
                              title: 'Username',
                              description: 'Enter the username of the account to be created',
                              data_type: 'str',
                              param_type: 'text',
                              validate: {
                                  required: true,
                                  regex: '^(?=.{3,32}$)(?![_.])(?!.*[_.]{2})[a-z0-9._]+(?<![_.])$'
                              }
                          },
                          {
                              name: 'email',
                              title: 'Email',
                              description: 'Enter the email address of the account to be created.',
                              data_type: 'str',
                              param_type: 'text',
                              validate: {
                                  required: true
                              }
                          },
                          {
                              name: 'email_verified',
                              title: 'Is Email Verified?',
                              description: 'If email is not verified, an email will be sent to the email address with a verification code.',
                              data_type: 'bool',
                              param_type: 'text',
                              validate: {
                                  required: true
                              },
                              default: false
                          },
                          {
                              name: 'password',
                              title: 'Password',
                              description: 'Enter password for the account.',
                              help_text: 'Password is required when email is verified',
                              data_type: 'str',
                              param_type: 'new-password',
                              validate: {
                                  required: true
                              },
                              when: {
                                  param: 'email_verified',
                                  eq: true
                              }
                          },
                          {
                              name: 'permissions',
                              title: 'Permissions',
                              description: 'Select applicable permissions for the user',
                              data_type: 'str',
                              param_type: 'select',
                              multiple: true,
                              choices: getPermissionChoices(),
                              validate: {
                                  required: true
                              }
                          },
                          {
                              name: 'login_shell',
                              title: 'Login Shell',
                              description: 'Select the login shell for the user:',
                              data_type: 'str',
                              param_type: 'select',
                              choices: [
                                  {
                                      title: 'Bash (/bin/bash)',
                                      value: '/bin/bash'
                                  },
                                  {
                                      title: 'Shell (/bin/sh)',
                                      value: '/bin/sh'
                                  },
                                  {
                                      title: 'Bash (/usr/bin/bash)',
                                      value: '/usr/bin/bash'
                                  },
                                  {
                                      title: 'Shell (/usr/bin/sh)',
                                      value: '/usr/bin/sh'
                                  },
                                  {
                                      title: 'tcsh (/bin/tcsh)',
                                      value: '/bin/tcsh'
                                  },
                                  {
                                      title: 'csh (/bin/csh)',
                                      value: '/bin/csh'
                                  }
                              ],
                              default: '/bin/bash'
                          }
                      ]}/>
        )
    }

    /**
     * list groups as choices
     * used by AddUserToGroup and RemoveUserFromGroup Forms.
     * @param filters
     * @private
     */
    private listGroupsAsChoices(filters: SocaFilter[]) {
        return this.authAdmin().listGroups({
            filters: filters
        }).then(result => {
            const listing = result.listing!
            if (listing.length === 0) {
                return {
                    listing: []
                }
            } else {
                const choices: any = []
                listing.forEach((value) => {
                    choices.push({
                        title: `${value.name} (${value.gid})`,
                        value: value.name
                    })
                })
                return {
                    listing: choices
                }
            }
        })
    }

    buildAddUserToGroupForm() {
        return <IdeaForm ref={this.addUserToGroupForm}
                         name="add-user-to-group"
                         title="Add User to Group"
                         modal={true}
                         onCancel={() => {
                             this.hideAddUserToGroupForm()
                         }}
                         onSubmit={() => {
                             if (!this.getAddUserToGroupForm().validate()) {
                                 return
                             }
                             const values = this.getAddUserToGroupForm().getValues()
                             const username = this.getSelectedUser()?.username!
                             this.authAdmin().addUserToGroup({
                                 usernames: [username],
                                 group_name: values.group_name
                             }).then(_ => {
                                 this.props.onFlashbarChange({
                                     items: [
                                         {
                                             type: 'success',
                                             content: `User: ${username} was successfully added to group: ${values.group_name}`,
                                             dismissible: true
                                         }
                                     ]
                                 })
                                 this.hideAddUserToGroupForm()
                             }).catch(error => {
                                 this.getAddUserToGroupForm().setError(error.errorCode, error.message)
                             })
                         }}
                         onFetchOptions={(request: GetParamChoicesRequest): Promise<GetParamChoicesResult> => {
                             if (request.param === 'group_name') {
                                 let filters = []
                                 if (request.filters && request.filters.length > 0) {
                                     const filterValue = Utils.asString(request.filters[0].value)
                                     if (Utils.isNotEmpty(filterValue)) {
                                         filters.push({
                                             key: 'group_name',
                                             like: filterValue
                                         })
                                     }
                                 }
                                 return this.listGroupsAsChoices(filters)
                             } else {
                                 return Promise.resolve({
                                     listing: []
                                 })
                             }
                         }}
                         params={[
                             {
                                 name: 'group_name',
                                 title: 'Group',
                                 description: 'Please select a group to add user',
                                 data_type: 'str',
                                 param_type: 'autocomplete',
                                 validate: {
                                     required: true
                                 }
                             }
                         ]}/>
    }

    showAddUserToGroupForm() {
        this.setState({
            showAddUserToGroupForm: true
        }, () => {
            this.getAddUserToGroupForm().showModal()
        })
    }

    hideAddUserToGroupForm() {
        this.setState({
            showAddUserToGroupForm: false
        })
    }

    buildRemoveUserFromGroupForm() {
        return <IdeaForm ref={this.removeUserFromGroupForm}
                         name="remove-user-from-group"
                         title="Remove User from Group"
                         modal={true}
                         onCancel={() => {
                             this.hideRemoveUserFromGroupForm()
                         }}
                         onSubmit={() => {
                             if (!this.getRemoveUserFromGroupForm().validate()) {
                                 return
                             }
                             const values = this.getRemoveUserFromGroupForm().getValues()
                             const username = this.getSelectedUser()?.username!
                             this.authAdmin().removeUserFromGroup({
                                 usernames: [username],
                                 group_name: values.group_name
                             }).then(_ => {
                                 this.props.onFlashbarChange({
                                     items: [
                                         {
                                             type: 'success',
                                             content: `User: ${username} was successfully removed from group: ${values.group_name}`,
                                             dismissible: true
                                         }
                                     ]
                                 })
                                 this.hideRemoveUserFromGroupForm()
                             }).catch(error => {
                                 this.getRemoveUserFromGroupForm().setError(error.errorCode, error.message)
                             })
                         }}
                         onFetchOptions={(request: GetParamChoicesRequest): Promise<GetParamChoicesResult> => {
                             if (request.param === 'group_name') {
                                 const groups = this.getSelectedUser()?.additional_groups
                                 const listing: SocaUserInputChoice[] = []
                                 groups?.forEach(group => {
                                     listing.push({
                                         title: group,
                                         value: group
                                     })
                                 })
                                 return Promise.resolve({
                                     listing: listing
                                 })
                             } else {
                                 return Promise.resolve({
                                     listing: []
                                 })
                             }
                         }}
                         params={[
                             {
                                 name: 'group_name',
                                 title: 'Group',
                                 description: 'Please select a group to remove user',
                                 data_type: 'str',
                                 param_type: 'autocomplete',
                                 validate: {
                                     required: true
                                 }
                             }
                         ]}/>
    }

    showRemoveUserFromGroupForm() {
        this.setState({
            showRemoveUserFromGroupForm: true
        }, () => {
            this.getRemoveUserFromGroupForm().showModal()
        })
    }

    hideRemoveUserFromGroupForm() {
        this.setState({
            showRemoveUserFromGroupForm: false
        })
    }

    buildToggleAdminUserConfirmModal() {
        let message
        if (this.isSelectedUserAdmin()) {
            message = 'Are you sure you want to revoke admin rights from: '
        } else {
            message = 'Are you sure you want to grant admin rights to: '
        }
        return (
            <IdeaConfirm ref={this.toggleAdminUserConfirmModal}
                         title={(this.isSelectedUserAdmin()) ? 'Revoke Admin Rights' : 'Grant Admin Rights'}
                         onConfirm={() => {
                             const username = this.getSelectedUser()?.username
                             if (this.isSelectedUserAdmin()) {
                                 this.authAdmin().removeSudoUser({
                                     username: username
                                 }).then(_ => {
                                     this.getListing().fetchRecords()
                                     this.setFlashMessage(`Admin rights were revoked from User: ${username}.`, 'success')
                                 }).catch(error => {
                                     this.setFlashMessage(`Failed to revoke admin rights: ${error.message}`, 'error')
                                 })
                             } else {
                                 this.authAdmin().addSudoUser({
                                     username: username
                                 }).then(_ => {
                                     this.getListing().fetchRecords()
                                     this.setFlashMessage(`User: ${username} was successfully granted admin rights.`, 'success')
                                 }).catch(error => {
                                     this.setFlashMessage(`Failed to grant admin rights: ${error.message}`, 'error')
                                 })
                             }
                         }}>
                {message} <b>{this.getSelectedUser()?.username}</b> ?
            </IdeaConfirm>
        )
    }

    buildToggleUserEnabledConfirmModal() {
        let message
        if (this.isSelectedUserEnabled()) {
            message = 'Are you sure you want to disable user: '
        } else {
            message = 'Are you sure you want to enable user: '
        }
        return (
            <IdeaConfirm ref={this.toggleUserEnabledConfirmModal}
                         title={(this.isSelectedUserEnabled()) ? 'Disable User' : 'Enable User'}
                         onConfirm={() => {
                             const username = this.getSelectedUser()?.username
                             if (this.isSelectedUserEnabled()) {
                                 this.authAdmin().disableUser({
                                     username: username
                                 }).then(_ => {
                                     this.getListing().fetchRecords()
                                     this.setFlashMessage(`User: ${username} disabled successfully.`, 'success')
                                 }).catch(error => {
                                     this.setFlashMessage(`Failed to disable user: ${error.message}`, 'error')
                                 })
                             } else {
                                 this.authAdmin().enableUser({
                                     username: username
                                 }).then(_ => {
                                     this.getListing().fetchRecords()
                                     this.setFlashMessage(`User: ${username} enabled successfully.`, 'success')
                                 }).catch(error => {
                                     this.setFlashMessage(`Failed to enable user: ${error.message}`, 'error')
                                 })
                             }
                         }}>
                {message} <b>{this.getSelectedUser()?.username}</b> ?
            </IdeaConfirm>
        )
    }

    buildResetPasswordConfirmModal() {
        return (
            <IdeaConfirm ref={this.resetPasswordConfirmModal}
                         title="Reset Password"
                         onConfirm={() => {
                             const username = this.getSelectedUser()!.username!
                             this.authAdmin().resetPassword({
                                 username: username
                             }).then(_ => {
                                 this.getListing().fetchRecords()
                                 this.setFlashMessage(`User: ${username} password was reset successfully.`, 'success')
                             }).catch(error => {
                                 this.setFlashMessage(`Failed to reset password for user: ${error.message}`, 'error')
                             })
                         }}
            >
                Are you sure you want to reset password for user: <b>{this.getSelectedUser()?.username}</b> ?
            </IdeaConfirm>
        )
    }

    setFlashMessage(message: string, type: 'success' | 'info' | 'error') {
        this.props.onFlashbarChange({
            items: [
                {
                    content: message,
                    type: type,
                    dismissible: true
                }
            ]
        })
    }

    isSelected(): boolean {
        return this.state.userSelected
    }

    getSelectedUser(): User | null {
        if (this.getListing() == null) {
            return null
        }
        return this.getListing().getSelectedItem<User>()
    }

    isSelectedUserAdmin(): boolean {
        if (!this.isSelected()) {
            return false
        }
        const selectedUser = this.getSelectedUser()
        if (selectedUser == null) {
            return false
        }
        return Utils.asBoolean(selectedUser.sudo, false)
    }

    isSelectedUserEnabled(): boolean {
        if (!this.isSelected()) {
            return false
        }
        const selectedUser = this.getSelectedUser()
        if (selectedUser == null) {
            return false
        }
        return selectedUser.enabled!
    }

    buildListing() {
        return (
            <IdeaListView
                ref={this.listing}
                preferencesKey={'users'}
                showPreferences={false}
                title="Users"
                description="Cluster user management"
                selectionType="single"
                primaryAction={{
                    id: 'create-user',
                    text: 'Create User',
                    onClick: () => {
                        this.getCreateUserForm().showModal()
                    }
                }}
                secondaryActionsDisabled={!this.isSelected()}
                secondaryActions={[
                    {
                        id: 'add-user-to-group',
                        text: 'Add user to Group',
                        onClick: () => {
                            this.showAddUserToGroupForm()
                        }
                    },
                    {
                        id: 'remove-user-from-group',
                        text: 'Remove user from Group',
                        onClick: () => {
                            this.showRemoveUserFromGroupForm()
                        }
                    },
                    {
                        id: 'toggle-sudo-user',
                        text: (this.isSelectedUserAdmin()) ? 'Remove as Admin User' : 'Set as Admin User',
                        onClick: () => {
                            this.getToggleAdminUserConfirmModal().show()
                        }
                    },
                    {
                        id: 'toggle-user-enabled',
                        text: (this.isSelectedUserEnabled()) ? 'Disable User' : 'Enable User',
                        onClick: () => {
                            this.getToggleUserEnabledConfirmModal().show()
                        }
                    },
                    {
                        id: 'reset-password',
                        text: 'Reset Password',
                        onClick: () => {
                            this.getResetPasswordConfirmModal().show()
                        }
                    }
                ]}
                showPaginator={true}
                showFilters={true}
                filters={[
                    {
                        key: 'username'
                    }
                ]}
                onFilter={(filters) => {
                    const usernameToken = Utils.asString(filters[0].value).trim().toLowerCase()
                    if (Utils.isEmpty(usernameToken)) {
                        return []
                    } else {
                        return [
                            {
                                key: 'username',
                                like: usernameToken
                            }
                        ]
                    }
                }}
                onRefresh={() => {
                    this.setState({
                        userSelected: false
                    }, () => {
                        this.getListing().fetchRecords()
                    })
                }}
                onSelectionChange={() => {
                    this.setState({
                        userSelected: true
                    })
                }}
                onFetchRecords={() => {
                    return this.authAdmin().listUsers({
                        filters: this.getListing().getFilters(),
                        paginator: this.getListing().getPaginator()
                    })
                }}
                columnDefinitions={USER_TABLE_COLUMN_DEFINITIONS}
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
                        text: 'Cluster Management',
                        href: '#/cluster/status'
                    },
                    {
                        text: 'Users',
                        href: ''
                    }
                ]}
                content={
                    <div>
                        {this.buildCreateUserForm()}
                        {this.state.showAddUserToGroupForm && this.buildAddUserToGroupForm()}
                        {this.state.showRemoveUserFromGroupForm && this.buildRemoveUserFromGroupForm()}
                        {this.buildToggleAdminUserConfirmModal()}
                        {this.buildToggleUserEnabledConfirmModal()}
                        {this.buildResetPasswordConfirmModal()}
                        {this.buildListing()}
                    </div>
                }
            />
        )
    }

}

export default withRouter(Users)
