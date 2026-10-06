import React, { Component, createRef, RefObject } from "react";
import {
    Alert,
    Box,
    Button,
    CodeEditor,
    ColumnLayout,
    Container,
    FormField,
    Header,
    Link,
    SpaceBetween,
    Table
} from "@cloudscape-design/components";
import { v4 as uuid } from "uuid";
import { Project, SubmitJobResult } from "../../client/data-model";
import { FileBrowserClient, ProjectsClient, SchedulerClient } from "../../client";
import { ProjectBedrockModels } from "../../components/common";
import QueueReference from "./queue-reference";
import { AppContext } from "../../common";
import Utils from "../../common/utils";
import { IdeaSideNavigationProps } from "../../components/side-navigation";
import IdeaAppLayout, { IdeaAppLayoutProps } from "../../components/app-layout";
import { withRouter } from "../../navigation/navigation-utils";
import type { NavigateFunction } from "react-router-dom";
import { CodeEditorProps } from "@cloudscape-design/components/code-editor";
import 'ace-builds/css/ace.css';
import 'ace-builds/css/theme/github_light_default.css';
import 'ace-builds/css/theme/github_dark.css';
import 'ace-builds/css/theme/chrome.css';
import 'ace-builds/css/theme/xcode.css';
import 'ace-builds/css/theme/dawn.css';
import 'ace-builds/css/theme/textmate.css';
import 'ace-builds/css/theme/solarized_light.css';
import 'ace-builds/css/theme/tomorrow.css';
import 'ace-builds/css/theme/monokai.css';
import 'ace-builds/css/theme/dracula.css';
import 'ace-builds/css/theme/tomorrow_night.css';
import 'ace-builds/css/theme/solarized_dark.css';
import 'ace-builds/css/theme/twilight.css';
import 'ace-builds/css/theme/vibrant_ink.css';

// Import Ace editor directly
import ace from 'ace-builds';
// Registers mode/theme/ext loaders. Workers are not covered; this page only uses
// mode-sh, which has no worker (see src/common/ace-worker-urls.ts).
import 'ace-builds/esm-resolver';
// Import sh mode for shell script syntax highlighting
import 'ace-builds/src-noconflict/mode-sh';
// Import themes
import 'ace-builds/src-noconflict/theme-github_dark';
import 'ace-builds/src-noconflict/theme-github_light_default';

export interface ScriptWorkbenchProps extends IdeaSideNavigationProps {
    navigate: NavigateFunction;
    ideaPageId: string;
    toolsOpen: boolean;
    tools: React.ReactNode;
    onToolsChange: (event: any) => void;
    onPageChange: (event: any) => void;
    onFlashbarChange: (event: any) => void;
    flashbarItems: any[];
    searchParams: URLSearchParams;
}

export interface ScriptWorkbenchState {
    jobScript: string;
    clientSubmissionId: string;
    errorMessage: string;
    submitJobLoading: boolean;
    dryRunLoading: boolean;
    submitJobResult?: SubmitJobResult;
    ace: any;
    preferences: any;
    fileUploadError: string;
    isLoading: boolean;
    scriptModifiedSinceDryRun: boolean;
    projects: Project[];
}

/** What the scheduler does with each directive, so the list states what is really required: the
 * queue profile supplies instance types and nodes when the script leaves them out. Only the project
 * is required here, so a job is never charged to a project the user did not pick. */
const DIRECTIVES: { directive: string; meaning: string }[] = [
    {directive: '#PBS -P <project>', meaning: 'Required. The project the job runs under and is charged to.'},
    {directive: '#PBS -q <queue>', meaning: "Optional. The cluster's default queue is used if omitted."},
    {directive: '#PBS -l instance_type=<type>', meaning: "Optional. The queue's default instance types are used if omitted."},
    {directive: '#PBS -l nodes=<n> or select=<n>', meaning: "Optional. The queue's default is used if omitted."},
    {directive: '#PBS -N <name>', meaning: 'Optional. The job name.'},
];

const SAMPLE_PBS_SCRIPT = `#!/bin/bash
#PBS -N sample_job
#PBS -P default
#PBS -l instance_type=c5.large
#PBS -l nodes=1
#PBS -q normal
#PBS -l walltime=05:00:00

# This is a sample PBS script
# Change the parameters above to match your requirements
# Make sure to include all required PBS directives

echo "Job started on $(date)"
echo "Running on $(hostname)"

# Add your commands here
sleep 10
echo "Hello World!"

echo "Job completed on $(date)"
`;

class ScriptWorkbench extends Component<ScriptWorkbenchProps, ScriptWorkbenchState> {
    private fileInputRef = createRef<HTMLInputElement>();
    private editorRef: RefObject<any> = createRef();

    constructor(props: ScriptWorkbenchProps) {
        super(props);
        console.log("ScriptWorkbench constructor", props);
        this.state = {
            jobScript: "",
            clientSubmissionId: uuid(),
            errorMessage: "",
            submitJobLoading: false,
            dryRunLoading: false,
            ace: undefined,
            preferences: undefined,
            fileUploadError: "",
            isLoading: false,
            scriptModifiedSinceDryRun: false,
            projects: [],
        };
    }

    componentDidMount() {
        console.log("ScriptWorkbench componentDidMount");

        // Detect current mode and set appropriate theme
        const isDarkMode = AppContext.get().isDarkMode();
        const editorTheme = isDarkMode ? 'github_dark' : 'github_light_default';

        // Set initial preferences with appropriate theme
        this.setState({
            ace: ace,
            preferences: {
                wrapLines: true,
                theme: editorTheme,
                showGutter: true,
                showLineNumbers: true,
                showInvisibles: false,
                showPrintMargin: false
            },
            isLoading: false
        });

        this.getProjectsClient().getUserProjects({
            username: AppContext.get().auth().getUsername()
        }).then((result) => {
            this.setState({projects: result.projects ?? []});
        }).catch(() => {
            // the models block is additive: without the project list it simply does not render
            this.setState({projects: []});
        });

        // Check if file path is provided in URL
        const filePath = this.props.searchParams.get('file');
        if (filePath) {
            this.loadFileFromPath(filePath);
        }
    }

    componentWillUnmount() {
        // Cleanup if needed in the future
    }

    getProjectsClient(): ProjectsClient {
        return AppContext.get().client().projects();
    }

    /** Last `#PBS -P` value. Directives are read at the start of a line only, and qsub
     * applies the last -P, so the script is scanned from the bottom up. */
    getScriptProjectName(): string | undefined {
        const lines = this.state.jobScript.split("\n");
        for (let index = lines.length - 1; index >= 0; index--) {
            const match = lines[index].match(/^#PBS\s+-P\s+(\S+)/);
            if (match !== null) {
                return match[1].trim();
            }
        }
        return undefined;
    }

    /** The project named by the script's own #PBS -P directive, when the user is a member of it. */
    getScriptProject(): Project | undefined {
        const name = this.getScriptProjectName();
        if (name === undefined) {
            return undefined;
        }
        return this.state.projects.find((project) => project.name === name);
    }

    buildProjectAiModelsSection() {
        const project = this.getScriptProject();
        if (!project?.bedrock?.enabled) {
            return null;
        }
        return (
            <FormField
                label="AI Models"
                description={`Approved for ${project.title ?? project.name}. Compute nodes reach them through the project's instance profile, so the script does not carry credentials.`}
            >
                <ProjectBedrockModels project={project}/>
            </FormField>
        );
    }

    getSchedulerClient(): SchedulerClient {
        return AppContext.get().client().scheduler();
    }

    getFileBrowserClient(): FileBrowserClient {
        return AppContext.get().client().fileBrowser();
    }

    loadFileFromPath(filePath: string) {
        this.setState({ isLoading: true, fileUploadError: "" });

        this.getFileBrowserClient().readFile({
            file: filePath
        }).then(result => {
            if (result.content) {
                // If we had a successful dry run OR script was already modified, mark as modified
                const hadSuccessfulDryRun = this.isDryRunSuccessful();
                const shouldMarkAsModified = hadSuccessfulDryRun || this.state.scriptModifiedSinceDryRun;
                this.setState({
                    jobScript: atob(result.content),
                    isLoading: false,
                    scriptModifiedSinceDryRun: shouldMarkAsModified,
                    submitJobResult: hadSuccessfulDryRun ? undefined : this.state.submitJobResult
                });
            }
        }).catch(error => {
            this.setState({
                fileUploadError: `Failed to load file: ${error.message}`,
                isLoading: false
            });
        });
    }

    isSubmitted(): boolean {
        return this.state.submitJobResult !== undefined;
    }

    isJobSubmissionSuccessful(): boolean {
        return this.isSubmitted() &&
               !this.isDryRun() &&
               this.state.submitJobResult?.job?.job_id !== undefined &&
               this.state.submitJobResult?.job?.job_id !== 'tbd';
    }

    hasSubmissionErrors(): boolean {
        return Utils.isNotEmpty(this.state.errorMessage);
    }

    hasFileUploadError(): boolean {
        return Utils.isNotEmpty(this.state.fileUploadError);
    }

    isDryRun(): boolean {
        return this.isSubmitted() && Utils.asBoolean(this.state.submitJobResult?.dry_run);
    }

    hasValidationErrors(): boolean {
        const results = this.state.submitJobResult?.validations?.results;
        return results !== undefined && results.length > 0;
    }

    hasIncidentalErrors(): boolean {
        const results = this.state.submitJobResult?.incidentals?.results;
        return results !== undefined && results.length > 0;
    }

    isDryRunSuccessful(): boolean {
        // For dry runs, success is determined by empty validation errors AND no incidental errors
        if (!this.isDryRun()) {
            return false;
        }

        // Dry run is successful only if there are no validation or incidental errors
        return !this.hasValidationErrors() && !this.hasIncidentalErrors();
    }

    // a rejected submission is returned as a successful API call carrying accepted = false, and its
    // job keeps the placeholder id. a job script run through the bash interpreter has no job at all.
    isAccepted(): boolean {
        return this.isJobSubmissionSuccessful() || Utils.asBoolean(this.state.submitJobResult?.accepted);
    }

    canSubmitJob(): boolean {
        return !Utils.isEmpty(this.state.jobScript) &&
               this.isDryRunSuccessful() &&
               !this.state.scriptModifiedSinceDryRun;
    }

    // Helper to format currency to 2 decimal places
    formatCurrency(value: number | undefined): string {
        if (value === undefined) return '0.00';
        return value.toFixed(2);
    }

    hasProjectDirective(): boolean {
        // case-sensitive: #PBS -p is the job priority, not the project
        return /#PBS\s+-P\s+\S+/.test(this.state.jobScript);
    }

    onDryRun = () => {
        // Reset previous results first
        this.setState({
            submitJobResult: undefined,
            errorMessage: "",
            scriptModifiedSinceDryRun: false
        });

        // Validate form
        if (Utils.isEmpty(this.state.jobScript)) {
            this.setState({
                errorMessage: "Write or paste a job script first.",
            });
            return;
        }

        // instance type and nodes fall back to the queue profile defaults on the server, so only the
        // project is required before the script goes to the scheduler
        if (!this.hasProjectDirective()) {
            this.setState({
                errorMessage: "Add a #PBS -P <project> line naming the project to run the job under.",
            });
            return;
        }

        this.setState({
            dryRunLoading: true,
        }, () => {
            this.submitJob(true)
                .then(() => {
                    this.setState({
                        dryRunLoading: false,
                    });
                }, (error) => {
                    this.setState({
                        errorMessage: error.message,
                        dryRunLoading: false,
                    });
                });
        });
    }

    onSubmitJob = () => {
        // Reset previous results first
        this.setState({
            submitJobResult: undefined,
            errorMessage: ""
        });

        // Validate form
        if (Utils.isEmpty(this.state.jobScript)) {
            this.setState({
                errorMessage: "Write or paste a job script first.",
            });
            return;
        }

        if (!this.isDryRunSuccessful()) {
            this.setState({
                errorMessage: "Check the script before submitting it.",
            });
            return;
        }

        if (this.state.scriptModifiedSinceDryRun) {
            this.setState({
                errorMessage: "The script changed after it was checked. Check it again before submitting it.",
            });
            return;
        }

        this.setState({
            submitJobLoading: true,
        }, () => {
            this.submitJob(false)
                .then(() => {
                    this.setState({
                        submitJobLoading: false,
                    });
                }, (error) => {
                    this.setState({
                        errorMessage: error.message,
                        submitJobLoading: false,
                    });
                });
        });
    }

    submitJob(dryRun: boolean = false) {
        return new Promise((resolve, reject) => {
            const base64Script = btoa(this.state.jobScript);
            // the script file still carries #PBS -P, and qsub reads it. the API project is
            // what becomes qsub -P, which is the value the scheduler hook reads as the job project.
            this.getSchedulerClient().submitJob({
                project: this.getScriptProjectName(),
                job_script_interpreter: "pbs",
                job_script: base64Script,
                dry_run: dryRun,
                client_submission_id: this.state.clientSubmissionId
            }).then(result => {
                this.setState({
                    submitJobResult: result,
                    // a new id only once the job is really queued, so a retry after a
                    // timeout still deduplicates against the first attempt
                    clientSubmissionId: (!dryRun && Utils.asBoolean(result.accepted)) ? uuid() : this.state.clientSubmissionId
                }, () => {
                    // Log dry run results to help with debugging
                    if (dryRun) {
                        this.logDryRunResults();
                    }
                    resolve({});
                });
            }).catch(error => {
                reject(error);
            });
        });
    }

    resetForm = () => {
        this.setState({
            jobScript: "",
            errorMessage: "",
            submitJobResult: undefined,
            fileUploadError: "",
            scriptModifiedSinceDryRun: false
        });

        // Reset file input
        if (this.fileInputRef.current) {
            this.fileInputRef.current.value = "";
        }
    }

    getCodeEditorLanguage(): CodeEditorProps.Language {
        // Use 'sh' for shell script syntax highlighting
        return "sh";
    }

    getSampleScript(): string {
        return SAMPLE_PBS_SCRIPT;
    }

    handleFileUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
        this.setState({ fileUploadError: "" });

        const files = event.target.files;
        if (!files || files.length === 0) {
            return;
        }

        const file = files[0];
        const reader = new FileReader();

        reader.onload = (e) => {
            try {
                const content = e.target?.result as string;
                // If we had a successful dry run OR script was already modified, mark as modified
                const hadSuccessfulDryRun = this.isDryRunSuccessful();
                const shouldMarkAsModified = hadSuccessfulDryRun || this.state.scriptModifiedSinceDryRun;
                this.setState({
                    jobScript: content,
                    fileUploadError: "",
                    scriptModifiedSinceDryRun: shouldMarkAsModified,
                    submitJobResult: hadSuccessfulDryRun ? undefined : this.state.submitJobResult
                });
            } catch (error) {
                this.setState({
                    fileUploadError: "Failed to read file content. Please try again."
                });
            }
        };

        reader.onerror = () => {
            this.setState({
                fileUploadError: "Error reading file. Please try again."
            });
        };

        reader.readAsText(file);
    }

    triggerFileInput = () => {
        if (this.fileInputRef.current) {
            this.fileInputRef.current.click();
        }
    }

    buildScriptModifiedWarning() {
        if (this.state.scriptModifiedSinceDryRun) {
            return (
                <Alert type="warning" header="Script changed after it was checked">
                    Check it again before submitting it.
                </Alert>
            );
        }
        return null;
    }

    buildDirectivesSection() {
        return (
            <SpaceBetween size="m">
            <QueueReference/>
            <FormField label="Directives">
                <SpaceBetween size="xxs" direction="vertical">
                    {DIRECTIVES.map(({directive, meaning}) => (
                        <div key={directive}>
                            <Box variant="code">{directive}</Box>{' '}
                            <Box variant="span" color="text-body-secondary">{meaning}</Box>
                        </div>
                    ))}
                </SpaceBetween>
            </FormField>
            </SpaceBetween>
        );
    }

    buildCostEstimateSection() {
        const costData = this.state.submitJobResult?.estimated_bom_cost;
        if (!costData) {
            return null;
        }
        const budget = this.state.submitJobResult?.budget_usage;
        return (
            <Container
                header={<Header description="Based on the walltime in the script (#PBS -l walltime). The actual cost depends on how long the job runs.">Estimated cost</Header>}
            >
                <SpaceBetween size="l" direction="vertical">
                    <ColumnLayout columns={2}>
                        <div>
                            <Box variant="awsui-key-label">Total</Box>
                            <Box variant="h2">
                                ${this.formatCurrency(costData.line_items_total?.amount || 0)} {costData.line_items_total?.unit || 'USD'}
                            </Box>
                        </div>
                        {budget && (
                            <div>
                                <Box variant="awsui-key-label">Budget</Box>
                                <Box variant="h3">{budget.budget_name || 'N/A'}</Box>
                                <Box variant="small" color="text-body-secondary">
                                    ${this.formatCurrency(budget.actual_spend?.amount || 0)} spent of
                                    ${this.formatCurrency(budget.budget_limit?.amount || 0)} {budget.budget_limit?.unit || 'USD'}
                                </Box>
                            </div>
                        )}
                    </ColumnLayout>

                    {costData.line_items && costData.line_items.length > 0 && (
                        <Table
                            header={<Header variant="h3">By resource</Header>}
                            columnDefinitions={[
                                {id: "resource", header: "Resource", cell: (item: any) => item.title, width: 350},
                                {
                                    id: "details",
                                    header: "Quantity",
                                    cell: (item: any) => {
                                        const formattedQuantity = Number.isInteger(item.quantity)
                                            ? item.quantity
                                            : this.formatCurrency(item.quantity);
                                        const unitDisplay = item.unit === "per hour" ? "hours" : item.unit;
                                        return `${formattedQuantity} ${unitDisplay}`;
                                    },
                                    width: 140
                                },
                                {
                                    id: "rate",
                                    header: "Rate",
                                    cell: (item: any) => {
                                        const price = this.formatCurrency(item.unit_price?.amount || 0);
                                        const rateLabel = item.unit === "per hour" ? "/hour" : "";
                                        return `$${price}${rateLabel}`;
                                    },
                                    width: 100
                                },
                                {
                                    id: "cost",
                                    header: "Cost",
                                    cell: (item: any) => `$${this.formatCurrency(item.total_price?.amount || 0)}`,
                                    width: 100
                                }
                            ]}
                            items={costData.line_items}
                            sortingDisabled
                            trackBy="title"
                            variant="embedded"
                        />
                    )}
                </SpaceBetween>
            </Container>
        );
    }

    /** The scheduler's own messages, verbatim: they name the fix (for example the queues that run an
     * instance type's architecture), so the page adds no advice of its own. */
    buildServerMessages() {
        const messages = [
            ...(this.state.submitJobResult?.validations?.results ?? []),
            ...(this.state.submitJobResult?.incidentals?.results ?? [])
        ].map((result) => result.message).filter((message): message is string => Utils.isNotEmpty(message));
        if (messages.length === 0) {
            return <Box>The scheduler did not return a reason. Contact your cluster administrator.</Box>;
        }
        if (messages.length === 1) {
            return <Box>{messages[0]}</Box>;
        }
        return (
            <ul>
                {messages.map((message, index) => <li key={index}>{message}</li>)}
            </ul>
        );
    }

    buildSubmitJobResults() {
        const result = this.state.submitJobResult;
        if (result === undefined) {
            if (this.hasSubmissionErrors()) {
                return <Alert type="error">{this.state.errorMessage}</Alert>;
            }
            return null;
        }

        if (this.isDryRun()) {
            if (!this.isDryRunSuccessful()) {
                return (
                    <Alert type="error" header="Fix the script before submitting it">
                        {this.buildServerMessages()}
                    </Alert>
                );
            }
            return (
                <SpaceBetween size="l" direction="vertical">
                    <Alert type="success" header="Script passed the check">
                        Submit it to queue the job.
                    </Alert>
                    {this.buildCostEstimateSection()}
                </SpaceBetween>
            );
        }

        // a rejected submission is returned as a successful API call carrying accepted = false, with
        // the reasons in validations/incidentals.
        if (!this.isAccepted()) {
            return (
                <Alert type="error" header="Job submission failed">
                    {this.buildServerMessages()}
                </Alert>
            );
        }

        const job = result.job;
        const savedAs = job?.name ? `${job.name}_${job.job_uid}` : job?.job_uid;
        return (
            <SpaceBetween size="l" direction="vertical">
                <Alert
                    type="success"
                    header={`Job ${job?.job_id || ''} submitted`}
                    action={<Button href="#/home/active-jobs">View active jobs</Button>}
                >
                    The script is saved as <Box variant="code">~/jobs/{savedAs}.que</Box> for later review or resubmission.
                </Alert>
                <Container header={<Header>Job details</Header>}>
                    <Table
                        columnDefinitions={[
                            {id: "property", header: "Property", cell: (item: any) => item.property, width: 200},
                            {id: "value", header: "Value", cell: (item: any) => item.value}
                        ]}
                        items={[
                            {property: "Name", value: job?.name || 'N/A'},
                            {property: "Project", value: job?.project || 'N/A'},
                            {property: "Queue", value: job?.queue || 'N/A'},
                            {property: "Owner", value: job?.owner || 'N/A'},
                            {property: "Nodes", value: job?.params?.nodes || 'N/A'},
                            {property: "CPUs", value: job?.params?.cpus || 'N/A'},
                            {property: "Walltime", value: job?.params?.walltime || 'N/A'},
                            {property: "Instance type", value: job?.params?.instance_types?.join(', ') || 'N/A'}
                        ]}
                        sortingDisabled
                        trackBy="property"
                        variant="embedded"
                    />
                </Container>
                {this.buildCostEstimateSection()}
            </SpaceBetween>
        );
    }

    // Add a debugging method to help troubleshoot dry run results
    logDryRunResults(): void {
        if (this.isDryRun() && this.state.submitJobResult) {
            console.log("Dry Run Results:", {
                isDryRun: this.isDryRun(),
                validations: this.state.submitJobResult.validations,
                incidentals: this.state.submitJobResult.incidentals,
                isDryRunSuccessful: this.isDryRunSuccessful()
            });
        }
    }

    render() {
        console.log("ScriptWorkbench render", this.props, this.state);

        const breadcrumbs = [
            {
                text: 'IDEA',
                href: '#/'
            },
            {
                text: 'Write script',
                href: '#/home/script-workbench'
            }
        ];

        const content = (
            <Container>
                <SpaceBetween size="l" direction="vertical">
                    <Box variant="p">
                        Write or paste a PBS job script with #PBS directives and submit it to a queue. It is checked
                        before it is submitted. See{' '}
                        <Link external href="https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation/submit-a-job">
                            Submitting a job
                        </Link>{' '}and{' '}
                        <Link external href="https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation/supported-ec2-parameters">
                            Supported EC2 parameters
                        </Link>.
                    </Box>

                    {this.buildDirectivesSection()}
                    {this.buildProjectAiModelsSection()}

                    <FormField
                        label="Job script"
                        description={
                            <SpaceBetween size="xs" direction="vertical">
                                <SpaceBetween size="s" direction="horizontal">
                                    <Button
                                        variant="normal"
                                        onClick={() => {
                                            const hadSuccessfulDryRun = this.isDryRunSuccessful();
                                            const shouldMarkAsModified = hadSuccessfulDryRun || this.state.scriptModifiedSinceDryRun;
                                            this.setState({
                                                jobScript: this.getSampleScript(),
                                                scriptModifiedSinceDryRun: shouldMarkAsModified,
                                                submitJobResult: hadSuccessfulDryRun ? undefined : this.state.submitJobResult
                                            });
                                        }}
                                        iconName="add-plus"
                                    >
                                        Insert sample script
                                    </Button>
                                    <Button
                                        variant="normal"
                                        onClick={this.triggerFileInput}
                                        iconName="upload"
                                    >
                                        Upload file
                                    </Button>
                                    <Button
                                        variant="normal"
                                        onClick={() => {
                                            this.props.navigate('/home/file-browser');
                                        }}
                                        iconName="folder"
                                    >
                                        Browse files
                                    </Button>
                                    <input
                                        type="file"
                                        ref={this.fileInputRef}
                                        style={{ display: 'none' }}
                                        onChange={this.handleFileUpload}
                                        accept="text/plain,.sh,.que"
                                    />
                                </SpaceBetween>
                                {this.hasFileUploadError() && (
                                    <Box color="text-status-error">
                                        {this.state.fileUploadError}
                                    </Box>
                                )}
                            </SpaceBetween>
                        }
                        stretch={true}
                    >
                        <CodeEditor
                            ref={this.editorRef}
                            id="job-script-editor"
                            ace={this.state.ace}
                            language={this.getCodeEditorLanguage()}
                            value={this.state.jobScript}
                            preferences={this.state.preferences}
                            onPreferencesChange={e => this.setState({
                                preferences: e.detail
                            })}
                            onChange={(e) => {
                                // If we had a successful dry run OR script was already modified, mark as modified
                                const hadSuccessfulDryRun = this.isDryRunSuccessful();
                                const shouldMarkAsModified = hadSuccessfulDryRun || this.state.scriptModifiedSinceDryRun;

                                this.setState({
                                    jobScript: e.detail.value,
                                    scriptModifiedSinceDryRun: shouldMarkAsModified,
                                    submitJobResult: hadSuccessfulDryRun ? undefined : this.state.submitJobResult
                                });
                            }}
                            loading={!this.state.ace || this.state.isLoading}
                            themes={{
                                light: [
                                    'github_light_default',
                                    'chrome',
                                    'xcode',
                                    'dawn',
                                    'textmate',
                                    'solarized_light',
                                    'tomorrow'
                                ],
                                dark: [
                                    'github_dark',
                                    'monokai',
                                    'dracula',
                                    'tomorrow_night',
                                    'solarized_dark',
                                    'twilight',
                                    'vibrant_ink'
                                ]
                            }}
                            i18nStrings={{
                                loadingState: "Loading code editor",
                                errorState: "There was an error loading the code editor.",
                                errorStateRecovery: "Retry",
                                editorGroupAriaLabel: "Code editor",
                                statusBarGroupAriaLabel: "Status bar",
                                cursorPosition: (row: number, column: number) => `Ln ${row}, Col ${column}`,
                                errorsTab: "Errors",
                                warningsTab: "Warnings",
                                preferencesButtonAriaLabel: "Preferences",
                                paneCloseButtonAriaLabel: "Close",
                                preferencesModalHeader: "Preferences",
                                preferencesModalCancel: "Cancel",
                                preferencesModalConfirm: "Confirm",
                                preferencesModalWrapLines: "Wrap lines",
                                preferencesModalTheme: "Theme",
                                preferencesModalLightThemes: "Light themes",
                                preferencesModalDarkThemes: "Dark themes"
                            }}
                        />
                    </FormField>

                    <SpaceBetween size="xs" direction="horizontal">
                        <Button
                            variant="normal"
                            loading={this.state.dryRunLoading}
                            onClick={this.onDryRun}
                            disabled={Utils.isEmpty(this.state.jobScript) || (this.isDryRunSuccessful() && !this.state.scriptModifiedSinceDryRun)}
                        >
                            Check script
                        </Button>
                        <Button
                            variant="primary"
                            loading={this.state.submitJobLoading}
                            onClick={this.onSubmitJob}
                            disabled={!this.canSubmitJob()}
                        >
                            Submit job
                        </Button>
                    </SpaceBetween>

                    {this.buildScriptModifiedWarning()}

                    {this.buildSubmitJobResults()}
                </SpaceBetween>
            </Container>
        );

        const appLayoutProps: IdeaAppLayoutProps = {
            ...this.props,
            contentType: "default",
            breadcrumbItems: breadcrumbs,
            header: <Header variant="h1">Write script</Header>,
            content: content
        };

        console.log("ScriptWorkbench appLayoutProps", appLayoutProps);

        return <IdeaAppLayout {...appLayoutProps} />;
    }
}

export default withRouter(ScriptWorkbench);
