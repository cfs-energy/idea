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

import {VirtualDesktopClient} from "../../client";
import {VirtualDesktopSessionConnectionInfo} from "../../client/data-model";
import {AppContext} from "../../common";
import Utils from "../../common/utils";
import dot from "dot-object";
import AppLogger from "../../common/app-logger";

// created on use: a module-level logger reads window.idea before the page has set it
const logger = () => new AppLogger({name: 'desktop-connection'})

export const POPUP_BLOCKED_MESSAGE = 'Your browser blocked the new tab. Allow pop-ups for this site, then click Connect again.'

/** The connection details, or a rejection in words a person can read: a successful answer can still carry no token. */
export function getConnectionInfo(client: Pick<VirtualDesktopClient, 'getSessionConnectionInfo'>, connection_info: VirtualDesktopSessionConnectionInfo): Promise<VirtualDesktopSessionConnectionInfo> {
    return client.getSessionConnectionInfo({connection_info}).then(result => {
        if (result.connection_info?.failure_reason) {
            throw new Error(result.connection_info.failure_reason)
        }
        if (!result.connection_info?.access_token) {
            throw new Error('Connection details are unavailable. Try again in a few minutes.')
        }
        return result.connection_info
    })
}

/** Opens the desktop in a new tab. False when the browser refused the tab. */
export function openInNewTab(info: VirtualDesktopSessionConnectionInfo): boolean {
    return !!window.open(`${info.endpoint}${info.web_url_path}?authToken=${info.access_token}#${info.dcv_session_id}`)
}

/**
 * Whether the gateway serves the cluster's self-signed certificate: true only when the setting
 * (virtual-desktop-controller dcv_connection_gateway.certificate.provided) is read and says no certificate was
 * provided. A setting that cannot be read never makes the file accept an untrusted certificate.
 */
function gatewayCertificateSelfSigned(): Promise<boolean> {
    return AppContext.get().getClusterSettingsService().getVirtualDesktopSettings()
        .then(settings => {
            const provided = dot.pick('dcv_connection_gateway.certificate.provided', settings)
            if (provided == null) {
                logger().warn('gateway certificate setting not readable: the connection file leaves the check to the client')
                return false
            }
            return !Utils.asBoolean(provided, false)
        })
        .catch(error => {
            logger().warn('gateway certificate setting not readable: the connection file leaves the check to the client', error)
            return false
        })
}

/**
 * Saves the file the DCV client app opens. With the cluster's self-signed gateway certificate the file tells the
 * client to accept it; otherwise the client checks the certificate with its own trust policy.
 */
export function downloadDcvFile(info: VirtualDesktopSessionConnectionInfo, fileName: string, dcvSessionId?: string): Promise<void> {
    return gatewayCertificateSelfSigned().then(selfSigned => writeDcvFile(info, fileName, dcvSessionId, selfSigned))
}

function writeDcvFile(info: VirtualDesktopSessionConnectionInfo, fileName: string, dcvSessionId: string | undefined, selfSigned: boolean) {
    const url = new URL(info.endpoint ?? AppContext.get().getAlbEndpoint())
    let sessionFileContent = '[version]\n'
    sessionFileContent += 'format=1.0\n'
    sessionFileContent += '[connect]\n'
    sessionFileContent += `user=${AppContext.get().auth().getUsername()}\n`
    sessionFileContent += `sessionid=${info.dcv_session_id ?? dcvSessionId}\n`
    sessionFileContent += `host=${url.host}\n`
    sessionFileContent += `port=443\n`
    sessionFileContent += `webport=443\n`
    sessionFileContent += `quicport=443\n`
    sessionFileContent += `authtoken=${info.access_token}\n`
    if (selfSigned) {
        sessionFileContent += `certificatevalidationpolicy=accept-untrusted\n`
    }

    const element = document.createElement('a')
    element.setAttribute('href', 'data:text/plain;charset=utf-8,' + encodeURIComponent(sessionFileContent))
    element.setAttribute('download', `${fileName}.dcv`)
    element.style.display = 'none'
    document.body.appendChild(element)
    element.click()
    document.body.removeChild(element)
}
