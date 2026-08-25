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
import {v4 as uuid} from "uuid";

import IdeaForm from "../../components/form";
import {
    Alert,
    Box,
    Button, ButtonDropdown,
    ColumnLayout,
    Container, ExpandableSection,
    Form,
    FormField, Grid,
    Header, Link,
    Modal, PieChart,
    Select, SelectProps,
    SpaceBetween, StatusIndicator, Table, Tabs, TextFilter, Tiles
} from "@cloudscape-design/components";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import {faBug, faCheckCircle, faEdit, faPlay, faStar, faRefresh, faCircleMinus} from "@fortawesome/free-solid-svg-icons";
import {
    HpcApplication, JobValidationResultEntry,
    SocaInstanceTypeOptions,
    SocaJobEstimatedBOMCostLineItem,
    SocaUserInputParamMetadata,
    SubmitJobResult
} from "../../client/data-model";
import {ProjectsClient, SchedulerClient} from "../../client";
import IdeaException from '../../common/exceptions'
import {AppContext} from "../../common";
import Utils from "../../common/utils";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {JobTemplate} from "../../service/job-templates-service";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";
import {updateJobScriptSelect} from "./pbs-select";

import nunjucks from "nunjucks"


export interface SubmitJobProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface JobSizeEstimate {
    nodeCount: number
    cpusPerInstance: number
    instanceType: string
}

export interface SubmitJobState {
    splitPanelOpen: boolean

    showApplicationSelectModal: boolean
    applications: HpcApplication[]
    applicationFilterText: string,
    filteredApplications: HpcApplication[]
    selectedApplicationId: string | null
    selectedProject: SelectProps.Option | null
    projectOptions: SelectProps.Option[]

    showJobTemplatesModal: boolean
    jobTemplates: JobTemplate[]
    jobTemplateFilterText: string,
    filteredJobTemplates: JobTemplate[]
    selectedJobTemplateId: string | null

    showSaveJobTemplatesModal: boolean

    jobSubmissionParameters: any
    application?: HpcApplication
    submitJobResult?: SubmitJobResult
    jobScript?: string
    activeTab: string

    jobSizeEstimate?: JobSizeEstimate
    showJobSizeConfirmModal: boolean

    errorMessage: string
    submitJobLoading: boolean
    dryRunLoading: boolean

    // idempotency key for Scheduler.SubmitJob. stable while the same submission is retried,
    // regenerated once a submission has been accepted so the next one is a new job.
    clientSubmissionId: string
}

// no. of compute nodes above which the user must explicitly confirm the submission.
const JOB_SIZE_CONFIRM_NODE_COUNT = 10

// job params that change the no. of compute nodes the job will request.
const JOB_SIZE_PARAMS = ['cpus', 'instance_type', 'ht_support', 'enable_ht_support', 'queue', 'queue_name']

const TAB_SUBMIT_JOB = 'submit-job'
const TAB_COST_ESTIMATES = 'cost-estimates'
const TAB_SERVICE_QUOTAS = 'service-quotas'
const TAB_BUDGET_USAGE = 'budget-usage'
const TAB_JOB_SCRIPT = 'job-script'
const TAB_JOB_PARAMETERS = 'job-parameters'

class SubmitJob extends Component<SubmitJobProps, SubmitJobState> {

    jobSubmissionForm: RefObject<IdeaForm | null>
    saveJobTemplateForm: RefObject<IdeaForm | null>
    instanceTypeOptionsCacheKey: string | null
    instanceTypeOptionsCache: SocaInstanceTypeOptions[]
    jobSizeEstimateTimeout: any | null = null

    constructor(props: SubmitJobProps) {
        super(props);
        this.jobSubmissionForm = React.createRef()
        this.saveJobTemplateForm = React.createRef()
        this.instanceTypeOptionsCacheKey = null
        this.instanceTypeOptionsCache = []
        this.state = {
            splitPanelOpen: false,

            applications: [],
            applicationFilterText: '',
            filteredApplications: [],
            selectedApplicationId: null,
            selectedProject: null,
            projectOptions: [],

            jobSubmissionParameters: {},

            showJobTemplatesModal: false,
            jobTemplates: [],
            jobTemplateFilterText: '',
            filteredJobTemplates: [],
            selectedJobTemplateId: null,

            showSaveJobTemplatesModal: false,

            showApplicationSelectModal: false,
            activeTab: 'submit-job',

            showJobSizeConfirmModal: false,

            errorMessage: '',
            submitJobLoading: false,
            dryRunLoading: false,

            clientSubmissionId: uuid()
        }
        nunjucks.configure({
            autoescape: false
        })
    }

    getSchedulerClient(): SchedulerClient {
        return AppContext.get().client().scheduler()
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getJobSubmissionForm(): IdeaForm {
        return this.jobSubmissionForm.current!
    }

    getSaveJobTemplateForm(): IdeaForm {
        return this.saveJobTemplateForm.current!
    }

    componentDidMount() {
        this.initialize()
    }

    getErrorMessage = (error: any): string => {
        if (Utils.isNotEmpty(error?.message)) {
            return error.message
        }
        if (Utils.isNotEmpty(error?.errorCode)) {
            return `Request failed with error code: ${error.errorCode}`
        }
        return `${error}`
    }

    onError = (error: any, header: string) => {
        this.props.onFlashbarChange({
            items: [
                {
                    type: 'error',
                    header: header,
                    content: this.getErrorMessage(error),
                    dismissible: true
                }
            ]
        })
    }

    componentWillUnmount() {
        if (this.jobSizeEstimateTimeout) {
            clearTimeout(this.jobSizeEstimateTimeout)
            this.jobSizeEstimateTimeout = null
        }
    }

    loadApplication(application: HpcApplication) {
        let projectOptions: any = []
        application.projects?.forEach(project => {
            projectOptions.push({
                label: project.title,
                description: `Project Code: ${project.name}`,
                value: project.name
            })
        })
        this.setState({
            application: {
                ...application
            },
            selectedApplicationId: null,
            showApplicationSelectModal: false,
            projectOptions: projectOptions,
            selectedProject: projectOptions[0]
        })
    }

    initialize(stateBase64?: string | null) {

        const queryParams = new URLSearchParams(this.props.location.search)
        if (stateBase64 == null) {
            stateBase64 = queryParams.get('state')
        } else {
            this.updateUrlState(stateBase64)
        }

        let state: any = null
        if (stateBase64) {
            // ?state= is user-editable and template_data comes from stored job templates;
            // a corrupt value must not throw inside componentDidMount and blank the page
            try {
                state = JSON.parse(atob(stateBase64))
            } catch (error) {
                console.error('failed to decode job submission state', error)
                state = null
            }
        }

        if (state && state.applicationId) {
            this.getSchedulerClient().getUserApplications({
                application_ids: [state.applicationId]
            }).then(result => {
                if (result.applications?.length > 0) {
                    this.loadApplication(result.applications[0])
                }
            }).catch(error => {
                this.onError(error, 'Failed to load the application')
            })
        }

        let jobSubmissionParameters: any = null
        if (state && state.params) {
            jobSubmissionParameters = state.params
        }

        const inputFile = queryParams.get('input_file')
        if (inputFile) {
            jobSubmissionParameters = {
                ...jobSubmissionParameters,
                input_file: inputFile,
                job_name: inputFile.substring(inputFile.lastIndexOf('/') + 1, inputFile.length)
                    .replaceAll(`.`, '_')
                    .replaceAll(/\s+/g, '_')  // Replace spaces with underscores
                    .replace(/[^A-Za-z0-9_-]/g, '')  // Remove any other invalid characters
                    .substring(0, 50),  // Limit to 50 characters to match backend validation
            }
        }

        this.setState({
            jobSubmissionParameters: jobSubmissionParameters
        })
    }

    buildUrlState = () => {
        const state = {
            project: this.state.selectedProject?.value,
            applicationId: this.state.application?.application_id,
            params: this.state.jobSubmissionParameters
        }
        return btoa(JSON.stringify(state))
    }

    updateUrlState = (stateBase64?: string) => {
        if (stateBase64) {
            this.props.searchParams.set('state', stateBase64)
        } else {
            this.props.searchParams.set('state', this.buildUrlState())
        }
        this.props.setSearchParams(this.props.searchParams)
    }

    resetForm() {
        this.setState({
            application: undefined,
            submitJobResult: undefined,
            jobSizeEstimate: undefined
        }, () => {
            this.props.searchParams.set('state', '')
        })
    }

    buildApplicationSelectModal() {
        const onCancel = () => {
            this.setState({
                selectedApplicationId: null,
                showApplicationSelectModal: false
            })
        }
        return (
            <Modal visible={this.state.showApplicationSelectModal}
                   size="large"
                   onDismiss={onCancel}
                   header="Select an Application to Submit your Job"
                   footer={
                       <Box float="right">
                           <SpaceBetween size="xs" direction="horizontal">
                               <Button variant="normal" onClick={onCancel}>Cancel</Button>
                               <Button variant="primary"
                                       disabled={this.state.selectedApplicationId == null}
                                       onClick={() => {
                                           this.state.applications.forEach(application => {
                                               if(application.application_id === this.state.selectedApplicationId) {
                                                   this.loadApplication(application)
                                               }
                                           })
                                       }}>Select</Button>
                           </SpaceBetween>
                       </Box>
                   }
            >
                <div style={{height: '60vh', overflow: 'auto'}}>
                    <SpaceBetween size="m" direction="vertical">
                        <TextFilter
                            filteringText={this.state.applicationFilterText}
                            filteringPlaceholder="Search available applications"
                            onChange={(event) => {
                                this.setState({
                                    applicationFilterText: event.detail.filteringText
                                })
                            }}
                            onDelayedChange={(event) => {
                                const filteringText = event.detail.filteringText.trim().toLowerCase()
                                let filteredApplications: HpcApplication[] = []
                                if (Utils.isEmpty(filteringText)) {
                                    filteredApplications = [...this.state.applications]
                                } else {
                                    this.state.applications.forEach((application) => {
                                        if (application.title) {
                                            if (application.title.trim().toLowerCase().includes(filteringText)) {
                                                filteredApplications.push(application)
                                            }
                                        }
                                    })
                                }
                                this.setState({
                                    filteredApplications: filteredApplications
                                })
                            }}
                        />
                        <Tiles
                            value={this.state.selectedApplicationId}
                            onChange={(event) => this.setState({
                                selectedApplicationId: event.detail.value
                            })}
                            columns={4}
                            items={this.state.filteredApplications.map((application) => {
                                return {
                                    label: application.title!,
                                    value: application.application_id!,
                                    image: (
                                        this.getApplicationImage(application)
                                    )
                                }
                            })}
                        />
                    </SpaceBetween>
                </div>
            </Modal>
        )
    }

    loadAllJobTemplates(): Promise<boolean> {
        return new Promise((resolve, reject) => {
            AppContext.get().jobTemplates().listJobTemplates().then(jobTemplates => {
                this.setState({
                    jobTemplates: [...jobTemplates],
                    filteredJobTemplates: [...jobTemplates]
                }, () => {
                    resolve(true)
                })
            }).catch(error => {
                reject(error)
            })
        })
    }

    showJobTemplatesSelectionModal() {
        this.loadAllJobTemplates().then(() => {
            this.setState({
                showJobTemplatesModal: true
            })
        }).catch(error => {
            this.onError(error, 'Failed to load job templates')
        })
    }

    showSaveJobTemplateForm() {
        this.loadAllJobTemplates().then(() => {
            this.setState({
                showSaveJobTemplatesModal: true
            }, () => {
                this.getSaveJobTemplateForm().showModal()
            })
        }).catch(error => {
            this.onError(error, 'Failed to load job templates')
        })
    }

    buildSaveJobTemplateForm() {
        const getJobTemplateChoices = () => {
            return this.state.jobTemplates.map(template => {
                return {
                    title: template.title,
                    description: template.description,
                    value: template.id!
                }
            })
        }
        const onCancel = () => {
            this.setState({
                showSaveJobTemplatesModal: false
            })
        }
        return (
            <IdeaForm ref={this.saveJobTemplateForm}
                      name="save-job-template"
                      modal={true}
                      modalSize="medium"
                      title="Save Job Template"
                      description="You can create a new job template or update an existing one"
                      onCancel={onCancel}
                      onStateChange={(event) => {
                          if (event.param.name === 'job_template_id') {
                              const found = this.state.jobTemplates.find(jobTemplate => jobTemplate.id === event.value)
                              if (found) {
                                  this.getSaveJobTemplateForm().setParamValue('title', found.title)
                                  this.getSaveJobTemplateForm().setParamValue('description', found.description)
                              }
                          }
                      }}
                      onSubmit={() => {
                          if (!this.getSaveJobTemplateForm().validate()) {
                              return
                          }

                          const values = this.getSaveJobTemplateForm().getValues()
                          if (values.create_new) {
                              AppContext.get().jobTemplates().createJobTemplate({
                                  title: values.title,
                                  description: values.description,
                                  template_data: this.buildUrlState()
                              }).catch(error => {
                                  this.onError(error, 'Failed to save the job template')
                              }).finally(onCancel)
                          } else {
                              AppContext.get().jobTemplates().updateJobTemplate({
                                  id: values.job_template_id,
                                  title: values.title,
                                  description: values.description,
                                  template_data: this.buildUrlState()
                              }).catch(error => {
                                  this.onError(error, 'Failed to update the job template')
                              }).finally(onCancel)
                          }
                      }}
                      params={[
                          {
                              name: 'create_new',
                              title: 'Create new Job Template?',
                              description: 'Check to create a new one, or no to select an existing job template',
                              data_type: 'bool',
                              param_type: 'confirm',
                              default: true
                          },
                          {
                              title: 'Job Template',
                              name: 'job_template_id',
                              description: 'Select an existing job template',
                              param_type: 'select',
                              data_type: 'str',
                              choices: getJobTemplateChoices(),
                              validate: {
                                  required: true
                              },
                              when: {
                                  param: 'create_new',
                                  eq: false
                              }
                          },
                          {
                              title: 'Title',
                              name: 'title',
                              description: 'Enter a title for your job template',
                              param_type: 'text',
                              data_type: 'str',
                              validate: {
                                  required: true
                              }
                          },
                          {
                              title: 'Description',
                              name: 'description',
                              description: 'Enter a description for your job template',
                              param_type: 'text',
                              data_type: 'str',
                              multiline: true,
                              validate: {
                                  required: true
                              }
                          }
                      ]}
            />
        )
    }

    buildJobTemplatesSelectionModal() {
        const onCancel = () => {
            this.setState({
                selectedJobTemplateId: null,
                showJobTemplatesModal: false
            })
        }
        const onDeleteJobTemplate = () => {
            AppContext.get().jobTemplates().deleteJobTemplate(this.state.selectedJobTemplateId!).then(() => {
                this.loadAllJobTemplates().catch(error => {
                    this.onError(error, 'Failed to load job templates')
                }).finally(() => {
                    this.setState({
                        selectedJobTemplateId: null
                    })
                })
            }).catch(error => {
                this.onError(error, 'Failed to delete the job template')
            })
        }
        const onLoadJobTemplate = () => {
            AppContext.get().jobTemplates().getJobTemplate(this.state.selectedJobTemplateId!).then(jobTemplate => {
                this.setState({
                    selectedJobTemplateId: null,
                    showJobTemplatesModal: false,
                    errorMessage: ''
                }, () => {
                    this.initialize(jobTemplate.template_data)
                })
            }).catch(error => {
                this.onError(error, 'Failed to load the job template')
            })
        }
        return (
            <Modal visible={this.state.showJobTemplatesModal}
                   size="large"
                   onDismiss={onCancel}
                   header="Select a saved Job Template"
                   footer={
                       <div>
                           <Box float="left">
                               <SpaceBetween size="xs" direction="horizontal">
                                   <Button variant="normal"
                                           disabled={this.state.selectedJobTemplateId == null}
                                           onClick={onDeleteJobTemplate}>Delete</Button>
                               </SpaceBetween>
                           </Box>
                           <Box float="right">
                               <SpaceBetween size="xs" direction="horizontal">
                                   <Button variant="normal" onClick={onCancel}>Cancel</Button>
                                   <Button variant="primary"
                                           disabled={this.state.selectedJobTemplateId == null}
                                           onClick={onLoadJobTemplate}>Load Job Template</Button>
                               </SpaceBetween>
                           </Box>
                       </div>
                   }
            >
                <div style={{height: '60vh', overflow: 'auto'}}>
                    <SpaceBetween size="m" direction="vertical">
                        <TextFilter
                            filteringText={this.state.jobTemplateFilterText}
                            filteringPlaceholder="Search Job Templates"
                            onChange={(event) => {
                                this.setState({
                                    jobTemplateFilterText: event.detail.filteringText
                                })
                            }}
                            onDelayedChange={(event) => {
                                const filteringText = event.detail.filteringText.trim().toLowerCase()
                                let filteredJobTemplates: JobTemplate[] = []
                                if (Utils.isEmpty(filteringText)) {
                                    filteredJobTemplates = [...this.state.jobTemplates]
                                } else {
                                    this.state.jobTemplates.forEach((jobTemplate) => {
                                        if (jobTemplate.title) {
                                            if (jobTemplate.title.trim().toLowerCase().includes(filteringText)) {
                                                filteredJobTemplates.push(jobTemplate)
                                            }
                                        }
                                    })
                                }
                                this.setState({
                                    filteredJobTemplates: filteredJobTemplates
                                })
                            }}
                        />
                        <Tiles
                            value={this.state.selectedJobTemplateId}
                            onChange={(event) => this.setState({
                                selectedJobTemplateId: event.detail.value
                            })}
                            columns={3}
                            items={this.state.filteredJobTemplates.map((jobTemplate) => {
                                return {
                                    label: (
                                        <div>
                                            <strong>{jobTemplate.title!}</strong><br/>
                                            <span>{jobTemplate.description}</span>
                                        </div>
                                    ),
                                    value: jobTemplate.id!
                                }
                            })}
                        />
                    </SpaceBetween>
                </div>
            </Modal>
        )
    }

    getJobSubmissionFormTemplateParams(): SocaUserInputParamMetadata[] {
        if (this.state.application && this.state.application.form_template) {
            if (this.state.application.form_template.sections && this.state.application.form_template.sections.length > 0) {
                return this.state.application.form_template.sections[0].params!
            }
        }
        return []
    }

    hasFormTemplate = () => {
        if (!this.state.application) {
            return false
        }
        if (!this.state.application.form_template) {
            return false
        }
        if (!this.state.application.form_template.sections) {
            return false
        }
        if (this.state.application.form_template.sections.length === 0) {
            return false
        }
        if (!this.state.application.form_template.sections[0].params) {
            return false
        }
        if (this.state.application.form_template.sections[0].params.length === 0) {
            return false
        }
        return true
    }

    getApplicationImage(application: HpcApplication) {
        if (application.thumbnail_data) {
            return <img
                width={60}
                src={application.thumbnail_data}
                alt={application.title}
            />
        } else if (application.thumbnail_url) {
            return <img
                width={60}
                src={application.thumbnail_url}
                alt={application.title}
            />
        } else {
            return <div className="application-placeholder-image">
                No Image
            </div>
        }
    }

    buildApplicationCard(application: HpcApplication) {
        return <div style={{display: 'flex', flexDirection: 'row', alignItems: 'center'}}>
            {this.getApplicationImage(application)}
            &nbsp;&nbsp;
            <span style={{display: 'inline-block', paddingRight: '10px'}}>{application.title}</span>
        </div>
    }

    buildSubmitJobForm() {

        const getProjectErrorText = () => {
            if (this.state.selectedProject) {
                return ''
            }
            return 'Project is required to submit the job'
        }

        return (
            <div>
                <Form header={<h3>Fill the below form to submit your job.</h3>} variant="embedded">
                    <SpaceBetween size="l" direction="vertical">
                        <FormField
                            label="Application"
                            description="Select an application to submit the job"
                        >
                            <SpaceBetween size="m" direction="horizontal">
                                {this.state.application &&
                                    <div className={"hpc-application-card"}>
                                        {this.buildApplicationCard(this.state.application)}
                                    </div>
                                }
                                <Button variant="normal" onClick={() => {
                                    this.getSchedulerClient().getUserApplications({
                                    }).then(result => {
                                        const listing: HpcApplication[] = (result.applications) ? result.applications : []
                                        this.setState({
                                            applications: [...listing],
                                            filteredApplications: [...listing],
                                            showApplicationSelectModal: true
                                        })
                                    }).catch(error => {
                                        this.onError(error, 'Failed to list applications')
                                    })
                                }}><FontAwesomeIcon icon={faEdit}/></Button>
                            </SpaceBetween>
                        </FormField>
                        {this.state.application && <FormField label="Project"
                                    description="Select a project to tag AWS resources for the Job"
                                    errorText={getProjectErrorText()}
                        >
                            <Select selectedOption={this.state.selectedProject}
                                    options={this.state.projectOptions}
                                    onChange={(event) => {
                                        this.setState({
                                            selectedProject: event.detail.selectedOption
                                        })
                                    }}
                            />
                        </FormField>}
                        {this.hasFormTemplate() &&
                            <IdeaForm
                                ref={this.jobSubmissionForm}
                                name="job-submission-form"
                                columns={1}
                                showHeader={false}
                                showActions={false}
                                values={this.state.jobSubmissionParameters}
                                onStateChange={(event) => {
                                    if (JOB_SIZE_PARAMS.includes(event.param.name!)) {
                                        this.scheduleJobSizeEstimate()
                                    }
                                }}
                                params={this.getJobSubmissionFormTemplateParams()}/>
                        }
                        {this.buildJobSizeEstimate()}
                    </SpaceBetween>
                </Form>
            </div>
        )
    }

    buildJobSizeEstimate() {
        const jobSizeEstimate = this.state.jobSizeEstimate
        if (!jobSizeEstimate) {
            return
        }
        const isLargeJob = jobSizeEstimate.nodeCount >= JOB_SIZE_CONFIRM_NODE_COUNT
        return (
            <Alert type={(isLargeJob) ? 'warning' : 'info'} header="Compute Nodes">
                This job will request <strong>{jobSizeEstimate.nodeCount}</strong> x <strong>{jobSizeEstimate.instanceType}</strong> node(s),
                one EC2 instance per node, at {jobSizeEstimate.cpusPerInstance} cpus per node.
                {isLargeJob && <p>The node count is computed from the requested cpus. Reduce cpus if this is not what you intended.</p>}
            </Alert>
        )
    }

    buildJobSizeConfirmModal() {
        const jobSizeEstimate = this.state.jobSizeEstimate
        const onCancel = () => {
            this.setState({
                showJobSizeConfirmModal: false
            })
        }
        return (
            <Modal visible={this.state.showJobSizeConfirmModal}
                   size="medium"
                   onDismiss={onCancel}
                   header="Confirm Job Size"
                   footer={
                       <Box float="right">
                           <SpaceBetween size="xs" direction="horizontal">
                               <Button variant="normal" onClick={onCancel}>Cancel</Button>
                               <Button variant="primary" onClick={() => {
                                   this.setState({
                                       showJobSizeConfirmModal: false
                                   }, () => {
                                       this.runSubmitJob()
                                   })
                               }}>Submit Job</Button>
                           </SpaceBetween>
                       </Box>
                   }>
                <Alert type="warning">
                    This job will provision <strong>{jobSizeEstimate?.nodeCount}</strong> x <strong>{jobSizeEstimate?.instanceType}</strong> EC2
                    instances ({jobSizeEstimate?.cpusPerInstance} cpus per node). The node count is computed from the requested cpus.
                    Cancel and reduce cpus if this is not what you intended.
                </Alert>
            </Modal>
        )
    }

    onDryRun = () => {
        // clear the previous result so a failed run doesn't leave the last success on screen.
        // pin the form tab: with submitJobResult unset, the result tabs are disabled and would render blank.
        this.setState({
            errorMessage: '',
            submitJobResult: undefined,
            activeTab: 'submit-job',
            dryRunLoading: true
        })
        this.submitJob(true)
            .then(() => {
                this.setState({
                    activeTab: 'submit-job',
                    dryRunLoading: false
                })
            }, (error) => {
                this.setState({
                    errorMessage: this.getErrorMessage(error),
                    activeTab: 'submit-job',
                    dryRunLoading: false
                })
            })
    }

    onSubmitJob = () => {
        // the previous dry-run result is NOT cleared here: cancelling the size confirmation
        // modal must be a no-op, so clearing waits until runSubmitJob() actually runs.
        this.setState({
            errorMessage: '',
            submitJobLoading: true
        }, () => {
            // a job that provisions more than a handful of instances must be confirmed by the user.
            // a transient estimate failure skips confirmation; max_nodes_per_job on the server is the backstop.
            this.updateJobSizeEstimate().then(jobSizeEstimate => {
                if (jobSizeEstimate && jobSizeEstimate.nodeCount >= JOB_SIZE_CONFIRM_NODE_COUNT) {
                    this.setState({
                        showJobSizeConfirmModal: true,
                        submitJobLoading: false
                    })
                } else {
                    this.runSubmitJob()
                }
            })
        })
    }

    runSubmitJob = () => {
        this.setState({
            errorMessage: '',
            submitJobResult: undefined,
            activeTab: 'submit-job',
            submitJobLoading: true
        }, () => {
            this.submitJob(false)
                .then(() => {
                    this.setState({
                        activeTab: 'submit-job',
                        submitJobLoading: false
                    })
                }, (error) => {
                    this.setState({
                        errorMessage: this.getErrorMessage(error),
                        activeTab: 'submit-job',
                        submitJobLoading: false
                    })
                })
        })
    }

    buildEmptyTabMessage = () => {
        return <Alert type="info" header={"Job Submission or Dry Run Results"}>
            <Box variant="div">
                <li>Click <strong>Dry Run</strong> to validate job parameters, compute cost estimations and applicable service
                    quotas.
                </li>
                <li>Click <strong>Submit Job</strong> to submit your simulation job.</li>
            </Box>
        </Alert>
    }

    buildCostEstimates() {

        const getColumnDefinitions = (): TableProps.ColumnDefinition<SocaJobEstimatedBOMCostLineItem>[] => {
            return [
                {
                    id: 'title',
                    header: 'Item',
                    cell: item => item.title
                },
                {
                    id: 'qty',
                    header: 'Qty',
                    cell: item => (item.quantity) ? item.quantity.toFixed(2) : 0.0
                },
                {
                    id: 'unit',
                    header: 'Unit',
                    cell: item => item.unit
                },
                {
                    id: 'unit-price',
                    header: 'Unit Price',
                    cell: item => Utils.getFormattedAmount(item.unit_price)
                },
                {
                    id: 'total-price',
                    header: 'Total Price',
                    cell: item => Utils.getFormattedAmount(item.total_price)
                }
            ]
        }

        if (this.state.submitJobResult && this.state.submitJobResult.estimated_bom_cost) {
            const estimated_bom_cost = this.state.submitJobResult.estimated_bom_cost
            return (
                <ColumnLayout columns={1}>
                    <Table items={(estimated_bom_cost.line_items) ? estimated_bom_cost.line_items! : []}
                           columnDefinitions={getColumnDefinitions()}/>

                    {estimated_bom_cost.savings && estimated_bom_cost.savings.length > 0 && <div>
                        <h4>Potential Savings: {Utils.getFormattedAmount(estimated_bom_cost.savings_total)}</h4>
                        <Table items={estimated_bom_cost.savings}
                               columnDefinitions={getColumnDefinitions()}/>
                    </div>}

                    <ColumnLayout columns={2}>
                        <Box textAlign="left">
                            <h3>Estimated Total Cost Per Hour</h3>
                        </Box>
                        <Box textAlign="right">
                            <h3>{Utils.getFormattedAmount(estimated_bom_cost?.total)}</h3>
                        </Box>
                    </ColumnLayout>

                    <ExpandableSection header="Disclaimer: Baseline numbers">
                        <p>These numbers are just an estimate.</p>
                        <ul>
                            <li>Does not reflect any additional charges such as network or storage transfer, usage of io1 volume (default to
                                gp3)
                            </li>
                            <li>Compute rate is retrieved for your running region</li>
                            <li>FSx Persistent Baseline: (50 MB/s/TiB baseline, up to 1.3 GB/s/TiB burst)</li>
                            <li>FSx Scratch Baseline: (200 MB/s/TiB baseline, up to 1.3 GB/s/TiB burst)</li>
                            <li>EBS/FSx rates as of May 2020 based on us-east-1</li>
                        </ul>
                    </ExpandableSection>
                </ColumnLayout>
            )
        } else {
            return this.buildEmptyTabMessage()
        }
    }

    buildServiceQuotas() {
        if (this.state.submitJobResult?.service_quotas) {
            return <ColumnLayout columns={1}>
                <Table items={this.state.submitJobResult.service_quotas}
                       columnDefinitions={[
                           {
                               id: 'name',
                               header: 'Quota Name',
                               cell: q => q.quota_name
                           },
                           {
                               id: 'available',
                               header: 'Available',
                               cell: q => q.available
                           },
                           {
                               id: 'consumed',
                               header: 'Consumed',
                               cell: q => q.consumed
                           },
                           {
                               id: 'desired',
                               header: 'Desired',
                               cell: q => q.desired
                           }
                       ]}/>
            </ColumnLayout>
        } else {
            return this.buildEmptyTabMessage()
        }
    }

    buildBudgetUsage() {

        const hasBudget = () => {
            return typeof this.state.submitJobResult?.budget_usage !== 'undefined'
        }

        const hasBudgetError = () => {
            if (!this.state.submitJobResult?.incidentals?.results) return false;
            return this.state.submitJobResult.incidentals.results.some(result =>
                result.error_code === 'Budgets.LimitExceeded' &&
                result.message && result.message.includes('Budget not found')
            );
        }

        if (this.isSubmitted()) {
            if (hasBudgetError()) {
                return <Box color="text-status-error">
                    <StatusIndicator type="error">
                        Budget not found for project: <strong>{this.state.selectedProject?.label}</strong>.
                        The configured budget no longer exists in AWS Budgets.
                    </StatusIndicator>
                </Box>
            } else if (hasBudget()) {
                const budget = this.state.submitJobResult?.budget_usage!

                if ((budget as any).is_missing) {
                    return <Box color="text-status-error">
                        <StatusIndicator type="error">
                            Budget not found: <strong>{budget.budget_name}</strong>.
                            The configured budget no longer exists in AWS Budgets.
                        </StatusIndicator>
                    </Box>
                }

                return (
                    <Container>
                        <Grid gridDefinition={[{ colspan: { default: 12, xxs: 12 } }]}>
                            <div className="budget-summary-container">
                                <Header variant="h2">Budget Overview</Header>
                                <Box padding={{ top: 'l' }}>
                                    <Grid gridDefinition={[
                                        { colspan: { default: 7, xxs: 12 } },
                                        { colspan: { default: 5, xxs: 12 } }
                                    ]}>
                                        <div>
                                            <SpaceBetween size="l">
                                                <Container
                                                    header={<Header variant="h3">Budget Details</Header>}
                                                >
                                                    <ColumnLayout columns={2} variant="text-grid">
                                                        <div>
                                                            <Box variant="awsui-key-label">Budget Name</Box>
                                                            <div>{budget.budget_name}</div>
                                                        </div>
                                                        <div>
                                                            <Box variant="awsui-key-label">Budget Limit</Box>
                                                            <div>{Utils.getFormattedAmount(budget.budget_limit)}</div>
                                                        </div>
                                                        <div>
                                                            <Box variant="awsui-key-label">Actual Spend</Box>
                                                            <div>{Utils.getFormattedAmount(budget.actual_spend)}</div>
                                                        </div>
                                                        <div>
                                                            <Box variant="awsui-key-label">Forecasted Spend</Box>
                                                            <div>{Utils.getFormattedAmount(budget.forecasted_spend)}</div>
                                                        </div>
                                                        <div>
                                                            <Box variant="awsui-key-label">Remaining Budget</Box>
                                                            <div>
                                                                {Utils.getFormattedAmount({
                                                                    amount: Math.max(0, (budget.budget_limit?.amount || 0) - (budget.forecasted_spend?.amount || 0)),
                                                                    unit: budget.budget_limit?.unit
                                                                })}
                                                            </div>
                                                        </div>
                                                        <div>
                                                            {budget.job_usage_percent !== undefined && (
                                                                <>
                                                                    <Box variant="awsui-key-label">Job Budget Impact</Box>
                                                                    <div>{budget.job_usage_percent.toFixed(2)}%</div>
                                                                </>
                                                            )}
                                                        </div>
                                                    </ColumnLayout>
                                                </Container>
                                            </SpaceBetween>
                                        </div>
                                        <div>
                                            <Container
                                                header={<Header variant="h3">Budget Allocation</Header>}
                                            >
                                                <PieChart
                                                    hideFilter={true}
                                                    data={[
                                                        {
                                                            title: "Actual Spend",
                                                            value: Number((budget.actual_spend?.amount || 0).toFixed(2)),
                                                            lastUpdate: new Date().toISOString()
                                                        },
                                                        {
                                                            title: "Forecasted Additional Spend",
                                                            value: Number(Math.max(0, (budget.forecasted_spend?.amount || 0) - (budget.actual_spend?.amount || 0)).toFixed(2)),
                                                            lastUpdate: new Date().toISOString()
                                                        },
                                                        {
                                                            title: "Remaining Budget",
                                                            value: Number(Math.max(0, (budget.budget_limit?.amount || 0) - (budget.forecasted_spend?.amount || 0)).toFixed(2)),
                                                            lastUpdate: new Date().toISOString()
                                                        }
                                                    ]}
                                                    segmentDescription={(datum, sum) => {
                                                        const percentage = (datum.value / sum * 100).toFixed(1);
                                                        return `${datum.title}: ${Utils.getFormattedAmount({ amount: datum.value, unit: budget.budget_limit?.unit })} (${percentage}%)`;
                                                    }}
                                                />
                                            </Container>
                                        </div>
                                    </Grid>
                                </Box>
                            </div>
                        </Grid>
                    </Container>
                )
            } else {
                return <Box color="text-body-secondary">
                    Budgets are not enabled for project: <strong>{this.state.selectedProject?.label}</strong>
                </Box>
            }
        }
    }

    isSubmitted = () => {
        return typeof this.state.submitJobResult?.job !== 'undefined'
    }

    hasSubmitJobResult = () => {
        return this.state.submitJobResult != null
    }

    // a successful dry run returns accepted = false, and a job script submitted via the bash
    // interpreter is accepted without a job object.
    isAccepted = () => {
        return this.isSubmitted() || Utils.asBoolean(this.state.submitJobResult?.accepted)
    }

    hasSubmissionErrors = () => {
        return Utils.isNotEmpty(this.state.errorMessage)
    }

    isDryRun = () => {
        return this.hasSubmitJobResult() && Utils.asBoolean(this.state.submitJobResult?.dry_run)
    }

    hasValidationErrors = () => {
        return this.state.submitJobResult?.validations &&
            this.state.submitJobResult.validations.results &&
            this.state.submitJobResult.validations.results?.length > 0
    }

    hasIncidentalErrors = () => {
        return this.state.submitJobResult?.incidentals &&
            this.state.submitJobResult.incidentals.results &&
            this.state.submitJobResult.incidentals.results?.length > 0
    }

    showTab = (tabId: string) => {
        this.setState({
            activeTab: tabId
        })
    }

    buildSubmitJobResults() {

        const buildPostSubmissionLinks = () => {
            return (
                <ul>
                    <li><Link onFollow={() => this.showTab(TAB_COST_ESTIMATES)}>Show Cost Estimates</Link></li>
                    <li><Link onFollow={() => this.showTab(TAB_SERVICE_QUOTAS)}>Show Service Quota Usage</Link></li>
                    <li><Link onFollow={() => this.showTab(TAB_BUDGET_USAGE)}>Show Budget Usage</Link></li>
                    <li><Link onFollow={() => this.showTab(TAB_JOB_SCRIPT)}>Show Job Script</Link></li>
                    <li><Link onFollow={() => this.showTab(TAB_JOB_PARAMETERS)}>Show Job Parameters</Link></li>
                </ul>
            )
        }

        // gate on the result and not on result.job: a rejected job is returned without a job object,
        // carrying the validations that explain the rejection.
        if (this.hasSubmitJobResult()) {
            if (this.hasValidationErrors() || this.hasIncidentalErrors()) {
                const errors: JobValidationResultEntry[] = []
                if (this.hasValidationErrors()) {
                    this.state.submitJobResult?.validations?.results?.forEach(result => {
                        errors.push(result)
                    })
                }
                if (this.hasIncidentalErrors()) {
                    this.state.submitJobResult?.incidentals?.results?.forEach(result => {
                        errors.push(result)
                    })
                }
                return (
                    <ColumnLayout columns={1}>
                        {this.isDryRun() && (
                            <div>
                                <Box variant="h3" color="text-status-error">Dry Run Failed</Box>
                                <p>
                                    <StatusIndicator type="error">Job cannot be submitted with this configuration due to below
                                        errors.</StatusIndicator>
                                </p>
                            </div>
                        )}
                        {!this.isDryRun() && (
                            <div>
                                <Box variant="h3" color="text-status-error">Job Submission Failed</Box>
                                <p>
                                    <StatusIndicator type="error">Job submission failed with errors</StatusIndicator>
                                </p>
                            </div>
                        )}
                        <Table items={errors}
                               columnDefinitions={[
                                   {
                                       id: 'error-code',
                                       header: 'Error Code',
                                       cell: e => e.error_code
                                   },
                                   {
                                       id: 'error-code',
                                       header: 'Description',
                                       cell: e => <pre>{e.message}</pre>
                                   }
                               ]}/>
                    </ColumnLayout>
                )
            } else if (this.isAccepted()) {
                if (this.isDryRun()) {
                    return (
                        <ColumnLayout columns={1}>
                            <h3>Dry Run Results</h3>
                            <StatusIndicator type="success">Job can be submitted successfully</StatusIndicator>
                            {this.isSubmitted() && buildPostSubmissionLinks()}
                        </ColumnLayout>
                    )
                } else {
                    return <ColumnLayout columns={1}>
                        <Box variant="h3" color="text-status-success"><FontAwesomeIcon icon={faCheckCircle}/> Job Submitted
                            Successfully</Box>
                        <SpaceBetween size="m" direction="horizontal">
                            <Button variant="normal"
                                    onClick={() => {
                                        this.resetForm()
                                    }}>Reset Form and Submit Another Job</Button>
                            <Button variant="primary"
                                    onClick={() => {
                                        this.props.navigate('/home/active-jobs')
                                    }}>Show Active Jobs</Button>
                        </SpaceBetween>
                        {this.isSubmitted() && buildPostSubmissionLinks()}
                    </ColumnLayout>
                }
            } else {
                // job was rejected without any validation entry - never render the neutral empty state.
                return (
                    <div>
                        <Box variant="h3" color="text-status-error"><FontAwesomeIcon icon={faCircleMinus}/> Job Submission Failed</Box>
                        <p>
                            <StatusIndicator type="error">
                                {(this.hasSubmissionErrors()) ? this.state.errorMessage : 'Job was not accepted by the scheduler and no reason was returned. Contact your cluster administrator.'}
                            </StatusIndicator>
                        </p>
                    </div>
                )
            }
        } else if (this.hasSubmissionErrors()) {
            return (
                <div>
                    <Box variant="h3" color="text-status-error"><FontAwesomeIcon icon={faCircleMinus}/> Job Submission Failed</Box>
                    <p>{this.state.errorMessage}</p>
                </div>
            )
        } else {
            return this.buildEmptyTabMessage()
        }
    }

    buildJobActions() {
        const canSubmitJob = () => {
            if (Utils.isEmpty(this.state.application?.application_id)) {
                return false
            }
            if (Utils.isEmpty(this.state.selectedProject?.value)) {
                return false
            }
            return true
        }
        return (
            <SpaceBetween size="xs" direction="horizontal">
                <Button variant="normal"
                        disabled={!canSubmitJob()}
                        onClick={() => this.resetForm()}><FontAwesomeIcon icon={faRefresh}/> Reset Form</Button>
                <ButtonDropdown
                    onItemClick={(event) => {
                        if (event.detail.id === 'load') {
                            this.showJobTemplatesSelectionModal()
                        } else if (event.detail.id === 'save') {
                            this.showSaveJobTemplateForm()
                        }
                    }}
                    items={[
                        {
                            id: 'save',
                            text: 'Save Job Template',
                            disabled: !canSubmitJob(),
                            disabledReason: 'Select an application before saving job template'
                        },
                        {
                            id: 'load',
                            text: 'Load Job Template'
                        }
                    ]}><FontAwesomeIcon icon={faStar}/> Job Templates</ButtonDropdown>
                <Button variant="normal"
                        disabled={!canSubmitJob()}
                        loading={this.state.dryRunLoading}
                        onClick={this.onDryRun}><FontAwesomeIcon icon={faBug}/> Dry Run</Button>
                <Button variant="primary"
                        disabled={!canSubmitJob()}
                        loading={this.state.submitJobLoading}
                        onClick={this.onSubmitJob}><FontAwesomeIcon icon={faPlay}/> Submit Job</Button>
            </SpaceBetween>
        )
    }

    getJobSubmissionParameters(): any {
        const form = this.getJobSubmissionForm()
        if (!form) {
            return
        }
        if (!form.validate()) {
            return
        }
        if (!this.state.selectedProject) {
            return
        }
        const values = form.getValues()
        return {
            ...values,
            project_name: this.state.selectedProject.value
        }
    }

    getJobSubmissionFormValues(): any {
        const form = this.getJobSubmissionForm()
        if (!form) {
            return null
        }
        return form.getValues()
    }

    /** the instance type that sizes the job: the smallest by cpu count of the selected types, or of the
     * queue profile defaults. The name is tracked so the size Alert pairs the count with its type. */
    getSmallestInstanceTypeOption(instanceTypeOptions: SocaInstanceTypeOptions[], values?: any): { name: string, cpuCount: number } | null {
        const instanceTypes = this.getSelectedInstanceTypes(values)
        let smallest: { name: string, cpuCount: number } | null = null
        instanceTypeOptions.forEach(instanceTypeOption => {
            if (instanceTypes.length > 0 && instanceTypes.indexOf(Utils.asString(instanceTypeOption.name)) < 0) {
                return
            }
            const optionCpuCount = Utils.asNumber(instanceTypeOption.threads_per_core, 0) * Utils.asNumber(instanceTypeOption.default_core_count, 0)
            if (isNaN(optionCpuCount) || optionCpuCount <= 0) {
                return
            }
            if (smallest === null || optionCpuCount < smallest.cpuCount) {
                smallest = {name: Utils.asString(instanceTypeOption.name), cpuCount: optionCpuCount}
            }
        })
        return smallest
    }

    getUnknownInstanceTypes(instanceTypeOptions: SocaInstanceTypeOptions[]): string[] {
        const instanceTypeNames = instanceTypeOptions.map(instanceTypeOption => Utils.asString(instanceTypeOption.name))
        return this.getSelectedInstanceTypes().filter(instanceType => instanceTypeNames.indexOf(instanceType) < 0)
    }

    getSelectedProjectName(): string | null {
        if (!this.state.selectedProject) {
            return null
        }
        if (Utils.isEmpty(this.state.selectedProject.value)) {
            return null
        }
        return this.state.selectedProject.value!
    }

    getSelectedQueue(values?: any): string | null {
        if (!values) {
            values = this.getJobSubmissionParameters()
        }
        if (!values) {
            return null
        }
        if(Utils.isNotEmpty(values.queue_name)) {
            return values.queue_name
        }
        if (Utils.isNotEmpty(values.queue)) {
            return values.queue
        }
        return null
    }

    getSelectedInstanceTypes(values?: any): string[] {
        if (!values) {
            values = this.getJobSubmissionParameters()
        }
        if (!values) {
            return []
        }
        if (!values.instance_type) {
            return []
        }
        return values.instance_type.split('+')
            .map((instanceType: string) => instanceType.trim())
            .filter((instanceType: string) => Utils.isNotEmpty(instanceType))
    }

    isHyperThreadingEnabled(values?: any): boolean | undefined {
        if (!values) {
            values = this.getJobSubmissionParameters()
        }
        if (!values) {
            return
        }
        if ('ht_support' in values) {
            return Utils.asBoolean(values['ht_support'], false)
        }
        if ('enable_ht_support' in values) {
            return Utils.asBoolean(values['enable_ht_support'], false)
        }
    }

    /** instance type options only change with the instance types, hyper-threading and queue selection.
     * cached so that editing cpus does not call the API on every keystroke. */
    getInstanceTypeOptionsCached(instanceTypes: string[], enableHtSupport: boolean | undefined, queueName: string | null): Promise<SocaInstanceTypeOptions[]> {
        const cacheKey = `${instanceTypes.join('+')}|${enableHtSupport}|${queueName}`
        if (this.instanceTypeOptionsCacheKey === cacheKey) {
            return Promise.resolve(this.instanceTypeOptionsCache)
        }
        return this.getSchedulerClient().getInstanceTypeOptions({
            enable_ht_support: enableHtSupport,
            instance_types: instanceTypes,
            queue_name: (queueName) ? queueName : undefined
        }).then(result => {
            this.instanceTypeOptionsCache = (result.instance_types) ? result.instance_types : []
            this.instanceTypeOptionsCacheKey = cacheKey
            return this.instanceTypeOptionsCache
        })
    }

    computeNodeCount(cpus: any, cpusPerInstance: number): number {
        if (cpusPerInstance <= 0) {
            return 0
        }
        const nodeCount = Math.ceil(Utils.asNumber(cpus, 0) / cpusPerInstance)
        if (!isFinite(nodeCount) || nodeCount <= 0) {
            return 0
        }
        return nodeCount
    }

    /** computes the no. of compute nodes the job will request, using the same inputs as the generated
     * #PBS -l select= line. returns null when it cannot be computed from the current form. */
    computeJobSizeEstimate(values: any): Promise<JobSizeEstimate | null> {
        if (!values) {
            return Promise.resolve(null)
        }
        // no early return on an empty selection: generateJobScript sizes the job from the
        // queue profile default options in that case, and the estimate must match the script
        const instanceTypes = this.getSelectedInstanceTypes(values)
        return this.getInstanceTypeOptionsCached(instanceTypes, this.isHyperThreadingEnabled(values), this.getSelectedQueue(values)).then(instanceTypeOptions => {
            const smallest = this.getSmallestInstanceTypeOption(instanceTypeOptions, values)
            if (!smallest) {
                return null
            }
            const nodeCount = this.computeNodeCount(values.cpus, smallest.cpuCount)
            if (nodeCount === 0) {
                return null
            }
            return {
                nodeCount: nodeCount,
                cpusPerInstance: smallest.cpuCount,
                instanceType: smallest.name
            }
        })
    }

    /**
     * debounced so that typing in a text based job param does not fetch instance type options per keystroke.
     */
    scheduleJobSizeEstimate() {
        if (this.jobSizeEstimateTimeout) {
            clearTimeout(this.jobSizeEstimateTimeout)
        }
        this.jobSizeEstimateTimeout = setTimeout(() => {
            this.jobSizeEstimateTimeout = null
            this.updateJobSizeEstimate()
        }, 500)
    }

    updateJobSizeEstimate(): Promise<JobSizeEstimate | null> {
        if (this.jobSizeEstimateTimeout) {
            clearTimeout(this.jobSizeEstimateTimeout)
            this.jobSizeEstimateTimeout = null
        }
        return this.computeJobSizeEstimate(this.getJobSubmissionFormValues())
            .then(jobSizeEstimate => {
                this.setState({
                    jobSizeEstimate: (jobSizeEstimate) ? jobSizeEstimate : undefined
                })
                return jobSizeEstimate
            }, () => {
                this.setState({
                    jobSizeEstimate: undefined
                })
                return null
            })
    }

    generateJobScript(): Promise<string> {
        const queueName = this.getSelectedQueue()
        const projectName = this.getSelectedProjectName()
        if (Utils.areAllEmpty(queueName, projectName)) {
            return Promise.reject(new IdeaException({
                errorCode: 'INVALID_PARAMS',
                message: 'queue or project are required to generate the job script.'
            }))
        }
        return this.getInstanceTypeOptionsCached(this.getSelectedInstanceTypes(), this.isHyperThreadingEnabled(), queueName).then(instanceTypeOptions => {

            if (!this.state.application?.job_script_template) {
                throw new IdeaException({
                    errorCode: 'JOB_SCRIPT_NOT_FOUND',
                    message: 'Application configuration is not valid. Job script template not found.'
                })
            }
            const params = this.getJobSubmissionFormTemplateParams()
            const values = this.getJobSubmissionParameters()
            if (!values) {
                throw new IdeaException({
                    errorCode: 'INVALID_PARAMS',
                    message: 'Job parameters validation failed.'
                })
            }

            this.setState({
                jobSubmissionParameters: values
            })

            // begin select statement computation
            const unknownInstanceTypes = this.getUnknownInstanceTypes(instanceTypeOptions)
            if (unknownInstanceTypes.length > 0) {
                throw new IdeaException({
                    errorCode: 'INVALID_PARAMS',
                    message: `Instance type options not found for: ${unknownInstanceTypes.join(', ')}`
                })
            }
            // size from the smallest option and label it with that option's name, so the estimate
            // and confirmation modal show a consistent type/cpu pair (matches computeJobSizeEstimate).
            const smallest = this.getSmallestInstanceTypeOption(instanceTypeOptions, values)
            if (!smallest) {
                throw new IdeaException({
                    errorCode: 'INVALID_PARAMS',
                    message: 'Node computation failed. Unable to compute the cpu count of the selected instance type.'
                })
            }
            const cpusPerInstance = smallest.cpuCount
            const requestedCpus = Utils.asNumber(values.cpus, 0)
            if (isNaN(requestedCpus) || requestedCpus <= 0) {
                throw new IdeaException({
                    errorCode: 'INVALID_PARAMS',
                    message: `Node computation failed. cpus must be greater than 0, found: ${values.cpus}`
                })
            }
            const nodeCount = Math.ceil(requestedCpus / cpusPerInstance)

            this.setState({
                jobSizeEstimate: {
                    nodeCount: nodeCount,
                    cpusPerInstance: cpusPerInstance,
                    instanceType: smallest.name
                }
            })

            // begin parameter replacement. it runs before the select update: a templated select line
            // ({{ ncpus }} / %ncpus%) is not a parsable PBS directive until the placeholders are gone.
            let jobScript = this.state.application.job_script_template!
            if (this.state.application?.job_script_type && this.state.application?.job_script_type === 'jinja2') {
                jobScript = nunjucks.renderString(jobScript, values)
            } else {
                jobScript = jobScript.replaceAll(`%project_name%`, values.project_name)
                params.forEach((param) => {
                    if (param.name && Utils.isNotEmpty(param.name)) {
                        let value = ''
                        if (param.name in values) {
                            value = values[param.name]
                            if (Utils.isEmpty(value)) {
                                value = ''
                            }
                        }
                        jobScript = jobScript.replaceAll(`%${param.name}%`, value)
                    }
                })
            }

            // chunk resources authored in the template (mpiprocs, mem, place, ...) are preserved
            return updateJobScriptSelect(jobScript, nodeCount, cpusPerInstance)
        })
    }

    submitJob(dryRun: boolean = false) {
        return new Promise((resolve, reject) => {
            this.generateJobScript().then((jobScript) => {
                if (Utils.isEmpty(jobScript)) {
                    reject(new IdeaException({
                        errorCode: 'SUBMIT_JOB_FAILED',
                        message: 'Failed to generate job submission script'
                    }))
                    return
                }
                this.updateUrlState()
                this.setState({
                    jobScript: jobScript
                }, () => {
                    this.getSchedulerClient().submitJob({
                        project: this.state.selectedProject?.value,
                        dry_run: dryRun,
                        job_script_interpreter: this.state.application?.job_script_interpreter,
                        job_script: btoa(jobScript),
                        client_submission_id: this.state.clientSubmissionId
                    }).then(result => {
                        this.setState({
                            submitJobResult: result,
                            // the id only rotates once a job is queued, so a retry after a failure
                            // returns the first result instead of submitting the job again.
                            clientSubmissionId: (!dryRun && Utils.asBoolean(result.accepted)) ? uuid() : this.state.clientSubmissionId
                        }, () => {
                            resolve({})
                        })
                    }).catch(error => {
                        reject(error)
                    })
                })
            }).catch(error => {
                reject(error)
            })
        })
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
                        text: 'Home',
                        href: '#/'
                    },
                    {
                        text: 'Submit Job',
                        href: ''
                    }
                ]}
                header={<Header variant={"h1"} actions={this.buildJobActions()}>Submit Job</Header>}
                contentType={"form"}
                content={
                    <div>
                        {this.buildApplicationSelectModal()}
                        {this.buildJobTemplatesSelectionModal()}
                        {this.buildJobSizeConfirmModal()}
                        {this.state.showSaveJobTemplatesModal && this.buildSaveJobTemplateForm()}
                        <Container className={"hpc-submit-job"}>
                            <Tabs
                                activeTabId={this.state.activeTab}
                                onChange={(event) => this.setState({
                                    activeTab: event.detail.activeTabId
                                })}
                                tabs={[
                                    {
                                        id: TAB_SUBMIT_JOB,
                                        label: 'Submit Job Form',
                                        content: (
                                            <SpaceBetween size="m" direction="vertical">
                                                <Grid gridDefinition={[{colspan: {s: 6, xxs: 12}}, {colspan: {s: 6, xxs: 12}}]}>
                                                    {this.buildSubmitJobForm()}
                                                    {this.buildSubmitJobResults()}
                                                </Grid>
                                            </SpaceBetween>
                                        )
                                    },
                                    {
                                        id: TAB_COST_ESTIMATES,
                                        disabled: !this.isSubmitted(),
                                        label: 'Cost Estimates',
                                        content: this.buildCostEstimates()
                                    },
                                    {
                                        id: TAB_SERVICE_QUOTAS,
                                        disabled: !this.isSubmitted(),
                                        label: 'AWS Service Quotas',
                                        content: this.buildServiceQuotas()
                                    },
                                    {
                                        id: TAB_BUDGET_USAGE,
                                        disabled: !this.isSubmitted(),
                                        label: 'Budget Usage',
                                        content: this.buildBudgetUsage()
                                    },
                                    {
                                        id: TAB_JOB_SCRIPT,
                                        disabled: !this.isSubmitted(),
                                        label: 'Job Script',
                                        content: (
                                            <div className={"job-script"}>
                                                <Container>
                                                    <SpaceBetween size="l" direction="vertical">
                                                        <ColumnLayout columns={2}>
                                                            <Box>
                                                                <Header variant="h3">PBS Job Script</Header>
                                                                <p>
                                                                    This is the PBS job script generated based on your inputs.
                                                                    This script will be used to submit your job to the scheduler.
                                                                </p>
                                                                {this.isDryRun() && (
                                                                    <StatusIndicator type="success">
                                                                        This script has been validated and is ready for submission.
                                                                    </StatusIndicator>
                                                                )}
                                                            </Box>
                                                            <Box textAlign="right">
                                                                <SpaceBetween size="m" direction="vertical">
                                                                    <div>
                                                                        <Button
                                                                            iconName="external"
                                                                            variant="primary"
                                                                            onClick={() => {
                                                                                if (this.state.submitJobResult?.job) {
                                                                                    const job = this.state.submitJobResult.job;
                                                                                    const jobName = job.name || '';
                                                                                    const jobUid = job.job_uid || '';
                                                                                    const username = job.owner || AppContext.get().auth().getUsername() || 'clusteradmin';
                                                                                    const filePath = `/data/home/${username}/jobs/${jobName}_${jobUid}.que`;
                                                                                    window.location.href = `/#/home/script-workbench?file=${encodeURIComponent(filePath)}`;
                                                                                }
                                                                            }}
                                                                        >
                                                                            Open in Script Workbench
                                                                        </Button>
                                                                    </div>
                                                                    {this.isDryRun() && (
                                                                        <Alert type="info">
                                                                            You can edit this script before final submission by opening it in the Script Workbench.
                                                                        </Alert>
                                                                    )}
                                                                </SpaceBetween>
                                                            </Box>
                                                        </ColumnLayout>

                                                        <Container header={<Header variant="h3">Script Contents</Header>}>
                                                            <Box variant="code" padding="m" color="text-body-secondary">
                                                                <pre style={{
                                                                    padding: '12px',
                                                                    margin: 0,
                                                                    overflowX: 'auto',
                                                                    fontFamily: 'monospace',
                                                                    lineHeight: '1.4',
                                                                    maxHeight: '600px'
                                                                }}>
                                                                    {this.state.jobScript}
                                                                </pre>
                                                            </Box>
                                                        </Container>
                                                    </SpaceBetween>
                                                </Container>
                                            </div>
                                        )
                                    },
                                    {
                                        id: TAB_JOB_PARAMETERS,
                                        disabled: !this.isSubmitted(),
                                        label: 'Job Parameters',
                                        content: (
                                            <div className={"job-parameters"}>
                                                <Box variant="code">
                                <pre style={{padding: '8px'}}>
                                    {this.state.submitJobResult && JSON.stringify(this.state.submitJobResult.job, null, 4)}
                                </pre>
                                                </Box>
                                            </div>
                                        )
                                    }
                                ]}/>
                        </Container>
                    </div>
                }
            />
        )
    }
}

export default withRouter(SubmitJob)
