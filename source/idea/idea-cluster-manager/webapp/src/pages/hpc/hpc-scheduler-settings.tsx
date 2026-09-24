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

import {SettingsSection, SettingsSource} from '../cluster-admin/settings-sections';

import React, {Component, RefObject} from "react";

import {ColumnLayout, Container, Header} from "@cloudscape-design/components";
import IdeaForm from "../../components/form";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import {IdeaAppLayoutProps} from "../../components/app-layout";
import {KeyValue} from "../../components/key-value";
import {AppContext} from "../../common";
import dot from "dot-object";
import {EnabledDisabledStatusIndicator} from "../../components/common";
import {Constants} from "../../common/constants";
import Utils from "../../common/utils";
import {withRouter} from "../../navigation/navigation-utils";

export interface HpcSchedulerSettingsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
    renderSections: (source: SettingsSource) => React.ReactNode

}

export interface HpcSchedulerSettingsState {
    moduleInfo: any
    settings: any
    cluster: any
    settingsErrors: string[]
}

class HpcSchedulerSettings extends Component<HpcSchedulerSettingsProps, HpcSchedulerSettingsState> {

    generalSettingsForm: RefObject<IdeaForm | null>

    constructor(props: HpcSchedulerSettingsProps) {
        super(props);
        this.generalSettingsForm = React.createRef()
        this.state = {
            moduleInfo: {},
            settings: {},
            cluster: {},
            settingsErrors: [],
        }
    }

    componentDidMount() {
        const service = AppContext.get().getClusterSettingsService()
        Promise.allSettled([service.getSchedulerSettings(), service.getClusterSettings()]).then(results => {
            const value = (index: number) => {
                const result = results[index]
                return result.status === 'fulfilled' ? result.value ?? {} : {}
            }
            let moduleInfo = AppContext.get().getClusterSettingsService().getModuleInfo(Constants.MODULE_SCHEDULER)
            this.setState({
                moduleInfo: moduleInfo,
                settings: value(0),
                cluster: value(1),
                settingsErrors: results.flatMap((result, index) => result.status === 'rejected' ? [`Could not read ${index === 0 ? 'job service' : 'cluster'} settings.`] : [])
            })
        })
    }

    render() {

        const sections: SettingsSection[] = [
            {
                label: 'CloudWatch Logs',
                id: 'cloudwatch-logs',
                content: (
                    <Container header={<Header variant={"h2"}>CloudWatch Logs</Header>}>
                        <ColumnLayout variant={"text-grid"} columns={3}>
                            <KeyValue title="Status">
                                <EnabledDisabledStatusIndicator enabled={Utils.asBoolean(dot.pick('cloudwatch_logs.enabled', this.state.settings)) && Utils.asBoolean(dot.pick('cloudwatch_logs.enabled', this.state.cluster))}/>
                            </KeyValue>
                            <KeyValue title="Force Flush Interval" value={dot.pick('cloudwatch_logs.force_flush_interval', this.state.cluster)} suffix={"seconds"}/>
                            <KeyValue title="Log Retention" value={dot.pick('cloudwatch_logs.retention_in_days', this.state.cluster)} suffix={"days"}/>
                        </ColumnLayout>
                    </Container>
                )
            }
        ];
        return <>{this.props.renderSections({sections, values: {scheduler: this.state.settings, cluster: this.state.cluster}, errors: this.state.settingsErrors})}
        </>
    }
}

export default withRouter(HpcSchedulerSettings)
