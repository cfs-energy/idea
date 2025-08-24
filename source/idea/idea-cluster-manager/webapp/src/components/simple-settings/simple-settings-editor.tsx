import React, {Component, RefObject} from "react";
import {Modal, Box, Button, SpaceBetween} from "@cloudscape-design/components";
import IdeaForm from "../form";
import {SocaUserInputParamMetadata} from "../../client/data-model";

export interface SimpleSettingsEditorProps {
    visible: boolean;
    onDismiss: () => void;
    title: string;
    settingConfig: SocaUserInputParamMetadata;           // IDEA form parameter config
    currentValue: any;                                   // Current setting value
    onSave: (newValue: any) => Promise<boolean>;         // Save handler
}

export interface SimpleSettingsEditorState {
    saving: boolean;
}

class SimpleSettingsEditor extends Component<SimpleSettingsEditorProps, SimpleSettingsEditorState> {

    form: RefObject<IdeaForm>;

    constructor(props: SimpleSettingsEditorProps) {
        super(props);
        this.form = React.createRef();
        this.state = {
            saving: false
        };
    }

    // Remove the problematic componentDidUpdate that was resetting the form
    // and causing it to lose the current value when the modal opens

    handleSave = async () => {
        if (!this.form.current?.validate()) {
            return;
        }

        this.setState({ saving: true });
        try {
            const formValues = this.form.current?.getValues();
            const settingName = this.props.settingConfig.name;

            if (!settingName) {
                console.error('Setting name is undefined');
                return;
            }

            const newValue = formValues?.[settingName];

            const success = await this.props.onSave(newValue);
            if (success) {
                this.props.onDismiss();
            }
        } finally {
            this.setState({ saving: false });
        }
    };

    render() {
        const { visible, onDismiss, title, settingConfig, currentValue } = this.props;
        const { saving } = this.state;

        // Create form values object with the current value to ensure proper initialization
        const formValues = {
            [settingConfig.name!]: currentValue
        };

        return (
            <Modal
                onDismiss={onDismiss}
                visible={visible}
                header={title}
                footer={
                    <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                            <Button variant="link" onClick={onDismiss} disabled={saving}>
                                Cancel
                            </Button>
                            <Button variant="primary" onClick={this.handleSave} loading={saving}>
                                Save
                            </Button>
                        </SpaceBetween>
                    </Box>
                }
            >
                <IdeaForm
                    ref={this.form}
                    name="settings-editor"
                    showHeader={false}
                    showActions={false}
                    values={formValues}
                    params={[{
                        ...settingConfig,
                        default: currentValue
                    }]}
                />
            </Modal>
        );
    }
}

export default SimpleSettingsEditor;
