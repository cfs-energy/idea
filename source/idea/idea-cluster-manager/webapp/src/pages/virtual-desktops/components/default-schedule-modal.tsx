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

// Default Schedule Modal
import {VirtualDesktopSchedule, VirtualDesktopWeekSchedule} from "../../../client/data-model";
import React, {Component, RefObject} from "react";
import IdeaTimeRangeSlider from "../../../components/time-range-slider";
import {IdeaFormField} from "../../../components/form-field";
import moment from "moment";
import {Alert, Box, Button, ColumnLayout, Form, Header, Modal, SpaceBetween} from "@cloudscape-design/components";
import {AppContext} from "../../../common";

// Day Of Week Schedule Component (reusing the same structure)
interface DefaultScheduleDayOfWeekScheduleProps {
    dayOfWeek: string
    schedule?: VirtualDesktopSchedule
    working_hours_start: string
    working_hours_end: string
}

interface DefaultScheduleDayOfWeekScheduleState {
    schedule: VirtualDesktopSchedule
}

class DefaultScheduleDayOfWeekSchedule extends Component<DefaultScheduleDayOfWeekScheduleProps, DefaultScheduleDayOfWeekScheduleState> {

    timeRangeSlider: RefObject<IdeaTimeRangeSlider>

    constructor(props: DefaultScheduleDayOfWeekScheduleProps) {
        super(props);
        this.timeRangeSlider = React.createRef()
        this.state = {
            schedule: (this.props.schedule && this.props.schedule.schedule_type) ? this.props.schedule : {
                schedule_type: 'NO_SCHEDULE'
            }
        }
    }

    componentDidUpdate(prevProps: DefaultScheduleDayOfWeekScheduleProps) {
        // Update state when props change (e.g., when modal opens with new data)
        if (prevProps.schedule !== this.props.schedule) {
            this.setState({
                schedule: (this.props.schedule && this.props.schedule.schedule_type) ? this.props.schedule : {
                    schedule_type: 'NO_SCHEDULE'
                }
            })
        }
    }

    getTimeRangeSlider(): IdeaTimeRangeSlider | null {
        if (this.state.schedule.schedule_type === 'CUSTOM_SCHEDULE') {
            return this.timeRangeSlider.current!
        }
        return null
    }

    getValue(): VirtualDesktopSchedule {
        if (this.state.schedule.schedule_type === 'CUSTOM_SCHEDULE') {
            return {
                schedule_type: "CUSTOM_SCHEDULE",
                start_up_time: this.getTimeRangeSlider()!.getStartTime(),
                shut_down_time: this.getTimeRangeSlider()!.getEndTime()
            }
        } else {
            return {
                schedule_type: this.state.schedule.schedule_type
            }
        }
    }

    render() {
        return (
            <div>
                <IdeaFormField
                    key={`${this.props.dayOfWeek}-${this.state.schedule.schedule_type}`}
                    module={"defaultScheduleDayOfWeek"}
                    param={{
                        name: 'schedule_type',
                        title: this.props.dayOfWeek,
                        param_type: 'select',
                        data_type: 'str',
                        default: this.state.schedule.schedule_type,
                        choices: [
                            {
                                title: 'Working Hours (' + (this.props.working_hours_start || '09:00') + ' - ' + (this.props.working_hours_end || '17:00') + ')',
                                value: 'WORKING_HOURS'
                            },
                            {
                                title: 'Stop On Idle',
                                value: 'STOP_ON_IDLE'
                            },
                            {
                                title: 'Start All Day',
                                value: 'START_ALL_DAY'
                            },
                            {
                                title: 'Custom Schedule',
                                value: 'CUSTOM_SCHEDULE'
                            },
                            {
                                title: 'No Schedule',
                                value: 'NO_SCHEDULE'
                            }
                        ]
                    }}
                    onStateChange={(event) => {
                        this.setState({
                            schedule: {
                                schedule_type: event.value
                            }
                        })
                    }}
                />
                {this.state.schedule.schedule_type === 'CUSTOM_SCHEDULE' &&
                    <IdeaTimeRangeSlider
                        key={`${this.props.dayOfWeek}-slider-${this.state.schedule.start_up_time}-${this.state.schedule.shut_down_time}`}
                        ref={this.timeRangeSlider}
                        startTime={this.state.schedule.start_up_time ? this.state.schedule.start_up_time : '09:00'}
                        endTime={this.state.schedule.shut_down_time ? this.state.schedule.shut_down_time : '17:00'}
                    />
                }
            </div>
        )
    }
}

interface DefaultScheduleModalProps {
    onScheduleChange: (defaultSchedule: VirtualDesktopWeekSchedule) => Promise<boolean>
}

interface DefaultScheduleModalState {
    visible: boolean
    defaultSchedule: VirtualDesktopWeekSchedule | null
    errorMessage: string | null
    saveLoading: boolean,
    currentTime: any
    working_hours_start: string
    working_hours_end: string
}

class DefaultScheduleModal extends Component<DefaultScheduleModalProps, DefaultScheduleModalState> {

    mondaySchedule: RefObject<DefaultScheduleDayOfWeekSchedule>
    tuesdaySchedule: RefObject<DefaultScheduleDayOfWeekSchedule>
    wednesdaySchedule: RefObject<DefaultScheduleDayOfWeekSchedule>
    thursdaySchedule: RefObject<DefaultScheduleDayOfWeekSchedule>
    fridaySchedule: RefObject<DefaultScheduleDayOfWeekSchedule>
    saturdaySchedule: RefObject<DefaultScheduleDayOfWeekSchedule>
    sundaySchedule: RefObject<DefaultScheduleDayOfWeekSchedule>

    clockInterval: any

    constructor(props: DefaultScheduleModalProps) {
        super(props);

        this.mondaySchedule = React.createRef()
        this.tuesdaySchedule = React.createRef()
        this.wednesdaySchedule = React.createRef()
        this.thursdaySchedule = React.createRef()
        this.fridaySchedule = React.createRef()
        this.saturdaySchedule = React.createRef()
        this.sundaySchedule = React.createRef()

        this.state = {
            visible: false,
            defaultSchedule: null,
            errorMessage: null,
            saveLoading: false,
            currentTime: null,
            working_hours_start: '09:00', // Default fallback
            working_hours_end: '17:00'    // Default fallback
        }
    }

    componentDidMount() {
        AppContext.get().getClusterSettingsService().getVirtualDesktopSettings().then(settings => {
            this.setState({
                working_hours_start: settings.dcv_session.working_hours.start_up_time || '09:00',
                working_hours_end: settings.dcv_session.working_hours.shut_down_time || '17:00'
            })
        }).catch(error => {
            console.error('Failed to load VDC settings:', error);
            // Set default working hours if loading fails
            this.setState({
                working_hours_start: '09:00',
                working_hours_end: '17:00'
            })
        })
        this.clockInterval = setInterval(() => {
            this.setState({
                currentTime: moment()
            })
        }, 1000)
    }

    componentWillUnmount() {
        clearInterval(this.clockInterval)
    }

    showDefaultSchedule(defaultSchedule: VirtualDesktopWeekSchedule) {
        this.setState({
            visible: true,
            defaultSchedule: defaultSchedule
        })
    }

    cancel() {
        this.setState({
            visible: false,
            defaultSchedule: null,
            errorMessage: null,
            saveLoading: false
        })
    }

    save() {
        if (this.state.defaultSchedule) {
            let weekSchedule: VirtualDesktopWeekSchedule = {
                monday: this.mondaySchedule.current!.getValue(),
                tuesday: this.tuesdaySchedule.current!.getValue(),
                wednesday: this.wednesdaySchedule.current!.getValue(),
                thursday: this.thursdaySchedule.current!.getValue(),
                friday: this.fridaySchedule.current!.getValue(),
                saturday: this.saturdaySchedule.current!.getValue(),
                sunday: this.sundaySchedule.current!.getValue()
            }
            this.setState({
                errorMessage: null,
                saveLoading: true
            }, () => {
                this.props.onScheduleChange(weekSchedule)
                    .then(status => {
                        if (status) {
                            this.cancel()
                        } else {
                            this.setState({
                                saveLoading: false
                            })
                        }
                    })
            })
        }
    }

    setErrorMessage(message: string) {
        this.setState({
            errorMessage: message
        })
    }

    render() {
        return (
            <Modal visible={this.state.visible}
                   size="medium"
                   onDismiss={() => {
                       this.cancel()
                   }}
                   header={
                       <Header variant="h3"
                       description={
                        <>
                            Setup default schedules for new virtual desktop sessions. These schedules will be applied to new sessions unless users customize their schedules.
                            The schedule operates at the cluster timezone setup by your cluster administrator.
                            <br /><br /><a href="https://docs.idea-hpc.com/modules/virtual-desktop-interfaces/user-documentation/virtual-desktop-scheduling"
                                target="_blank" rel="noopener noreferrer">See documentation for scheduling explanations</a>.
                        </>
                    }>Edit Default Schedules</Header>
                   }
                   footer={
                       <Box float="right">
                           <SpaceBetween size="xs" direction="horizontal">
                               <Button disabled={this.state.saveLoading}
                                       onClick={() => this.cancel()}>Cancel</Button>
                               <Button loading={this.state.saveLoading} variant="primary"
                                       onClick={() => this.save()}>Save</Button>
                           </SpaceBetween>
                       </Box>
                   }>

                <SpaceBetween size={"m"}>
                    <Alert>
                        <strong>Cluster Time: {this.state.currentTime && this.state.currentTime.tz(AppContext.get().getClusterSettingsService().getClusterTimeZone()).format('LLL')} ({AppContext.get().getClusterSettingsService().getClusterTimeZone()})</strong><br/>
                    </Alert>

                    <Form errorText={this.state.errorMessage}>
                        <ColumnLayout columns={1}>
                            <DefaultScheduleDayOfWeekSchedule
                                ref={this.mondaySchedule}
                                dayOfWeek="Monday"
                                schedule={this.state.defaultSchedule?.monday}
                                working_hours_start={this.state.working_hours_start}
                                working_hours_end={this.state.working_hours_end}
                            />
                            <DefaultScheduleDayOfWeekSchedule
                                ref={this.tuesdaySchedule}
                                dayOfWeek="Tuesday"
                                schedule={this.state.defaultSchedule?.tuesday}
                                working_hours_start={this.state.working_hours_start}
                                working_hours_end={this.state.working_hours_end}
                            />
                            <DefaultScheduleDayOfWeekSchedule
                                ref={this.wednesdaySchedule}
                                dayOfWeek="Wednesday"
                                schedule={this.state.defaultSchedule?.wednesday}
                                working_hours_start={this.state.working_hours_start}
                                working_hours_end={this.state.working_hours_end}
                            />
                            <DefaultScheduleDayOfWeekSchedule
                                ref={this.thursdaySchedule}
                                dayOfWeek="Thursday"
                                schedule={this.state.defaultSchedule?.thursday}
                                working_hours_start={this.state.working_hours_start}
                                working_hours_end={this.state.working_hours_end}
                            />
                            <DefaultScheduleDayOfWeekSchedule
                                ref={this.fridaySchedule}
                                dayOfWeek="Friday"
                                schedule={this.state.defaultSchedule?.friday}
                                working_hours_start={this.state.working_hours_start}
                                working_hours_end={this.state.working_hours_end}
                            />
                            <DefaultScheduleDayOfWeekSchedule
                                ref={this.saturdaySchedule}
                                dayOfWeek="Saturday"
                                schedule={this.state.defaultSchedule?.saturday}
                                working_hours_start={this.state.working_hours_start}
                                working_hours_end={this.state.working_hours_end}
                            />
                            <DefaultScheduleDayOfWeekSchedule
                                ref={this.sundaySchedule}
                                dayOfWeek="Sunday"
                                schedule={this.state.defaultSchedule?.sunday}
                                working_hours_start={this.state.working_hours_start}
                                working_hours_end={this.state.working_hours_end}
                            />
                        </ColumnLayout>
                    </Form>
                </SpaceBetween>
            </Modal>)
    }
}

export default DefaultScheduleModal
