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
    // the desktop is up but its remote session is being restarted, so it is not usable yet
    recovering?: boolean
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

/** One word per state, shared by the status line, the card button and the preview. */
export function desktopStateLabel(state: VirtualDesktopSessionState | undefined, hibernation_enabled?: boolean): string {
    switch (state) {
        case 'PROVISIONING':
        case 'INITIALIZING':
        case 'CREATING':
            return 'Setting up'
        case 'READY':
            return 'Ready'
        case 'STOPPING':
            return hibernation_enabled ? 'Hibernating' : 'Stopping'
        case 'STOPPED':
            return hibernation_enabled ? 'Hibernated' : 'Stopped'
        case 'RESUMING':
            return 'Starting'
        case 'DELETING':
        case 'DELETED':
            return 'Terminating'
        case 'ERROR':
            return 'Failed'
    }
    return 'Unknown'
}

function VirtualDesktopSessionStatusIndicator(props: VirtualDesktopSessionStatusIndicatorProps) {
    if (props.recovering) {
        return <StatusIndicator type="in-progress" colorOverride="blue">Restarting</StatusIndicator>
    }
    const label = desktopStateLabel(props.state, props.hibernation_enabled)
    switch (props.state) {
        case 'READY':
            return <StatusIndicator type="success">{label}</StatusIndicator>
        case 'STOPPED':
            return <StatusIndicator type="info" colorOverride="grey">{label}</StatusIndicator>
        case 'PROVISIONING':
        case 'INITIALIZING':
        case 'CREATING':
        case 'RESUMING':
            return <StatusIndicator type="in-progress" colorOverride="blue">{label}{waitedFor(props)}</StatusIndicator>
        case 'STOPPING':
        case 'DELETING':
        case 'DELETED':
            return <StatusIndicator type="in-progress" colorOverride="blue">{label}</StatusIndicator>
    }
    return <StatusIndicator type="error">{label}</StatusIndicator>
}

export default VirtualDesktopSessionStatusIndicator
