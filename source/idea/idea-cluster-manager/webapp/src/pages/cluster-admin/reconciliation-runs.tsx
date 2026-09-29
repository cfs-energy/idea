import IdeaAppLayout from '../../components/app-layout';
import AccountReconcileSettings from './account-reconcile-settings';

export default function ReconciliationRuns(props: any) {
    return <IdeaAppLayout
        {...props}
        content={<AccountReconcileSettings active={true} identityProvider={null} mode="runs"/>}/>;
}
