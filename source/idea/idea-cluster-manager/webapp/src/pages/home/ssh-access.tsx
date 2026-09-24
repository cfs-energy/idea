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

import {Badge, Box, Button, Container, CopyToClipboard, Grid, Header, Link, SpaceBetween, TextContent} from "@cloudscape-design/components";
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
            if (Utils.asBoolean(moduleInfo.public)) {
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
        const alias = `${auth.getClusterName()}-${auth.getAwsRegion()}`
        const sshConfig = [
            `Host ${alias}`,
            `  User ${username}`,
            `  Hostname ${host}`,
            '  ServerAliveInterval 10',
            '  ServerAliveCountMax 2',
            `  IdentityFile ~/.ssh/${keyName('pem')}`
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
                        text: 'Home',
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
                    <Grid gridDefinition={[
                        {colspan: {xxs: 12, s: 6}},
                        {colspan: {xxs: 12, s: 6}}
                    ]}>
                        <Container header={<Header variant="h2" description="Connect from a terminal.">Linux and macOS</Header>}>
                            <SpaceBetween size="l">
                                {step('1. Download your private key', (
                                    <SpaceBetween size="xs">
                                        <Box variant="p">Save it in your ~/.ssh directory.</Box>
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
                                {step('2. Restrict the key permissions', command(`chmod 600 ~/.ssh/${keyName('pem')}`))}
                                {step('3. Connect', command(`ssh -i ~/.ssh/${keyName('pem')} ${username}@${host}`))}
                                {step('4. Keep the session alive', (
                                    <SpaceBetween size="xs">
                                        <Box variant="p">
                                            Add this to <Box variant="code">~/.ssh/config</Box> so idle sessions stay open,
                                            then connect with <Box variant="code">ssh {alias}</Box>.
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
                            </SpaceBetween>
                        </Container>
                        <Container header={<Header variant="h2" description="Connect with PuTTY.">Windows</Header>}>
                            <SpaceBetween size="l">
                                {step('1. Download your PuTTY private key', (
                                    <Button
                                        variant="primary"
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
                        </Container>
                    </Grid>
                }/>
        )
    }
}

export default withRouter(SSHAccess)
