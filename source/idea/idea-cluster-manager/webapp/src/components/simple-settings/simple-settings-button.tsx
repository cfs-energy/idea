import React, {Component} from "react";
import {Button} from "@cloudscape-design/components";
import SimpleSettingsEditor from "./simple-settings-editor";
import {SocaUserInputParamMetadata} from "../../client/data-model";

export interface SimpleSettingsButtonProps {
    title: string;
    settingConfig: SocaUserInputParamMetadata;
    currentValue: any;
    onSave: (newValue: any) => Promise<boolean>;
    disabled?: boolean;
    checkPermissions?: () => boolean;
}

export interface SimpleSettingsButtonState {
    showModal: boolean;
    canEdit: boolean;
}

class SimpleSettingsButton extends Component<SimpleSettingsButtonProps, SimpleSettingsButtonState> {

    constructor(props: SimpleSettingsButtonProps) {
        super(props);
        this.state = {
            showModal: false,
            canEdit: true
        };
    }

    componentDidMount() {
        if (this.props.checkPermissions) {
            this.setState({
                canEdit: this.props.checkPermissions()
            });
        }
    }

    componentDidUpdate(prevProps: SimpleSettingsButtonProps) {
        if (prevProps.checkPermissions !== this.props.checkPermissions && this.props.checkPermissions) {
            this.setState({
                canEdit: this.props.checkPermissions()
            });
        }
    }

    handleOpenModal = () => {
        this.setState({ showModal: true });
    };

    handleCloseModal = () => {
        this.setState({ showModal: false });
    };

    render() {
        const { title, settingConfig, currentValue, onSave, disabled } = this.props;
        const { showModal, canEdit } = this.state;

        // Don't render button if user doesn't have permission
        if (!canEdit) {
            return null;
        }

        // Don't allow editing if current value is undefined (settings not loaded yet)
        const isSettingsLoaded = currentValue !== undefined;

        return (
            <>
                <Button
                    variant="icon"
                    iconName="edit"
                    disabled={disabled || !isSettingsLoaded}
                    onClick={this.handleOpenModal}
                    ariaLabel={`Edit ${title.toLowerCase()}`}
                />

                {isSettingsLoaded && (
                    <SimpleSettingsEditor
                        visible={showModal}
                        onDismiss={this.handleCloseModal}
                        title={title}
                        settingConfig={settingConfig}
                        currentValue={currentValue}
                        onSave={onSave}
                    />
                )}
            </>
        );
    }
}

export default SimpleSettingsButton;
