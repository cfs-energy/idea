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

import {IdeaAppLayoutProps} from "../../components/app-layout";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import React, {Component, RefObject} from "react";
import {withRouter} from "../../navigation/navigation-utils";
import {AppContext} from "../../common";
import Utils from "../../common/utils";
import {Box, Button, Container, Grid, StatusIndicator, StatusIndicatorProps} from "@cloudscape-design/components";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from '@xterm/addon-fit';
import "@xterm/xterm/css/xterm.css";


export interface IdeaLogTailProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface IdeaLogTailState {
    cwd: string
    logFile: string
    next_token: string
    autoScroll: boolean
    statusVisible: boolean
    statusType: StatusIndicatorProps.Type
    statusDescription: string

    pollingStatus: boolean
    emptyReceives: number
    fileIsEmpty: boolean
}

const MAX_LINE_COUNT = 1000
const MAX_EMPTY_RECEIVES = 10
const DEFAULT_POLLING_INTERVAL = 10000 // 10 seconds
const POLLING_INTERVAL = parseInt(process.env.REACT_APP_TAIL_POLLING_INTERVAL || '') || DEFAULT_POLLING_INTERVAL

/** What a failed read means for the person watching, and what they can do about it. */
export function describeTailFailure(error: {errorCode?: string, message?: string}): {type: StatusIndicatorProps.Type, text: string} {
    switch (error?.errorCode) {
        case 'TAIL_THROTTLE':
            return {type: 'warning', text: 'Too many live views are open. Close one if you have several. This one keeps trying.'}
        case 'FILE_NOT_FOUND':
            return {type: 'error', text: 'This file is gone or was moved. Close this view and go back to Files.'}
        case 'NOT_A_TEXT_FILE':
            return /too large/i.test(error.message ?? '')
                ? {type: 'error', text: 'This file is too large to watch here. Download it from Files instead.'}
                : {type: 'error', text: "This isn't a text file, so it can't be shown here. Download it from Files instead."}
        case 'UNAUTHORIZED_ACCESS':
            return {type: 'error', text: 'You do not have access to this file. If you need it, ask its owner.'}
        default:
            return {type: 'error', text: `Could not read this file: ${error?.message ?? 'unknown error'}`}
    }
}

export class IdeaLogTail extends Component<IdeaLogTailProps, IdeaLogTailState> {

    logEntries: RefObject<HTMLDivElement | null>
    interval: any
    xterm: RefObject<HTMLDivElement | null>
    terminal: Terminal
    xtermFitAddon: FitAddon

    constructor(props: IdeaLogTailProps) {
        super(props);
        this.logEntries = React.createRef()
        this.xterm = React.createRef()
        this.terminal = new Terminal()
        this.xtermFitAddon = new FitAddon()
        this.interval = null
        this.state = {
            cwd: '',
            logFile: '',
            next_token: '',
            autoScroll: true,
            statusType: 'loading',
            statusDescription: '',
            statusVisible: true,
            pollingStatus: true,
            emptyReceives: 0,
            fileIsEmpty: false
        }
    }

    startPolling() {
        if (this.interval != null) {
            return
        }
        this.interval = setInterval(() => {
            if (Utils.isEmpty(this.state.next_token)) {
                return
            }
            this.setState({
                statusVisible: true,
                statusType: "loading",
                statusDescription: 'Checking for new lines',
                pollingStatus: true
            }, () => {
                AppContext.get().client().fileBrowser().tailFile({
                    next_token: this.state.next_token,
                    line_count: MAX_LINE_COUNT,
                    file: this.state.logFile
                }).then(result => {
                    const lines = result.lines!
                    lines.forEach(line => {
                        this.newLogEntry(line)
                    })
                    let emptyReceives = this.state.emptyReceives
                    if(lines.length === 0) {
                        emptyReceives = emptyReceives + 1
                    } else {
                        emptyReceives = 0
                    }
                    this.setState({
                        statusVisible: false,
                        emptyReceives: emptyReceives,
                        fileIsEmpty: this.state.fileIsEmpty && lines.length === 0,
                        next_token: Utils.asString(result.next_token)
                    }, () => {
                        if(this.state.emptyReceives > MAX_EMPTY_RECEIVES) {
                            this.stopPolling()
                            this.setState({
                                statusVisible: true,
                                statusType: 'info',
                                statusDescription: 'Paused: no new lines for 2 minutes. Tick Auto-refresh to keep watching.'
                            })
                        }
                    })
                }).catch((error) => {
                    const failure = describeTailFailure(error)
                    this.setState({
                        statusVisible: true,
                        statusType: failure.type,
                        statusDescription: failure.text,
                        emptyReceives: this.state.emptyReceives + 1
                    }, () => {
                        if(this.state.emptyReceives > MAX_EMPTY_RECEIVES) {
                            this.stopPolling()
                            this.setState({
                                statusVisible: true,
                                statusType: 'error',
                                statusDescription: `${failure.text} Stopped trying. Tick Auto-refresh to try again.`
                            })
                        }
                    })
                })
            })
        }, POLLING_INTERVAL)
    }

    stopPolling() {
        if (this.interval != null) {
            clearInterval(this.interval)
            this.interval = null
            this.setState({
                pollingStatus: false,
                emptyReceives: 0
            })
        }
    }

    newLogEntry = (line: string) => {
        // leading space is part of the log: a traceback without it is unreadable
        this.terminal.writeln(line.trimEnd())
    }

    componentDidMount() {

        Utils.hideLoadingAnimation()

        const file = Utils.asString(this.props.searchParams.get('file'))
        const cwd = Utils.asString(this.props.searchParams.get('cwd'))
        this.setState({
            'logFile': file,
            'cwd': cwd
        })

        if (this.xterm.current) {
            this.terminal.open(this.xterm.current)
            this.terminal.options = {
                scrollback: 10000
            }
            this.terminal.onKey((event) => {
                if(event.domEvent.code === 'Enter') {
                    this.terminal.writeln('')
                }
            })
            this.xtermFitAddon.activate(this.terminal)
            this.xtermFitAddon.fit()
        }

        this.loadEndOfFile(file)
    }

    /** The first read: the end of the file, and the position to poll from. */
    loadEndOfFile(file: string) {
        AppContext.get().client().fileBrowser().tailFile({
            line_count: MAX_LINE_COUNT,
            file: file
        }).then(result => {
            const lines = result.lines!
            lines.forEach(line => {
                this.newLogEntry(line)
            })
            this.setState({
                next_token: Utils.asString(result.next_token),
                statusVisible: false,
                fileIsEmpty: lines.length === 0,
                pollingStatus: true
            }, () => {
                this.startPolling()
            })
        }).catch((error) => {
            const failure = describeTailFailure(error)
            // nothing is polling yet, so the box must not claim otherwise; ticking it tries again
            this.setState({
                statusVisible: true,
                statusType: failure.type,
                statusDescription: failure.text,
                pollingStatus: false
            })
        })
    }

    destroy(): Promise<boolean>{
        return new Promise((resolve, _) => {
            this.stopPolling()
            resolve(true)
        })
    }

    componentWillUnmount() {
        this.destroy().finally()
    }

    render() {
        return <Container
            disableContentPaddings={true}
            disableHeaderPaddings={true}
            header={
                <Grid gridDefinition={[
                    {colspan: {xxs: 9}},
                    {colspan: {xxs: 3}}
                ]}
                disableGutters={true}>
                    <Box className={"idea-log-tail-header"}>
                        <span className={"title"}>End of file, from the last {MAX_LINE_COUNT.toLocaleString()} lines</span>
                        <span className={"file-name"}>{this.state.logFile}</span>
                        <span>
                            <input type={"checkbox"} checked={this.state.pollingStatus} onChange={(event) => {
                                this.setState({
                                    pollingStatus: event.target.checked
                                }, () => {
                                    if(event.target.checked) {
                                        this.setState({statusVisible: false})
                                        if (Utils.isEmpty(this.state.next_token)) {
                                            this.loadEndOfFile(this.state.logFile)
                                        } else {
                                            this.startPolling()
                                        }
                                    } else {
                                        this.stopPolling()
                                    }
                                })
                            }}/> Auto-refresh
                        </span>
                        {this.state.fileIsEmpty && <StatusIndicator type="info">This file is empty. New lines will appear here.</StatusIndicator>}
                        {this.state.statusVisible && <StatusIndicator type={this.state.statusType}>{this.state.statusDescription}</StatusIndicator>}
                    </Box>

                    <Box textAlign={"right"} padding={{top: "xxs", right: "xs"}}>
                        <Button variant={"primary"} onClick={() => {
                            this.destroy().then(() => {
                                let url = '/home/file-browser'
                                if (Utils.isNotEmpty(this.state.cwd)) {
                                    url = `${url}?cwd=${encodeURIComponent(this.state.cwd)}`
                                }
                                this.props.navigate(url)
                            })
                        }}>Close</Button>
                    </Box>
                </Grid>
            }
        >
            <div className={"idea-log-tail"}>
                <div ref={this.xterm} className={"idea-terminal"}/>
            </div>
        </Container>
    }

}

export default withRouter(IdeaLogTail)
