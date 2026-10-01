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

import React, {Component} from "react";

import {Badge, Box, Button, Container, CopyToClipboard, ExpandableSection, Header, Link, SpaceBetween, TextContent} from "@cloudscape-design/components";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {AppContext} from "../../common";
import Utils from "../../common/utils";
import {Constants} from "../../common/constants";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";

export interface SSHAccessProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface SSHAccessState {
    downloadPpkLoading: boolean
    downloadPemLoading: boolean
    sshHostIp: string
}

class SSHAccess extends Component<SSHAccessProps, SSHAccessState> {
    constructor(props: SSHAccessProps) {
        super(props);
        this.state = {
            downloadPpkLoading: false,
            downloadPemLoading: false,
            sshHostIp: ''
        }
    }

    componentDidMount() {
        AppContext.get().getClusterSettingsService().getModuleSettings(Constants.MODULE_BASTION_HOST).then(moduleInfo => {
            let sshHostIp: string
            if (Utils.isNotEmpty(moduleInfo.ssh_hostname)) {
                sshHostIp = Utils.asString(moduleInfo.ssh_hostname)
            } else if (Utils.asBoolean(moduleInfo.public)) {
                sshHostIp = Utils.asString(moduleInfo.public_ip)
            } else {
                sshHostIp = Utils.asString(moduleInfo.private_ip || moduleInfo.private_dns_name)
            }
            this.setState({
                sshHostIp: sshHostIp
            })
        })
    }

    onDownloadPrivateKey = (keyFormat: 'pem' | 'ppk') => {
        const state: any = {}
        if (keyFormat === 'pem') {
            state.downloadPemLoading = true
        } else if (keyFormat === 'ppk') {
            state.downloadPpkLoading = true
        }
        this.setState(state, () => {
            AppContext.get().auth().downloadPrivateKey(keyFormat).finally(() => {
                const state: any = {}
                if (keyFormat === 'pem') {
                    state.downloadPemLoading = false
                } else if (keyFormat === 'ppk') {
                    state.downloadPpkLoading = false
                }
                this.setState(state)
            })
        })
    }

    render() {
        const auth = AppContext.get().auth()
        const username = auth.getUsername()
        const keyName = (keyFormat: string) => `${username}_${auth.getClusterName()}_privatekey.${keyFormat}`
        const host = this.state.sshHostIp
        const pem = `~/.ssh/${keyName('pem')}`
        const target = `${username}@${host}`
        const alias = `${auth.getClusterName()}-${auth.getAwsRegion()}`
        const sshConfig = [
            `Host ${alias}`,
            `  User ${username}`,
            `  Hostname ${host}`,
            '  ServerAliveInterval 10',
            '  ServerAliveCountMax 2',
            `  IdentityFile ${pem}`
        ].join('\n')
        const command = (text: string) => (
            <CopyToClipboard
                variant="inline"
                textToCopy={text}
                copyButtonAriaLabel="Copy command"
                copySuccessText="Copied"
                copyErrorText="Copy failed"
            />
        )
        const labelled = (label: string, text: string) => (
            <Box key={label}>
                <Box variant="awsui-key-label">{label}</Box>
                {command(text)}
            </Box>
        )
        const step = (title: string, content: React.ReactNode, optional = false) => (
            <Box key={title}>
                <Box variant="h4" padding={{bottom: 'xxs'}}>
                    {title} {optional && <Badge>Optional</Badge>}
                </Box>
                {content}
            </Box>
        )

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
                        href: '#'
                    },
                    {
                        text: 'SSH access',
                        href: ''
                    }
                ]}
                header={<Header variant={"h1"}>SSH access</Header>}
                contentType={"default"}
                content={
                    <SpaceBetween size="l">
                        <Container header={<Header variant="h2" description="Linux, macOS and Windows 10 or later, using the built-in ssh command.">Connect from a terminal</Header>}>
                            <SpaceBetween size="l">
                                {step('1. Download your private key', (
                                    <SpaceBetween size="xs">
                                        <Box variant="p">
                                            Save it in <Box variant="code">~/.ssh</Box> on Linux and macOS,
                                            or <Box variant="code">%USERPROFILE%\.ssh</Box> on Windows.
                                        </Box>
                                        <Button
                                            variant="primary"
                                            iconName="download"
                                            loading={this.state.downloadPemLoading}
                                            onClick={() => this.onDownloadPrivateKey('pem')}
                                        >
                                            Download private key
                                        </Button>
                                    </SpaceBetween>
                                ))}
                                {step('2. Restrict the key permissions', (
                                    <SpaceBetween size="xs">
                                        {labelled('Linux and macOS', `chmod 600 ${pem}`)}
                                        {labelled('Windows (PowerShell)', `icacls "$env:USERPROFILE\\.ssh\\${keyName('pem')}" /inheritance:r /grant:r "$($env:USERNAME):R"`)}
                                    </SpaceBetween>
                                ))}
                                {step('3. Connect', command(`ssh -i ${pem} ${target}`))}
                                {step('4. Keep the session alive', (
                                    <SpaceBetween size="xs">
                                        <Box variant="p">
                                            Add this to <Box variant="code">~/.ssh/config</Box> (<Box variant="code">%USERPROFILE%\.ssh\config</Box> on
                                            Windows) so idle sessions stay open, then connect with <Box variant="code">ssh {alias}</Box>.
                                        </Box>
                                        <Box variant="code" display="block" padding="s" className="idea-code-block">{sshConfig}</Box>
                                        <CopyToClipboard
                                            variant="button"
                                            textToCopy={sshConfig}
                                            copyButtonText="Copy"
                                            copySuccessText="Copied"
                                            copyErrorText="Copy failed"
                                        />
                                    </SpaceBetween>
                                ), true)}
                                <ExpandableSection headerText="Using PuTTY">
                                    <SpaceBetween size="l">
                                        {step('1. Download your PuTTY private key', (
                                            <Button
                                                iconName="download"
                                                loading={this.state.downloadPpkLoading}
                                                onClick={() => this.onDownloadPrivateKey('ppk')}
                                            >
                                                Download private key
                                            </Button>
                                        ))}
                                        {step('2. Configure PuTTY', (
                                            <TextContent>
                                                <ul>
                                                    <li>
                                                        <Link external={true} href="https://www.chiark.greenend.org.uk/~sgtatham/putty/latest.html">
                                                            Download PuTTY
                                                        </Link>
                                                    </li>
                                                    <li>Host name: <code>{host}</code></li>
                                                    <li>
                                                        Under Connection, SSH, Auth, set the private key file
                                                        to <code>{keyName('ppk')}</code>
                                                    </li>
                                                    <li>Save the session, then open it</li>
                                                </ul>
                                            </TextContent>
                                        ))}
                                        {step('3. Keep the session alive', (
                                            <Box variant="p">
                                                Under Connection, set <strong>Seconds between keepalives</strong> to 3 so idle sessions stay open.
                                            </Box>
                                        ), true)}
                                    </SpaceBetween>
                                </ExpandableSection>
                            </SpaceBetween>
                        </Container>
                        <Container header={<Header variant="h2" description="Run these on your computer. Replace my-file.txt and my-folder with your own names.">Copy files</Header>}>
                            <SpaceBetween size="m">
                                {labelled('Copy a file to your home directory', `scp -i ${pem} my-file.txt ${target}:~/`)}
                                {labelled('Copy a file from your home directory', `scp -i ${pem} ${target}:~/my-file.txt .`)}
                                {labelled('Sync a folder (Linux and macOS)', `rsync -avz -e "ssh -i ${pem}" my-folder/ ${target}:~/my-folder/`)}
                                {labelled('Browse and transfer interactively', `sftp -i ${pem} ${target}`)}
                            </SpaceBetween>
                        </Container>
                        <Container header={<Header variant="h2" description="While a job is running, you can open a session on its compute node from the bastion host.">Connect to a job's compute node</Header>}>
                            <SpaceBetween size="l">
                                {step('1. Find the node', (
                                    <SpaceBetween size="xs">
                                        <Box variant="p">
                                            Open the job in My jobs and check its execution hosts, or run this after you connect:
                                        </Box>
                                        {command('qstat -f <job id> | grep exec_host')}
                                    </SpaceBetween>
                                ))}
                                {step('2. Connect from the bastion host', command('ssh <node host name>'))}
                            </SpaceBetween>
                        </Container>
                    </SpaceBetween>
                }/>
        )
    }
}

export default withRouter(SSHAccess)
