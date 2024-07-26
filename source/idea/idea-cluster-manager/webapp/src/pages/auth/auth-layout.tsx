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
import {AppLayout, Grid} from "@cloudscape-design/components";
import {AppContext} from "../../common";
import './auth.scss'
import Utils from "../../common/utils";
import AppLogger from "../../common/app-logger";

export interface AuthLayoutProps {
    loading?: boolean
    content: JSX.Element
}

export interface AuthLayoutState {
    ready: boolean
}

class AuthLayout extends Component<AuthLayoutProps, AuthLayoutState> {

    private logger: AppLogger

    constructor(props: AuthLayoutProps) {
        super(props);
        this.state = {
            ready: false
        }
        this.logger = new AppLogger({
            name: 'auth-layout.ts'
        })
        this.logger.debug('AuthLayout component constructed with initial state: ', this.state);
    }

    componentDidMount() {
        this.logger.debug('AuthLayout component mounted. Checking login status.');
        AppContext.get().auth().isLoggedIn().then((status) => {
            this.logger.debug(`Login status received: ${status}`);
            // show loading animation spinner until the initial sso redirect is not complete
            if (!status && Utils.isSsoEnabled() && typeof window.idea.app.sso_auth_status === 'undefined') {
                this.logger.debug('SSO is enabled and auth status is undefined. Waiting for SSO redirect completion.');
                return;
            }
            this.logger.debug('Setting state to ready.');
            this.setState({
                ready: true
            }, () => {
                this.logger.debug('State set to ready. Hiding loading animation.');
                Utils.hideLoadingAnimation();
            })
        }).catch(error => {
            this.logger.error('Error while checking login status: ', error);
        });
    }

    isLoading(): boolean {
        if (this.props.loading == null) {
            this.logger.debug('Loading prop is null. Returning false.');
            return false;
        }
        this.logger.debug(`Loading prop is: ${this.props.loading}`);
        return this.props.loading;
    }

    render() {
        this.logger.debug(`Rendering AuthLayout component. Ready state: ${this.state.ready}`);
        return <AppLayout
            navigationHide={true}
            toolsHide={true}
            content={
                this.state.ready && <main className="soca-app-content auth">
                    <Grid gridDefinition={[
                        { colspan: { xxs: 12, s: 6, l: 4 }, offset: { xxs: 0, s: 3, l: 4 } },
                    ]}>
                        <div className="auth-content-wrapper">
                            { !this.isLoading() && this.props.content }
                            <p className="copyright">{ AppContext.get().getCopyRightText() }</p>
                        </div>
                    </Grid>
                </main>
            }
        />
    }
}

export default AuthLayout;

