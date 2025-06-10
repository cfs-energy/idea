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

import {Box, Button, Header, Modal, Tabs} from "@cloudscape-design/components";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import {faDownload, faExternalLinkAlt} from "@fortawesome/free-solid-svg-icons";
import React from "react";
import {VirtualDesktopSession} from "../../../client/data-model";
import {AppContext} from "../../../common";

function downloadDcvClient(os: string) {
    try {
        const client_settings = AppContext.get()?.getClusterSettingsService()?.globalSettings?.package_config?.dcv?.clients;
        if (!client_settings) {
            console.warn('DCV client settings not available');
            return;
        }

        if (os === 'windows-zip' && client_settings.windows?.zip?.url) {
            window.open(client_settings.windows.zip.url);
        } else if (os === 'windows-msi' && client_settings.windows?.msi?.url) {
            window.open(client_settings.windows.msi.url);
        } else if (os === 'macos-m1' && client_settings.macos?.m1?.url) {
            window.open(client_settings.macos.m1.url);
        } else if (os === 'macos-intel' && client_settings.macos?.intel?.url) {
            window.open(client_settings.macos.intel.url);
        } else if (os === 'linux-rhel_centos7' && client_settings.linux?.rhel_centos7?.url) {
            window.open(client_settings.linux.rhel_centos7.url);
        } else if (os === 'linux-rhel_centos_rocky8' && client_settings.linux?.rhel_centos_rocky8?.url) {
            window.open(client_settings.linux.rhel_centos_rocky8.url);
        } else if (os === 'linux-rhel_centos_rocky9' && client_settings.linux?.rhel_centos_rocky9?.url) {
            window.open(client_settings.linux.rhel_centos_rocky9.url);
        } else if (os === 'linux-suse15' && client_settings.linux?.suse15?.url) {
            window.open(client_settings.linux.suse15.url);
        } else if (os === 'ubuntu-ubuntu2004' && client_settings.linux?.ubuntu2004?.url) {
            window.open(client_settings.linux.ubuntu2004.url);
        } else if (os === 'ubuntu-ubuntu2204' && client_settings.linux?.ubuntu2204?.url) {
            window.open(client_settings.linux.ubuntu2204.url);
        } else if (os === 'ubuntu-ubuntu2404' && client_settings.linux?.ubuntu2404?.url) {
            window.open(client_settings.linux.ubuntu2404.url);
        }
    } catch (error) {
        console.error('Error downloading DCV client:', error);
    }
}

function getDCVClientLabelForOSFlavor(os: string): string {
    try {
        const client_settings = AppContext.get()?.getClusterSettingsService()?.globalSettings?.package_config?.dcv?.clients;

        if (!client_settings) {
            console.warn('DCV client settings not available');
            return os; // Return the OS name as fallback
        }

        // Safe property access with optional chaining
        if (os === 'windows-zip') {
            return client_settings.windows?.zip?.label || 'Windows (ZIP)';
        } else if (os === 'windows-msi') {
            return client_settings.windows?.msi?.label || 'Windows (MSI)';
        } else if (os === 'macos-m1') {
            return client_settings.macos?.m1?.label || 'macOS (Apple Silicon)';
        } else if (os === 'macos-intel') {
            return client_settings.macos?.intel?.label || 'macOS (Intel)';
        } else if (os === 'linux-rhel_centos7') {
            return client_settings.linux?.rhel_centos7?.label || 'RHEL/CentOS 7';
        } else if (os === 'linux-rhel_centos_rocky8') {
            return client_settings.linux?.rhel_centos_rocky8?.label || 'RHEL/CentOS/Rocky 8';
        } else if (os === 'linux-rhel_centos_rocky9') {
            return client_settings.linux?.rhel_centos_rocky9?.label || 'RHEL/CentOS/Rocky 9';
        } else if (os === 'linux-suse15') {
            return client_settings.linux?.suse15?.label || 'SUSE 15';
        } else if (os === 'ubuntu-ubuntu2004') {
            return client_settings.linux?.ubuntu2004?.label || 'Ubuntu 20.04';
        } else if (os === 'ubuntu-ubuntu2204') {
            return client_settings.linux?.ubuntu2204?.label || 'Ubuntu 22.04';
        } else if (os === 'ubuntu-ubuntu2404') {
            return client_settings.linux?.ubuntu2404?.label || 'Ubuntu 24.04';
        }
        return os; // Return the OS name as fallback
    } catch (error) {
        console.error('Error getting DCV client label:', error);
        return os; // Return the OS name as fallback
    }
}

export interface DcvClientHelpModalProps {
    session: VirtualDesktopSession
    onDismiss: () => void
    onDownloadDcvSessionFile: (session: VirtualDesktopSession) => Promise<boolean>
    onLaunchSession: (session: VirtualDesktopSession) => Promise<boolean>
    visible: boolean
}

export function DcvClientHelpModal(props: DcvClientHelpModalProps) {
    return (
        <Modal visible={props.visible}
               onDismiss={props.onDismiss}
               size="large"
               header={
                   <Header>How to connect to your Virtual Desktop?</Header>
               }
               footer={
                   <Box float="right">
                       <Button variant="primary" onClick={props.onDismiss}>Close</Button>
                   </Box>
               }
        >

            <Tabs tabs={[
                {
                    label: 'Windows',
                    id: 'dcv-native-windows',
                    content: (
                        <div>
                            <p>
                                <strong>Step 1)</strong> Download DCV Windows Client.
                            </p>
                            <p>
                                <Button variant={"link"} onClick={() => downloadDcvClient('windows-zip')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('windows-zip')}</Button>
                                <Button variant={"link"} onClick={() => downloadDcvClient('windows-msi')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('windows-msi')}</Button>
                            </p>
                            <p>
                                <strong>Step 2)</strong> Install the DCV client on your computer.
                            </p>
                            <p>
                                <strong>Step 3)</strong> Download your virtual desktop connection file. (DCV Session File)
                                <Button disabled={props.session.state !== 'READY'} variant={"link"} onClick={() => props.onDownloadDcvSessionFile(props.session).finally()}><FontAwesomeIcon icon={faDownload}/> Download</Button>
                            </p>
                            <p>
                                <strong>Step 4)</strong> Open your .dcv (DCV Session File) with DCV viewer client.
                            </p>
                        </div>
                    )
                },
                {
                    label: 'Mac OS',
                    id: 'dcv-native-mac-os',
                    content: (
                        <div>
                            <p>
                                <strong>Step 1)</strong> Download DCV MacOS Client.
                            </p>
                            <p>
                                <Button variant={"link"} onClick={() => downloadDcvClient('macos-m1')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('macos-m1')}</Button>
                                <Button variant={"link"} onClick={() => downloadDcvClient('macos-intel')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('macos-intel')}</Button>
                            </p>
                            <p>
                                <strong>Step 2)</strong> Install the DCV client on your computer.
                            </p>
                            <p>
                                <strong>Step 3)</strong> Download your virtual desktop connection file. (DCV Session File)
                                <Button disabled={props.session.state !== 'READY'} variant={"link"} onClick={() => props.onDownloadDcvSessionFile(props.session).finally()}><FontAwesomeIcon icon={faDownload}/> Download</Button>
                            </p>
                            <p>
                                <strong>Step 4)</strong> Open your .dcv (DCV Session File) with DCV viewer client.
                            </p>
                        </div>
                    )
                },
                {
                    label: 'Linux',
                    id: 'dcv-native-linux',
                    content: (
                        <div>
                            <p>
                                <strong>Step 1)</strong> Download DCV Linux Client.
                            </p>
                            <p>
                                <Button variant={"link"} onClick={() => downloadDcvClient('linux-rhel_centos7')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('linux-rhel_centos7')}</Button>
                                <Button variant={"link"} onClick={() => downloadDcvClient('linux-rhel_centos_rocky8')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('linux-rhel_centos_rocky8')}</Button>
                                <Button variant={"link"} onClick={() => downloadDcvClient('linux-rhel_centos_rocky9')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('linux-rhel_centos_rocky9')}</Button>
                                <Button variant={"link"} onClick={() => downloadDcvClient('linux-suse15')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('linux-suse15')}</Button>
                            </p>
                            <p>
                                <strong>Step 2)</strong> Install the DCV client on your computer.
                            </p>
                            <p>
                                <strong>Step 3)</strong> Download your virtual desktop connection file. (DCV Session File)
                                <Button disabled={props.session.state !== 'READY'} variant={"link"} onClick={() => props.onDownloadDcvSessionFile(props.session).finally()}><FontAwesomeIcon icon={faDownload}/> Download</Button>
                            </p>
                            <p>
                                <strong>Step 4)</strong> Open your .dcv (DCV Session File) with DCV viewer client.
                            </p>
                        </div>
                    )
                },
                {
                    label: 'Ubuntu',
                    content: (
                        <div>
                            <p>
                                <strong>Step 1)</strong> Download DCV Ubuntu Client.
                            </p>
                            <p>
                                <Button variant={"link"} onClick={() => downloadDcvClient('ubuntu-ubuntu2004')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('ubuntu-ubuntu2004')}</Button>
                                <Button variant={"link"} onClick={() => downloadDcvClient('ubuntu-ubuntu2204')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('ubuntu-ubuntu2204')}</Button>
                                <Button variant={"link"} onClick={() => downloadDcvClient('ubuntu-ubuntu2404')}><FontAwesomeIcon icon={faDownload}/> {getDCVClientLabelForOSFlavor('ubuntu-ubuntu2404')}</Button>
                            </p>
                            <p>
                                <strong>Step 2)</strong> Install the DCV client on your computer.
                            </p>
                            <p>
                                <strong>Step 3)</strong> Download your virtual desktop connection file. (DCV Session File)
                                <Button disabled={props.session.state !== 'READY'} variant={"link"} onClick={() => props.onDownloadDcvSessionFile(props.session).finally()}><FontAwesomeIcon icon={faDownload}/> Download</Button>
                            </p>
                            <p>
                                <strong>Step 4)</strong> Open your .dcv (DCV Session File) with DCV viewer client.
                            </p>
                        </div>
                    ),
                    id: 'dcv-native-ubuntu'
                },
                {
                    label: 'Web Browser',
                    id: 'web-browser',
                    content: (
                        <div>
                            <p>
                                Connect using your Web Browser (One-Click).
                            </p>
                            <p>
                                <Button disabled={props.session.state !== 'READY'} variant="primary" onClick={() => props.onLaunchSession(props.session).finally()}><FontAwesomeIcon icon={faExternalLinkAlt}/> Connect</Button>
                            </p>
                        </div>
                    )
                }
            ]}/>
        </Modal>
    )
}
