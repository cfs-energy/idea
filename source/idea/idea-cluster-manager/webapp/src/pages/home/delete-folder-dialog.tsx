import React, {useRef, useState} from 'react';
import {Alert, Box, Button, CopyToClipboard, FormField, Input, Modal, SpaceBetween} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {storageAge, storageSize, useStorageUsage} from './storage-usage';

/** The command to do it by hand, safe to paste whatever the folder is called. */
export function removeCommand(path: string): string {
    return `rm -r '${path.replace(/'/g, `'\\''`)}'`;
}

function UseTerminal(props: {path: string, reason: string, type: 'info' | 'warning' | 'error'}) {
    return <Alert type={props.type}>
        <SpaceBetween size="xs">
            <Box>{props.reason} To delete it, run this in a terminal:</Box>
            <CopyToClipboard variant="inline" textToCopy={removeCommand(props.path)} copyButtonAriaLabel="Copy command" copySuccessText="Copied" copyErrorText="Copy failed"/>
        </SpaceBetween>
    </Alert>;
}

function isInside(path: string, home: string): boolean {
    return path.startsWith(`${home.replace(/\/+$/, '')}/`);
}

function CannotDeleteHere(props: {path: string, onClose: () => void}) {
    return <Modal visible header="Delete folder" onDismiss={props.onClose}
        footer={<Box float="right"><Button onClick={props.onClose}>Close</Button></Box>}>
        <UseTerminal type="info" path={props.path} reason="Folders outside your home can't be deleted here."/>
    </Modal>;
}

function ConfirmDelete(props: {path: string, name: string, onClose: () => void, onDeleted: () => void}) {
    const {usage, error, retry} = useStorageUsage(props.path);
    const [confirmation, setConfirmation] = useState('');
    const [deleting, setDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    const [timedOut, setTimedOut] = useState(false);
    const submitted = useRef(false);
    const folder = usage?.folder;
    const canDelete = usage?.state === 'ready' && folder?.identity && !folder.partial && confirmation === props.name && !deleting;
    const remove = async () => {
        if (!canDelete || submitted.current) return;
        submitted.current = true;
        setDeleting(true);
        setDeleteError(null);
        try {
            await AppContext.get().client().fileBrowser().deleteFolder({path: props.path, identity: folder.identity!});
            props.onDeleted();
        } catch (e: any) {
            setTimedOut(e?.errorCode === 'FILE_OPERATION_TIMED_OUT');
            setDeleteError(e?.errorCode === 'DELETE_FAILED'
                ? "Some of this folder could not be deleted. What's left is still there."
                : (e?.message || 'The folder could not be deleted.').replace(/^Invalid params: /, ''));
            submitted.current = false;
            setDeleting(false);
        }
    };
    // the server cannot measure this folder: nothing here could ever enable the button
    if (usage?.state === 'unavailable' || (usage?.home && !isInside(props.path, usage.home))) {
        return <CannotDeleteHere path={props.path} onClose={props.onClose}/>;
    }
    return <Modal visible header="Delete folder" onDismiss={() => { if (!deleting) props.onClose(); }}
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
            <Button disabled={deleting} onClick={props.onClose}>Cancel</Button>
            <Button variant="primary" disabled={!canDelete} loading={deleting} onClick={remove}>Delete folder</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
            <Box>Permanently delete {props.path} and everything inside it? This cannot be undone.</Box>
            {error ? <Alert type="error" action={<Button onClick={retry}>Retry storage usage</Button>}>{error}</Alert> :
                (!usage || usage.state === 'computing') ? <Box>Computing storage usage...</Box> : null}
            {usage?.state === 'ready' && (!folder?.identity || folder.partial) &&
                <UseTerminal type="warning" path={props.path} reason={folder?.measured === false ? "This folder has not been measured yet, so it can't be deleted here. Try again later." : "This folder is too large to measure, so it can't be deleted here."}/>}
            {folder && folder.measured !== false && <SpaceBetween size="xs">
                <Box>Size: {folder.partial ? 'At least ' : ''}{storageSize(folder.bytes)} in {folder.files.toLocaleString()} files</Box>
                <Box>Last changed: {storageAge(folder.newest_mtime)}</Box>
                <Box>Measured {new Date(usage!.measured_at! * 1000).toLocaleString()}.</Box>
            </SpaceBetween>}
            <FormField label={`Type ${props.name} to confirm deletion`}>
                <Input value={confirmation} disabled={deleting} onChange={({detail}) => setConfirmation(detail.value)} autoComplete={false}/>
            </FormField>
            {timedOut
                ? <UseTerminal type="error" path={props.path} reason="This took too long to delete here and was stopped. Some of the folder is already gone."/>
                : deleteError && <Alert type="error">{deleteError}</Alert>}
        </SpaceBetween>
    </Modal>;
}

export default function DeleteFolderDialog(props: {path: string, name: string, home?: string, onClose: () => void, onDeleted: () => void}) {
    if (props.home && !isInside(props.path, props.home)) {
        return <CannotDeleteHere path={props.path} onClose={props.onClose}/>;
    }
    return <ConfirmDelete path={props.path} name={props.name} onClose={props.onClose} onDeleted={props.onDeleted}/>;
}
