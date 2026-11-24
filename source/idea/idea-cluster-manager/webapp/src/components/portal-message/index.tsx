import React, {useEffect} from 'react';
import {Alert, AlertProps, Box} from '@cloudscape-design/components';
import Utils from '../../common/utils';

/** A message shown in place of a page: outside the page layout, so it clears the startup spinner itself. */
export default function PortalMessage({type, header, action, children}: {type: AlertProps.Type; header: string; action?: React.ReactNode; children: React.ReactNode}) {
    useEffect(() => Utils.hideLoadingAnimation(), []);
    return <Box padding="xl"><Alert type={type} header={header} action={action}>{children}</Alert></Box>;
}
