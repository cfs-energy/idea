import React, {useRef, useState} from 'react';
import {Alert, Box, Button, FormField, Input, Modal, SpaceBetween} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {storageAge, storageSize, useStorageUsage} from './storage-usage';

export default function DeleteFolderDialog(props: {path: string, name: string, onClose: () => void, onDeleted: () => void}) {
    const {usage, error, retry} = useStorageUsage(props.path);
    const [confirmation, setConfirmation] = useState('');
    const [deleting, setDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
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
            setDeleteError(e?.message || 'The folder could not be deleted.');
            submitted.current = false;
            setDeleting(false);
        }
    };
    return <Modal visible header="Delete folder" onDismiss={() => { if (!deleting) props.onClose(); }}
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
            <Button disabled={deleting} onClick={props.onClose}>Cancel</Button>
            <Button variant="primary" disabled={!canDelete} loading={deleting} onClick={remove}>Delete folder</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
            <Box>Permanently delete {props.path} and everything inside it? This cannot be undone.</Box>
            {error ? <Alert type="error" action={<Button onClick={retry}>Retry storage usage</Button>}>{error}</Alert> :
                (!usage || usage.state === 'computing') ? <Box>Computing storage usage...</Box> : null}
            {usage?.state === 'ready' && (!folder?.identity || folder.partial) && <Alert type="warning">Complete usage is unavailable for this folder. Deletion is disabled.</Alert>}
            {folder && <SpaceBetween size="xs">
                <Box>Size: {folder.partial ? 'At least ' : ''}{storageSize(folder.bytes)} in {folder.files.toLocaleString()} files</Box>
                <Box>Last changed: {storageAge(folder.newest_mtime)}</Box>
                <Box>Measured {new Date(usage!.measured_at! * 1000).toLocaleString()}.</Box>
            </SpaceBetween>}
            <FormField label={`Type ${props.name} to confirm deletion`}>
                <Input value={confirmation} disabled={deleting} onChange={({detail}) => setConfirmation(detail.value)} autoComplete={false}/>
            </FormField>
            {deleteError && <Alert type="error">{deleteError}</Alert>}
        </SpaceBetween>
    </Modal>;
}
