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
    StatusIndicator,
    Table,
} from "@cloudscape-design/components";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faBug, faCheckCircle, faCircleMinus, faMicrochip } from "@fortawesome/free-solid-svg-icons";
import { SubmitJobResult } from "../../client/data-model";
import { FileBrowserClient, SchedulerClient } from "../../client";
import { AppContext } from "../../common";
import Utils from "../../common/utils";
import { IdeaSideNavigationProps } from "../../components/side-navigation";
import IdeaAppLayout, { IdeaAppLayoutProps } from "../../components/app-layout";
import { withRouter } from "../../navigation/navigation-utils";
import { NavigateFunction } from "react-router/dist/lib/hooks";
import { CodeEditorProps } from "@cloudscape-design/components/code-editor/interfaces";
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
// Use webpack resolver
import 'ace-builds/webpack-resolver';
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
    errorMessage: string;
    submitJobLoading: boolean;
    dryRunLoading: boolean;
    submitJobResult?: SubmitJobResult;
    ace: any;
    preferences: any;
    fileUploadError: string;
    isLoading: boolean;
    scriptModifiedSinceDryRun: boolean;
}

interface AvailableVariableProps {
    name: string
    required: boolean
    isReferenced: boolean
    description?: string
}

function AvailableVariable(props: AvailableVariableProps) {
    const getStyle = () => {
        if (props.isReferenced) {
            return {
                color: 'green'
            }
        } else {
            return {
                color: 'red'
            }
        }
    }
    const getText = (): string => {
        let s = '('
        if (props.required) {
            s += 'required'
        } else {
            s += 'optional'
        }
        if (props.description) {
            s += ', ' + props.description
        }
        s += ')'
        return s
    }
    return (
        <div>
            <code style={getStyle()}>{props.name}</code>&nbsp;&nbsp;
            <Box variant="span"
                 color="text-body-secondary">
                {getText()}
            </Box>
        </div>
    )
}

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
            errorMessage: "",
            submitJobLoading: false,
            dryRunLoading: false,
            ace: undefined,
            preferences: undefined,
            fileUploadError: "",
            isLoading: false,
            scriptModifiedSinceDryRun: false,
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

        // Check if file path is provided in URL
        const filePath = this.props.searchParams.get('file');
        if (filePath) {
            this.loadFileFromPath(filePath);
        }
    }

    componentWillUnmount() {
        // Cleanup if needed in the future
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

    isDryRunSuccessful(): boolean {
        // For dry runs, success is determined by empty validation errors AND no incidental errors
        if (!this.isDryRun()) {
            return false;
        }

        // Check if there are any validation errors
        const hasValidationErrors = this.state.submitJobResult?.validations?.results &&
                                   this.state.submitJobResult.validations.results.length > 0;

        // Check if there are any incidental errors
        const hasIncidentalErrors = this.state.submitJobResult?.incidentals?.results &&
                                   this.state.submitJobResult.incidentals.results.length > 0;

        // Dry run is successful only if there are no validation or incidental errors
        return !hasValidationErrors && !hasIncidentalErrors;
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

    isVariableReferencedInScript(name: string): boolean {
        // Check for specific PBS directives
        if (name === "project_name" || name === "my_project") {
            // Look for any #PBS -P directive
            const projectPattern = new RegExp('#PBS\\s+-P\\s+\\S+', 'i');
            return projectPattern.test(this.state.jobScript);
        }

        if (name === "job_name") {
            // Look for any #PBS -N directive
            const jobNamePattern = new RegExp('#PBS\\s+-N\\s+\\S+', 'i');
            return jobNamePattern.test(this.state.jobScript);
        }

        if (name === "instance_type") {
            // Look for instance_type in PBS resource directive
            const instanceTypePattern = new RegExp('#PBS\\s+-l\\s+(.*\\s)?instance_type=\\S+', 'i');
            return instanceTypePattern.test(this.state.jobScript);
        }

        if (name === "nodes") {
            // Look for nodes in PBS resource directive (supports two formats)
            const nodesPattern = new RegExp('#PBS\\s+-l\\s+(.*\\s)?nodes=\\S+', 'i');
            const selectPattern = new RegExp('#PBS\\s+-l\\s+(.*\\s)?select=\\S+', 'i');
            return nodesPattern.test(this.state.jobScript) || selectPattern.test(this.state.jobScript);
        }

        // Generic PBS directive check
        const pbsPattern = new RegExp(`#PBS\\s+-\\w+\\s+.*${name}.*`, 'i');
        if (pbsPattern.test(this.state.jobScript)) {
            return true;
        }

        // Regular variable check
        const varPattern1 = new RegExp(`\\$${name}\\b`, 'i');
        const varPattern2 = new RegExp(`\\${name}=`, 'i');
        return varPattern1.test(this.state.jobScript) || varPattern2.test(this.state.jobScript);
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
                errorMessage: "Job script cannot be empty",
            });
            return;
        }

        // Check all required directives
        const missingDirectives = [];

        if (!this.isVariableReferencedInScript("project_name")) {
            missingDirectives.push("project (#PBS -P your_project_name)");
        }

        if (!this.isVariableReferencedInScript("instance_type")) {
            missingDirectives.push("instance type (#PBS -l instance_type=...)");
        }

        if (!this.isVariableReferencedInScript("nodes")) {
            missingDirectives.push("nodes specification (#PBS -l nodes=... or #PBS -l select=...)");
        }

        // If directives are missing, show all of them
        if (missingDirectives.length > 0) {
            const errorMsg = missingDirectives.length === 1
                ? `The script is missing the required ${missingDirectives[0]} directive`
                : `The script is missing the following required directives: ${missingDirectives.join(", ")}`;

            this.setState({
                errorMessage: errorMsg,
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
                errorMessage: "Job script cannot be empty",
            });
            return;
        }

        if (!this.isDryRunSuccessful()) {
            this.setState({
                errorMessage: "Please run a dry run first and ensure it succeeds",
            });
            return;
        }

        if (this.state.scriptModifiedSinceDryRun) {
            this.setState({
                errorMessage: "The job script has been modified since the last successful dry run. Please run a new dry run before submitting.",
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
            this.getSchedulerClient().submitJob({
                job_script_interpreter: "pbs",
                job_script: base64Script,
                dry_run: dryRun
            }).then(result => {
                this.setState({
                    submitJobResult: result
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
                <Alert type="warning" header="Script Modified">
                    The job script has been modified since the last successful dry run.
                    Please run a new dry run to validate your changes before submitting the job.
                </Alert>
            );
        }
        return null;
    }

    buildRequiredVariablesSection() {
        return (
            <FormField
                label="Required PBS Directives"
                description="The following PBS directives are required in your job script:"
            >
                <ul>
                    <li>
                        <AvailableVariable
                            name="#PBS -P project_name"
                            isReferenced={this.isVariableReferencedInScript("project_name")}
                            required={true}
                            description="Project name directive is required"
                        />
                    </li>
                    <li>
                        <AvailableVariable
                            name="#PBS -l instance_type"
                            isReferenced={this.isVariableReferencedInScript("instance_type")}
                            required={true}
                            description="Instance type directive is required"
                        />
                    </li>
                    <li>
                        <AvailableVariable
                            name="#PBS -l nodes"
                            isReferenced={this.isVariableReferencedInScript("nodes")}
                            required={true}
                            description="Nodes/CPU specification (can also use #PBS -l select=N:ncpus=M format)"
                        />
                    </li>
                    <li>
                        <AvailableVariable
                            name="#PBS -N job_name"
                            isReferenced={this.isVariableReferencedInScript("job_name")}
                            required={false}
                            description="Job name directive"
                        />
                    </li>
                </ul>
            </FormField>
        );
    }

    buildSubmitJobResults() {
        if (this.isSubmitted()) {
            const buildCostEstimateSection = () => {
                if (this.state.submitJobResult?.estimated_bom_cost) {
                    const costData = this.state.submitJobResult.estimated_bom_cost;
                    return (
                        <Container
                            header={<Header>Cost Estimate</Header>}
                        >
                            <SpaceBetween size="l" direction="vertical">
                                {/* Walltime information alert */}
                                <Alert type="info">
                                    This cost estimate is based on the walltime specified in your job script (#PBS -l walltime=HH:MM:SS).
                                    Actual costs may vary based on job duration and resource utilization.
                                </Alert>

                                {/* Summary panel with total cost */}
                                <ColumnLayout columns={2}>
                                    <div>
                                        <Box variant="awsui-key-label">Total Estimated Cost</Box>
                                        <Box variant="h2" color="text-status-info">
                                            ${this.formatCurrency(costData.line_items_total?.amount || 0)}
                                        </Box>
                                        <Box variant="small" color="text-body-secondary">
                                            {costData.line_items_total?.unit || 'USD'}
                                        </Box>
                                    </div>

                                    {this.state.submitJobResult?.budget_usage && (
                                        <div>
                                            <Box variant="awsui-key-label">Budget Information</Box>
                                            <Box variant="h3">
                                                {this.state.submitJobResult.budget_usage.budget_name || 'N/A'}
                                            </Box>
                                            <Box variant="small" color="text-body-secondary">
                                                Usage: ${this.formatCurrency(this.state.submitJobResult.budget_usage.actual_spend?.amount || 0)} /
                                                ${this.formatCurrency(this.state.submitJobResult.budget_usage.budget_limit?.amount || 0)} {this.state.submitJobResult.budget_usage.budget_limit?.unit || 'USD'}
                                            </Box>
                                        </div>
                                    )}
                                </ColumnLayout>

                                {/* Resource cost breakdown */}
                                {costData.line_items && costData.line_items.length > 0 && (
                                    <Container
                                        header={<Header>Resource Breakdown</Header>}
                                    >
                                        <Table
                                            columnDefinitions={[
                                                {
                                                    id: "resource",
                                                    header: "Resource",
                                                    cell: (item: any) => item.title,
                                                    width: 350
                                                },
                                                {
                                                    id: "details",
                                                    header: "Details",
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
                                            empty={<Box textAlign="center">No cost data available</Box>}
                                            footer={
                                                <Box textAlign="right" variant="strong" padding="s">
                                                    Total: ${this.formatCurrency(costData.line_items_total?.amount || 0)}
                                                </Box>
                                            }
                                            variant="embedded"
                                            stickyHeader
                                        />
                                    </Container>
                                )}
                            </SpaceBetween>
                        </Container>
                    );
                }
                return null;
            };

            if (this.state.submitJobResult) {
                // Successful dry run
                if (this.isDryRun() && this.isDryRunSuccessful()) {
                    return (
                        <ColumnLayout columns={1}>
                            <Box variant="h3" color="text-status-success">
                                <FontAwesomeIcon icon={faCheckCircle} /> Dry Run Successful
                            </Box>
                            <StatusIndicator type="success">
                                The job script is valid. You can now submit your job.
                            </StatusIndicator>
                            <p>Click the <strong>Submit Job</strong> button to submit your job to the scheduler.</p>
                            {buildCostEstimateSection()}
                        </ColumnLayout>
                    );
                }
                // Failed dry run
                else if (this.isDryRun() && !this.isDryRunSuccessful()) {
                    return (
                        <SpaceBetween size="l" direction="vertical">
                            <Alert type="error" header="Validation Failed">
                                <Box variant="h3">
                                    <FontAwesomeIcon icon={faCircleMinus} /> Your job script cannot be submitted due to the following issues:
                                </Box>
                            </Alert>

                            {this.state.submitJobResult?.validations?.results && this.state.submitJobResult.validations.results.length > 0 && (
                                <Container
                                    header={<Header>Script Validation Errors</Header>}
                                >
                                    <SpaceBetween size="s" direction="vertical">
                                        <Box color="text-status-error">
                                            Please correct the following issues in your job script:
                                        </Box>
                                        <Table
                                            items={this.state.submitJobResult.validations.results.map((result, idx) => ({
                                                id: idx.toString(),
                                                message: result.message,
                                                type: "Validation Error"
                                            }))}
                                            columnDefinitions={[
                                                {
                                                    id: "type",
                                                    header: "Type",
                                                    cell: item => (
                                                        <StatusIndicator type="error">{item.type}</StatusIndicator>
                                                    ),
                                                    width: 150
                                                },
                                                {
                                                    id: "message",
                                                    header: "Message",
                                                    cell: item => item.message
                                                }
                                            ]}
                                            trackBy="id"
                                            variant="embedded"
                                            empty={<Box textAlign="center">No validation errors</Box>}
                                        />
                                        <Box variant="small">
                                            <Link external href="https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation/submit-a-job">
                                                Learn more about job submission requirements
                                            </Link>
                                        </Box>
                                    </SpaceBetween>
                                </Container>
                            )}

                            {this.state.submitJobResult?.incidentals?.results && this.state.submitJobResult.incidentals.results.length > 0 && (
                                <Container
                                    header={<Header>Authorization Errors</Header>}
                                >
                                    <SpaceBetween size="s" direction="vertical">
                                        <Box color="text-status-error">
                                            You don't have the required permissions:
                                        </Box>
                                        <Table
                                            items={this.state.submitJobResult.incidentals.results.map((result, idx) => ({
                                                id: idx.toString(),
                                                error_code: result.error_code || "Error",
                                                message: result.message
                                            }))}
                                            columnDefinitions={[
                                                {
                                                    id: "error_code",
                                                    header: "Error Type",
                                                    cell: item => (
                                                        <StatusIndicator type="warning">{item.error_code}</StatusIndicator>
                                                    ),
                                                    width: 150
                                                },
                                                {
                                                    id: "message",
                                                    header: "Message",
                                                    cell: item => item.message
                                                }
                                            ]}
                                            trackBy="id"
                                            variant="embedded"
                                            empty={<Box textAlign="center">No permission errors</Box>}
                                        />
                                        <Box variant="small">
                                            Contact your administrator if you need access to additional projects or queues.
                                        </Box>
                                    </SpaceBetween>
                                </Container>
                            )}

                            <Alert type="info">
                                <SpaceBetween size="s" direction="vertical">
                                    <div>To resolve these issues:</div>
                                    <ul>
                                        <li>Check your PBS directives for correct syntax</li>
                                        <li>Verify you're using a valid project name</li>
                                        <li>Ensure you have permission to use the specified queue</li>
                                        <li>Verify instance_type and nodes specifications</li>
                                    </ul>
                                    <div>Click <strong>Dry Run</strong> again after making changes.</div>
                                </SpaceBetween>
                            </Alert>
                        </SpaceBetween>
                    );
                }
                // Successful job submission
                else if (this.isJobSubmissionSuccessful()) {
                    return (
                        <ColumnLayout columns={1}>
                            <Box variant="h3" color="text-status-success">
                                <FontAwesomeIcon icon={faCheckCircle} /> Job Submitted Successfully
                            </Box>

                            {/* Job summary */}
                            <Container
                                header={<Header>Job Details</Header>}
                            >
                                <SpaceBetween size="l" direction="vertical">
                                    <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
                                        <Box variant="h3" color="text-label">Job ID: </Box>
                                        <Box variant="h3">{this.state.submitJobResult.job?.job_id || 'N/A'}</Box>
                                        <div style={{ width: "50px" }}></div>
                                        <Box variant="h3" color="text-label">Job Name: </Box>
                                        <Box variant="h3">{this.state.submitJobResult.job?.name || 'N/A'}</Box>
                                    </div>

                                    <Table
                                        columnDefinitions={[
                                            {
                                                id: "property",
                                                header: "Property",
                                                cell: (item: any) => item.property,
                                                width: 200
                                            },
                                            {
                                                id: "value",
                                                header: "Value",
                                                cell: (item: any) => item.value,
                                            }
                                        ]}
                                        items={[
                                            { property: "Project", value: this.state.submitJobResult.job?.project || 'N/A' },
                                            { property: "Queue", value: this.state.submitJobResult.job?.queue || 'N/A' },
                                            { property: "Owner", value: this.state.submitJobResult.job?.owner || 'N/A' },
                                            { property: "Nodes", value: this.state.submitJobResult.job?.params?.nodes || 'N/A' },
                                            { property: "CPUs", value: this.state.submitJobResult.job?.params?.cpus || 'N/A' },
                                            { property: "Walltime", value: this.state.submitJobResult.job?.params?.walltime || 'N/A' },
                                            {
                                                property: "Instance Type",
                                                value: this.state.submitJobResult.job?.params?.instance_types?.join(', ') || 'N/A'
                                            }
                                        ]}
                                        sortingDisabled
                                        trackBy="property"
                                        empty={<Box textAlign="center">No job details available</Box>}
                                        variant="embedded"
                                        stickyHeader
                                    />

                                    <Button
                                        variant="primary"
                                        iconName="external"
                                        href="#/home/active-jobs"
                                    >
                                        View Active Jobs
                                    </Button>
                                </SpaceBetween>
                            </Container>

                            <Alert type="info">
                                Your job script has been saved at <strong>~/jobs/{this.state.submitJobResult.job?.name ? `${this.state.submitJobResult.job.name}_${this.state.submitJobResult.job?.job_uid}` : this.state.submitJobResult.job?.job_uid}.que</strong> for later review or resubmission.
                            </Alert>

                            {buildCostEstimateSection()}
                        </ColumnLayout>
                    );
                }
            }
        } else if (this.hasSubmissionErrors()) {
            return (
                <Alert type="error" header="Validation Failed">
                    <SpaceBetween size="s" direction="vertical">
                        <Box variant="h3" color="text-status-error">
                            <FontAwesomeIcon icon={faCircleMinus} /> Script Validation Error
                        </Box>
                        <Box>{this.state.errorMessage}</Box>
                        <Box variant="small">
                            Please review the script requirements and try again.
                        </Box>
                    </SpaceBetween>
                </Alert>
            );
        } else {
            return (
                <Alert type="info" header={"Job Submission"}>
                    <Box variant="div">
                        <li>Enter your PBS job script in the code editor above or load it from a file.</li>
                        <li>Click <strong>Dry Run</strong> to validate your job script.</li>
                        <li>Once validation succeeds, click <strong>Submit Job</strong> to submit your job to the scheduler.</li>
                        <li><strong>Note:</strong> Any changes to the job script after a successful dry run will require a new dry run before submission.</li>
                    </Box>
                </Alert>
            );
        }
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
                text: 'Home',
                href: '#/'
            },
            {
                text: 'Script Workbench',
                href: '#/home/script-workbench'
            }
        ];

        const content = (
            <Container>
                <SpaceBetween size="l" direction="vertical">
                    <Box variant="div">
                        This form allows you to create and submit PBS job scripts directly without going through the application interface or using qsub.
                        You must include all job parameters directly in your script using #PBS directives.
                        <br/><br/>
                        For more information, see:
                        <ul>
                            <li>
                                <Link external href="https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation/submit-a-job">
                                    PBS Job Submission Documentation
                                </Link>
                            </li>
                            <li>
                                <Link external href="https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation/supported-ec2-parameters">
                                    Supported EC2 Parameters
                                </Link>
                            </li>
                        </ul>
                    </Box>

                    {this.buildRequiredVariablesSection()}

                    <FormField
                        label="PBS Job Script"
                        description={
                            <SpaceBetween size="xs" direction="vertical">
                                <span>Enter your PBS job script with #PBS directives. Use this to control instance types, node counts, and other job parameters.</span>
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
                                        Insert sample PBS script
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
                            <FontAwesomeIcon icon={faBug} /> Dry Run
                        </Button>
                    <Button
                        variant="primary"
                        loading={this.state.submitJobLoading}
                        onClick={this.onSubmitJob}
                            disabled={!this.canSubmitJob()}
                    >
                        <FontAwesomeIcon icon={faMicrochip} /> Submit Job
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
            header: <Header variant="h1">Script Workbench</Header>,
            content: content
        };

        console.log("ScriptWorkbench appLayoutProps", appLayoutProps);

        return <IdeaAppLayout {...appLayoutProps} />;
    }
}

export default withRouter(ScriptWorkbench);
