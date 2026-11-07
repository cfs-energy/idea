import React, {Component, ReactNode} from 'react';
import {Alert, Box, Button, SpaceBetween} from '@cloudscape-design/components';
import {reportClientError} from '../../common/client-error-reporter';
import Utils from '../../common/utils';

interface IdeaErrorBoundaryProps {
    children: ReactNode
    // a new value clears the error, so moving to another page leaves the crash behind
    resetKey?: string
}

interface IdeaErrorBoundaryState {
    crashed: boolean
    resetKey?: string
}

/** A page that throws while rendering gets this screen instead of a blank window. */
class IdeaErrorBoundary extends Component<IdeaErrorBoundaryProps, IdeaErrorBoundaryState> {
    state: IdeaErrorBoundaryState = {crashed: false, resetKey: this.props.resetKey};

    static getDerivedStateFromError() {
        return {crashed: true};
    }

    static getDerivedStateFromProps(props: IdeaErrorBoundaryProps, state: IdeaErrorBoundaryState) {
        return props.resetKey === state.resetKey ? null : {crashed: false, resetKey: props.resetKey};
    }

    componentDidCatch(error: Error) {
        console.error(error);
        Utils.hideLoadingAnimation();
        reportClientError('render_crash', {error});
    }

    render() {
        if (!this.state.crashed) {
            return this.props.children;
        }
        return <Box padding="xl">
            <Alert type="error" header="This page hit a problem">
                <SpaceBetween size="s">
                    <Box>Nothing you saved is affected. Reload the page to try again. If it keeps happening, tell your administrator which page it was.</Box>
                    <SpaceBetween direction="horizontal" size="xs">
                        <Button variant="primary" onClick={() => window.location.reload()}>Reload page</Button>
                        <Button href="#/">Go to home</Button>
                    </SpaceBetween>
                </SpaceBetween>
            </Alert>
        </Box>;
    }
}

export default IdeaErrorBoundary;
