import React from 'react';
import {toast} from 'react-toastify';

/** Pages load fresh on a reload, so an open tab only needs telling. */
export function notifyPortalUpdate() {
    toast.info(<span>A new version of the portal is available. <button type="button" onClick={() => window.location.reload()}>Reload</button></span>, {
        autoClose: false,
        toastId: 'portal-update'
    });
}
