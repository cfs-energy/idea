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

import {Box, Button, Popover, SpaceBetween, StatusIndicator} from "@cloudscape-design/components";
import Utils from "../../common/utils";
import React from "react";
import {Project} from "../../client/data-model";

export interface EnabledDisabledStatusIndicatorProps {
    enabled: boolean
}

export function EnabledDisabledStatusIndicator(props: EnabledDisabledStatusIndicatorProps) {
    if (props.enabled) {
        return <StatusIndicator type={"success"}>Enabled</StatusIndicator>
    } else {
        return <StatusIndicator type={"stopped"}>Disabled</StatusIndicator>
    }
}

export interface CopyToClipBoardProps {
    text: string
    feedback?: string
}
export function CopyToClipBoard(props: CopyToClipBoardProps) {
    return (<Popover
        size="small"
        position="top"
        triggerType="custom"
        dismissButton={false}
        content={Utils.isNotEmpty(props.feedback) && <StatusIndicator type="success">{props.feedback}</StatusIndicator>}
    >
        <Button variant={"inline-icon"} onClick={() => Utils.copyToClipBoard(props.text)} iconName={"copy"}/>
    </Popover>)
}

export interface ProjectBedrockModelsProps {
    project?: Project
}

// the profile arn is what the project policy allows a client to pass, so say so rather than leaving
// the user to guess between the two strings.
export function ProjectBedrockModels(props: ProjectBedrockModelsProps) {
    const bedrock = props.project?.bedrock
    if (!bedrock?.enabled) {
        return <span style={{color: 'grey'}}> -- </span>
    }
    const modelIds = bedrock.model_ids
    if (!modelIds || modelIds.length === 0) {
        return <span style={{color: 'grey'}}>None</span>
    }
    const profileArns = bedrock.inference_profile_arns ?? {}
    return (
        <SpaceBetween size="xxs" direction="vertical">
            <Box fontSize="body-s" color="text-body-secondary">
                Pass the profile below as the model id. The model name on its own is refused.
            </Box>
            {
                modelIds.map((modelId, index) => {
                    const profileArn = profileArns[modelId]
                    return (
                        <div key={index}>
                            <Box variant="awsui-key-label">{modelId}</Box>
                            {profileArn
                                ? <Box fontSize="body-s" color="text-body-secondary">
                                    {profileArn}
                                    <CopyToClipBoard text={profileArn} feedback="Copied"/>
                                </Box>
                                : <Box fontSize="body-s" color="text-status-inactive">Not provisioned yet</Box>
                            }
                        </div>
                    )
                })
            }
        </SpaceBetween>
    )
}
