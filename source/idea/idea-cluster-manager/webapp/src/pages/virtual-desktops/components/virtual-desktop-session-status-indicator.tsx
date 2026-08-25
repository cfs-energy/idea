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

import {StatusIndicator} from "@cloudscape-design/components";
import React from "react";
import {VirtualDesktopSessionState} from "../../../client/data-model";

export interface VirtualDesktopSessionStatusIndicatorProps {
    state: VirtualDesktopSessionState
    hibernation_enabled: boolean
    // last time the session changed, used to show how long a desktop has been waiting
    updated_on?: string
}

const WAITING_STATES: VirtualDesktopSessionState[] = ['PROVISIONING', 'INITIALIZING', 'CREATING', 'RESUMING']

export function waitedFor(props: VirtualDesktopSessionStatusIndicatorProps): string {
    if (!props.updated_on || WAITING_STATES.indexOf(props.state) < 0) {
        return ''
    }
    const since = new Date(props.updated_on).getTime()
    if (isNaN(since)) {
        return ''
    }
    const minutes = Math.floor((Date.now() - since) / 60000)
    if (minutes < 1) {
        return ''
    }
    return ` - ${minutes} min so far`
}

function VirtualDesktopSessionStatusIndicator(props: VirtualDesktopSessionStatusIndicatorProps) {
    const waiting = waitedFor(props)
    switch (props.state) {
        case 'PROVISIONING':
            return <StatusIndicator type="in-progress" colorOverride="blue">Provisioning{waiting}</StatusIndicator>
        case 'INITIALIZING':
            return <StatusIndicator type="in-progress" colorOverride="blue">Initializing{waiting}</StatusIndicator>
        case 'CREATING':
            return <StatusIndicator type="in-progress" colorOverride="blue">Creating{waiting}</StatusIndicator>
        case 'READY':
            return <StatusIndicator type="success">Ready</StatusIndicator>
        case 'STOPPING':
            if (props.hibernation_enabled) {
                return <StatusIndicator type="in-progress" colorOverride="blue">Hibernating</StatusIndicator>
            } else {
                return <StatusIndicator type="in-progress" colorOverride="blue">Stopping</StatusIndicator>
            }
        case 'STOPPED':
            if (props.hibernation_enabled) {
                return <StatusIndicator type="info" colorOverride="grey">Hibernated</StatusIndicator>
            } else {
                return <StatusIndicator type="info" colorOverride="grey">Stopped</StatusIndicator>
            }
        case 'RESUMING':
            return <StatusIndicator type="in-progress" colorOverride="blue">Resuming{waiting}</StatusIndicator>
        case 'DELETING':
            return <StatusIndicator type="in-progress" colorOverride="blue">Deleting</StatusIndicator>
        case 'DELETED':
            return <StatusIndicator type="in-progress" colorOverride="blue">Deleting</StatusIndicator>
        case 'ERROR':
            return <StatusIndicator type="error">Error</StatusIndicator>
    }
    return <StatusIndicator type="error">Unknown</StatusIndicator>
}

export default VirtualDesktopSessionStatusIndicator
