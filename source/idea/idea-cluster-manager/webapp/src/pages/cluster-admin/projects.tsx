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

import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {Project, SocaUserInputChoice} from '../../client/data-model'
import IdeaListView from "../../components/list-view";
import {AccountsClient, ProjectsClient} from "../../client";
import {AppContext} from "../../common";
import {Box, Button, Header, Modal, ProgressBar, SpaceBetween, StatusIndicator, TagEditor} from "@cloudscape-design/components";
import IdeaForm from "../../components/form";
import Utils from "../../common/utils";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";

export interface ProjectsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface ProjectsState {
    projectSelected: boolean
    createProjectModalType: string // create or update
    showCreateProjectForm: boolean
    showTagEditor: boolean
    tags: any[]
}

const PROJECT_TABLE_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<Project>[] = [
    {
        id: 'title',
        header: 'Title',
        cell: project => project.title,
        sortingField: 'title'
    },
    {
        id: 'name',
        header: 'Project Code',
        cell: project => project.name,
        sortingField: 'name'
    },
    {
        id: 'enabled',
        header: 'Status',
        cell: project => (project.enabled) ? <StatusIndicator type="success">Enabled</StatusIndicator> :
            <StatusIndicator type="stopped">Disabled</StatusIndicator>,
        sortingComparator: (a, b) => {
            const valueA = a.enabled ? 1 : 0;
            const valueB = b.enabled ? 1 : 0;
            return valueA - valueB;
        }
    },
    {
        id: 'budgets',
        header: 'Budgets',
        minWidth: 240,
        cell: project => {
            if (project.enable_budgets) {
                if (project.budget) {
                    if (project.budget.is_missing) {
                        return <StatusIndicator type="error">Budget not found: {project.budget.budget_name}</StatusIndicator>
                    }
                    const actualSpend = Utils.asNumber(project.budget.actual_spend?.amount, 0)
                    const limit = Utils.asNumber(project.budget.budget_limit?.amount, 0)
                    const usage = (actualSpend / limit) * 100
                    return (
                        <ProgressBar
                            value={usage}
                            additionalInfo={`Limit: ${Utils.getFormattedAmount(project.budget.budget_limit)}, Forecasted: ${Utils.getFormattedAmount(project.budget.forecasted_spend)}`}
                            description={`Actual Spend: ${Utils.getFormattedAmount(project.budget.actual_spend)} for budget: ${project.budget.budget_name}`}
                        />
                    )
                } else {
                    // Handle when budget is enabled but budget data is missing
                    return <StatusIndicator type="warning">Budget unavailable</StatusIndicator>
                }
            } else {
                return <span style={{color: 'grey'}}> -- </span>
            }
        },
        sortingComparator: (a, b) => {
            // Sort by budget usage percentage
            if (!a.enable_budgets && !b.enable_budgets) return 0;
            if (!a.enable_budgets) return -1;
            if (!b.enable_budgets) return 1;

            // Handle cases where budget is undefined
            if (!a.budget && !b.budget) return 0;
            if (!a.budget) return -1;
            if (!b.budget) return 1;

            const aSpend = Utils.asNumber(a.budget?.actual_spend?.amount, 0);
            const aLimit = Utils.asNumber(a.budget?.budget_limit?.amount, 0);
            const bSpend = Utils.asNumber(b.budget?.actual_spend?.amount, 0);
            const bLimit = Utils.asNumber(b.budget?.budget_limit?.amount, 0);

            const aUsage = aLimit > 0 ? (aSpend / aLimit) : 0;
            const bUsage = bLimit > 0 ? (bSpend / bLimit) : 0;

            return aUsage - bUsage;
        }
    },
    {
        id: 'ldap-group',
        header: 'Groups',
        cell: (project) => {
            if (project.ldap_groups && project.ldap_groups.length > 0) {
                return (
                    <div>
                        {
                            project.ldap_groups.map((ldap_group, index) => {
                                return <li key={index}>{ldap_group}</li>
                            })
                        }
                    </div>
                )
            } else {
                return <span style={{color: 'grey'}}>None</span>
            }
        },
        sortingComparator: (a, b) => {
            const aGroups = a.ldap_groups || [];
            const bGroups = b.ldap_groups || [];
            return aGroups.length - bGroups.length;
        }
    },
    {
        id: 'updated_on',
        header: 'Updated On',
        cell: project => new Date(project.updated_on!).toLocaleString(),
        sortingComparator: (a, b) => {
            const dateA = a.updated_on ? new Date(a.updated_on).getTime() : 0;
            const dateB = b.updated_on ? new Date(b.updated_on).getTime() : 0;
            return dateA - dateB;
        }
    }
]

class Projects extends Component<ProjectsProps, ProjectsState> {

    listing: RefObject<IdeaListView>
    createProjectForm: RefObject<IdeaForm>

    constructor(props: ProjectsProps) {
        super(props);
        this.listing = React.createRef()
        this.createProjectForm = React.createRef()
        this.state = {
            projectSelected: false,
            showCreateProjectForm: false,
            createProjectModalType: '',
            showTagEditor: false,
            tags: []
        }
    }

    projects(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    accounts(): AccountsClient {
        return AppContext.get().client().accounts()
    }

    getListing(): IdeaListView {
        return this.listing.current!
    }

    getCreateProjectForm(): IdeaForm {
        return this.createProjectForm.current!
    }

    showCreateProjectForm(type: string) {
        this.setState({
            showCreateProjectForm: true,
            createProjectModalType: type
        }, () => {
            this.getCreateProjectForm().showModal()
        })
    }

    hideCreateProjectForm() {
        this.setState({
            showCreateProjectForm: false,
            createProjectModalType: ''
        })
    }

    isSelected(): boolean {
        return this.state.projectSelected
    }

    isSelectedProjectEnabled(): boolean {
        if (!this.isSelected()) {
            return false
        }
        const selectedProject = this.getSelected()
        if (selectedProject == null) {
            return false
        }
        if (selectedProject.enabled != null) {
            return selectedProject.enabled
        }
        return false
    }

    getSelected(): Project | null {
        if (this.getListing() == null) {
            return null
        }
        return this.getListing().getSelectedItem()
    }

    buildCreateProjectForm() {
        let values = undefined
        const isUpdate = this.state.createProjectModalType === 'update'
        if (isUpdate) {
            const selected = this.getSelected()
            if (selected != null) {
                values = {
                    ...selected,
                    'budget.budget_name': selected.budget?.budget_name
                }
            }
        }
        return (this.state.showCreateProjectForm &&
            <IdeaForm ref={this.createProjectForm}
                      name="create-update-project"
                      modal={true}
                      modalSize="medium"
                      title={(isUpdate) ? 'Update Project' : 'Create new Project'}
                      values={values}
                      onSubmit={() => {
                          if (!this.getCreateProjectForm().validate()) {
                              return
                          }
                          const values = this.getCreateProjectForm().getValues()
                          let createOrUpdate
                          if (isUpdate) {
                              createOrUpdate = (request: any) => this.projects().updateProject(request)
                              values.project_id = this.getSelected()?.project_id
                          } else {
                              createOrUpdate = (request: any) => this.projects().createProject(request)
                          }
                          createOrUpdate({
                              project: values
                          }).then(() => {
                              this.setState({
                                  projectSelected: false
                              }, () => {
                                  this.hideCreateProjectForm()
                                  this.getListing().fetchRecords()
                              })
                          }).catch(error => {
                              this.getCreateProjectForm().setError(error.errorCode, error.message)
                          })
                      }}
                      onCancel={() => {
                          this.hideCreateProjectForm()
                      }}
                      onFetchOptions={(request) => {
                          if (request.param === 'ldap_groups') {
                              return this.accounts().listGroups({
                                  filters: [
                                      {
                                          key: 'group_type',
                                          eq: 'project'
                                      }
                                  ]
                              }).then(result => {
                                  const listing = result.listing!
                                  if (listing.length === 0) {
                                      return {
                                          listing: []
                                      }
                                  } else {
                                      const choices: SocaUserInputChoice[] = []
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
                          } else {
                              return Promise.resolve({
                                  listing: []
                              })
                          }

                      }}
                      params={[
                          {
                            name: 'title',
                            title: 'Title',
                            description: 'Enter a user friendly project title',
                            data_type: 'str',
                            param_type: 'text',
                            validate: {
                                required: true,
                                regex: '^([A-Za-z0-9- ]+){3,32}$'
                            }
                          },
                          {
                              name: 'name',
                              title: 'Code',
                              description: 'Enter a project code',
                              help_text: 'Project code cannot contain white spaces or special characters. hyphens (-) are OK.',
                              data_type: 'str',
                              param_type: 'text',
                              readonly: isUpdate,
                              validate: {
                                  required: true,
                                  regex: '^([a-z0-9-]+){3,18}$'
                              }
                          },
                          {
                              name: 'description',
                              title: 'Description',
                              description: 'Enter the project description',
                              data_type: 'str',
                              param_type: 'text',
                              multiline: true
                          },
                          {
                              name: 'ldap_groups',
                              title: 'Groups',
                              description: 'Select applicable ldap groups for the Project (optional)',
                              param_type: 'select',
                              multiple: true,
                              data_type: 'str',
                              validate: {
                                  required: false
                              },
                              dynamic_choices: true
                          },
                          {
                              name: 'enable_budgets',
                              title: 'Do you want to enable budgets for this project?',
                              data_type: 'bool',
                              param_type: 'confirm',
                              default: false,
                              validate: {
                                  required: true
                              }
                          },
                          {
                              name: 'budget.budget_name',
                              title: 'Enter the AWS budget name for the project',
                              data_type: 'str',
                              param_type: 'text',
                              validate: {
                                  required: true
                              },
                              when: {
                                  param: 'enable_budgets',
                                  eq: true
                              }
                          }
                      ]}
            />
        )
    }

    buildTagEditor() {
        const onCancel = () => {
            this.hideTagEditor()
        }

        const onSubmit = () => {
            this.projects().updateProject({
                project: {
                    project_id: this.getSelected()?.project_id,
                    tags: this.state.tags
                }
            }).then(() => {
                this.props.onFlashbarChange({
                    items: [
                        {
                            type: 'success',
                            content: `Tags updated for project: ${this.getSelected()?.name}`,
                            dismissible: true
                        }
                    ]
                })
                this.getListing().fetchRecords()
                this.hideTagEditor()
            })
        }

        return (
            <Modal
                size="large"
                visible={this.state.showTagEditor}
                onDismiss={onCancel}
                header={
                    <Header variant="h3">Tags: {this.getSelected()?.title}</Header>
                }
                footer={
                    <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" onClick={onCancel}>Cancel</Button>
                            <Button variant="primary" onClick={onSubmit}>Submit</Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                <TagEditor
                    tags={this.state.tags}
                    tagLimit={20}
                    onChange={(event) => {
                        const tags: any[] = []
                        event.detail.tags.forEach((tag) => {
                            tags.push({
                                key: tag.key,
                                value: tag.value
                            })
                        })
                        this.setState({
                            tags: tags
                        })
                    }}
                    i18nStrings={{
                        keyPlaceholder: "Enter key",
                        valuePlaceholder: "Enter value",
                        addButton: "Add new tag",
                        removeButton: "Remove",
                        undoButton: "Undo",
                        undoPrompt:
                            "This tag will be removed upon saving changes",
                        loading:
                            "Loading tags that are associated with this resource",
                        keyHeader: "Key",
                        valueHeader: "Value",
                        optional: "optional",
                        keySuggestion: "Custom tag key",
                        valueSuggestion: "Custom tag value",
                        emptyTags:
                            "No tags associated with the resource.",
                        tooManyKeysSuggestion:
                            "You have more keys than can be displayed",
                        tooManyValuesSuggestion:
                            "You have more values than can be displayed",
                        keysSuggestionLoading: "Loading tag keys",
                        keysSuggestionError:
                            "Tag keys could not be retrieved",
                        valuesSuggestionLoading: "Loading tag values",
                        valuesSuggestionError:
                            "Tag values could not be retrieved",
                        emptyKeyError: "You must specify a tag key",
                        maxKeyCharLengthError:
                            "The maximum number of characters you can use in a tag key is 128.",
                        maxValueCharLengthError:
                            "The maximum number of characters you can use in a tag value is 256.",
                        duplicateKeyError:
                            "You must specify a unique tag key.",
                        invalidKeyError:
                            "Invalid key. Keys can only contain alphanumeric characters, spaces and any of the following: _.:/=+@-",
                        invalidValueError:
                            "Invalid value. Values can only contain alphanumeric characters, spaces and any of the following: _.:/=+@-",
                        awsPrefixError: "Cannot start with aws:",
                        tagLimit: availableTags =>
                            availableTags === 1
                                ? "You can add up to 1 more tag."
                                : "You can add up to " +
                                availableTags +
                                " more tags.",
                        tagLimitReached: tagLimit =>
                            tagLimit === 1
                                ? "You have reached the limit of 1 tag."
                                : "You have reached the limit of " +
                                tagLimit +
                                " tags.",
                        tagLimitExceeded: tagLimit =>
                            tagLimit === 1
                                ? "You have exceeded the limit of 1 tag."
                                : "You have exceeded the limit of " +
                                tagLimit +
                                " tags.",
                        enteredKeyLabel: key => 'Use "' + key + '"',
                        enteredValueLabel: value => 'Use "' + value + '"'
                    }}
                />
            </Modal>
        )
    }

    showTagEditor() {
        const tags: any[] = []
        const selected = this.getSelected()
        if (selected != null) {
            selected.tags?.forEach(tag => {
                tags.push({
                    key: tag.key,
                    value: tag.value
                })
            })
        }
        this.setState({
            showTagEditor: true,
            tags: tags
        })
    }

    hideTagEditor() {
        this.setState({
            showTagEditor: false,
            tags: []
        })
    }

    buildListing() {
        return (
            <IdeaListView
                ref={this.listing}
                preferencesKey={'projects'}
                showPreferences={false}
                title="Projects"
                description="Cluster Project Management"
                selectionType="single"
                primaryAction={{
                    id: 'create-project',
                    text: 'Create Project',
                    onClick: () => {
                        this.showCreateProjectForm('create')
                    }
                }}
                secondaryActionsDisabled={!this.isSelected()}
                secondaryActions={[
                    {
                        id: 'edit-project',
                        text: 'Edit Project',
                        onClick: () => {
                            this.showCreateProjectForm('update')
                        }
                    },
                    {
                        id: 'toggle-enable-project',
                        text: (this.isSelectedProjectEnabled()) ? 'Disable Project' : 'Enable Project',
                        onClick: () => {
                            let enableOrDisable
                            if (this.isSelectedProjectEnabled()) {
                                enableOrDisable = (request: any) => this.projects().disableProject(request)
                            } else {
                                enableOrDisable = (request: any) => this.projects().enableProject(request)
                            }
                            enableOrDisable({
                                project_id: this.getSelected()?.project_id
                            }).then(() => {
                                this.getListing().fetchRecords()
                            }).catch(error => {
                                this.props.onFlashbarChange({
                                    items: [
                                        {
                                            type: 'error',
                                            content: `Operation failed: ${error.message}`,
                                            dismissible: true
                                        }
                                    ]
                                })
                            })
                        }
                    },
                    {
                        id: 'update-tags',
                        text: 'Update Tags',
                        onClick: () => {
                            this.showTagEditor()
                        }
                    }
                ]}
                showPaginator={true}
                showFilters={true}
                defaultSortingColumn="updated_on"
                defaultSortingDescending={true}
                filters={[
                    {
                        key: 'name'
                    }
                ]}
                onFilter={(filters) => {
                    const projectNameToken = Utils.asString(filters[0].value).trim().toLowerCase()
                    if (Utils.isEmpty(projectNameToken)) {
                        return []
                    } else {
                        return [
                            {
                                key: 'name',
                                like: projectNameToken
                            }
                        ]
                    }
                }}
                onRefresh={() => {
                    this.setState({
                        projectSelected: false
                    }, () => {
                        this.getListing().fetchRecords()
                    })
                }}
                onSelectionChange={() => {
                    this.setState({
                        projectSelected: true
                    }, () => {
                    })
                }}
                onFetchRecords={() => {
                    return this.projects().listProjects({
                        filters: this.getListing().getFilters(),
                        paginator: this.getListing().getPaginator(),
                        date_range: this.getListing().getDateRange()
                    })
                }}
                columnDefinitions={PROJECT_TABLE_COLUMN_DEFINITIONS}
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
                        text: 'Projects',
                        href: ''
                    }
                ]}
                content={
                    <div>
                        {this.buildCreateProjectForm()}
                        {this.buildTagEditor()}
                        {this.buildListing()}
                    </div>
                }/>
        )
    }
}

export default withRouter(Projects)
