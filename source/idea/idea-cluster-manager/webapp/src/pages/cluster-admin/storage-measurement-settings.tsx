import React from 'react';
import {Alert} from '@cloudscape-design/components';
import CatalogSettingsSection from './catalog-settings-section';

export default function StorageMeasurementSettings(props: React.ComponentProps<typeof CatalogSettingsSection>) {
    const storage = props.values['shared-storage'] ?? {};
    const attachments = Object.keys(storage).filter(name => storage[name]?.provider === 'fsx_netapp_ontap');
    const provider = props.values.metrics?.provider;
    const supported = provider === 'dogstatsd' || provider === 'cloudwatch';
    const incomplete = attachments.some(name => !storage[name].fsx_netapp_ontap?.metrics?.username || !storage[name].fsx_netapp_ontap?.metrics?.password_secret_arn);
    const settings = props.settings.filter(setting => setting.module !== 'shared-storage' || attachments.includes(setting.path.split('.')[0])).map(setting => ({...setting,
        advanced: setting.module === 'cluster-manager' && setting.path !== 'metrics.storage.enabled',
    }));
    return <CatalogSettingsSection {...props} settings={settings} editDisabled={props.editDisabled || !supported || !attachments.length}>
        {!attachments.length && <Alert type="info">No ONTAP attachments configured. EFS and Lustre do not use these measurement credentials.</Alert>}
        {!supported && <Alert type="info">Storage measurement requires the dogstatsd or cloudwatch metrics provider. The current provider does not support measurement.</Alert>}
        {(!props.values['cluster-manager']?.metrics?.storage?.enabled || incomplete) && <Alert type="info" header="Set up ONTAP measurement">
            Grant a read-only SVM REST user access to quota reports and volumes. Configure user quotas and network access to SVM HTTPS. Store its password in Secrets Manager with the cluster and module tags granting cluster-manager access. Enter the username and password secret ARN for each attachment, save, then restart. Keep TLS verification configured for the SVM certificate.
        </Alert>}
    </CatalogSettingsSection>;
}
