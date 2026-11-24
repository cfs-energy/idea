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

import React, {Component, RefObject} from "react";

import {AUTH_PARAM_USERNAME, AUTH_PARAM_PASSWORD, IdeaAuthProps, IdeaAuthState} from "./auth-interfaces";
import {Alert, Box, Button, ColumnLayout, SpaceBetween, StatusIndicator} from "@cloudscape-design/components";
import AuthLayout from "./auth-layout";
import IdeaAuthContext from "./auth-context";
import {AppContext} from "../../common";
import {AUTH_LOGIN_CHALLENGE, AUTH_PASSWORD_RESET_REQUIRED, NETWORK_ERROR, REQUEST_TIMEOUT, SERVER_ERROR} from "../../common/error-codes";
import {takeSessionEnded} from "../../common/session-return";
import IdeaForm from "../../components/form";
import Utils from "../../common/utils";
import {withRouter} from "../../navigation/navigation-utils";

class IdeaAuthLogin extends Component<IdeaAuthProps, IdeaAuthState> {
    static contextType = IdeaAuthContext
    form: RefObject<IdeaForm | null>
    sessionEnded = takeSessionEnded()

    constructor(props: IdeaAuthProps) {
        super(props);
        this.form = React.createRef()
        this.state = {
            loading: false,
            layoutLoading: false
        }
    }

    getForm(): IdeaForm {
        return this.form.current!
    }

    onSubmit = () => {
        this.setState({
            loading: true
        }, () => {
            if (this.getForm().validate()) {
                this.getForm().clearError()
                const values = this.getForm().getValues()
                // the route guard moves on once the session starts, back to where the person was
                AppContext.get().auth().login(values.username, values.password).catch(error => {
                    if (error.errorCode === AUTH_LOGIN_CHALLENGE) {
                        this.props.navigate('/auth/challenge')
                    } else if (error.errorCode === AUTH_PASSWORD_RESET_REQUIRED) {
                        this.props.navigate('/auth/confirm-forgot-password')
                    } else {
                        if(error.errorCode === NETWORK_ERROR || error.errorCode === REQUEST_TIMEOUT || error.errorCode === SERVER_ERROR) {
                            // says nothing about the password, so it stays in the form
                            this.getForm().setError(error.errorCode, error.message)
                        } else {
                            this.getForm().setError(error.errorCode, 'The username or password is not correct.')
                            this.getForm().setParamValue('password', '')
                        }
                    }
                }).finally(() => {
                    this.setState({
                        loading: false
                    })
                })
            } else {
                this.setState({
                    loading: false
                })
            }
        })
    }

    render() {
        const getSubtitle = () => {
            return AppContext.get().getSubtitle()
        }
        const hasSubtitle = () => {
            return Utils.isNotEmpty(getSubtitle())
        }

        const isSsoEnabled = Utils.isSsoEnabled();

        return <AuthLayout
            content={
                <ColumnLayout columns={1} className="auth-content">
                    <h3 className="title">{AppContext.get().getTitle()}</h3>
                    {hasSubtitle() && <p className="subtitle">{getSubtitle()}</p>}
                    {this.sessionEnded && <Alert type="info">Your session ended. Sign in to continue.</Alert>}
                    {isSsoEnabled && AppContext.get().auth().ssoDidNotComplete() && <Alert type="error">Sign-in with your company account did not complete. Try again. If it keeps failing, contact your administrator.</Alert>}
                    {!isSsoEnabled && ([
                        <IdeaForm key="login-form" name="login-form"
                                  ref={this.form}
                                  modalSize={"max"}
                                  showHeader={false}
                                  showActions={false}
                                  onSubmit={(_) => { this.onSubmit(); }}
                                  stretch={true}
                                  params={[AUTH_PARAM_USERNAME, AUTH_PARAM_PASSWORD]}
                        />,
                        <SpaceBetween key="actions" size={"xs"} direction={"vertical"} className="actions">
                            {!this.state.loading && (
                                <div>
                                    <Button variant="primary"
                                            onClick={() => { this.onSubmit(); }}
                                    >Sign In</Button>
                                    <Button variant="link" onClick={() => {
                                        this.props.navigate('/auth/forgot-password')
                                    }}
                                    >Forgot Password?</Button>
                                </div>
                            )}
                            {this.state.loading && <Box textAlign={"center"}>
                                <StatusIndicator type="loading"/>
                            </Box>}
                        </SpaceBetween>
                    ])}
                {isSsoEnabled && (

                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
                    <Button variant="primary" onClick={() => { AppContext.get().auth().initiateSso(); }}>
                        Login with SSO
                    </Button>
                    </div>

                    )}
                </ColumnLayout>
            }
        />
    }
}

export default withRouter(IdeaAuthLogin)
