// The deployed Lambda functions whose handler is now the TypeScript port under `src/lambda`.
//
// One table, two readers: `test/handler-inplace` proves each deployed function keeps its logical
// id and its replacement properties, and `intended-drift.ts` turns the same rows into the itemised
// runtime, handler and suppression differences the parity gate reports. A row added in one place
// and forgotten in the other is the failure this file exists to prevent.
//
// The logical id is the CDK id, which is derived from the construct path inside the stack and is
// therefore the same in every cluster.

export interface NodeHandler {
  /** The package directory under `src/lambda`, and the Python package it replaced. */
  packageName: string;
  logicalId: string;
  moduleId: string;
  moduleName: string;
}

/** The deployed functions whose handler is the port, as the dev27 capture carries them. */
export const DEPLOYED_HANDLERS: readonly NodeHandler[] = [
  {
    packageName: 'idea_analytics_sink',
    logicalId: 'analyticssinklambdaADB37882',
    moduleId: 'analytics',
    moduleName: 'analytics',
  },
  {
    packageName: 'idea_custom_resource_opensearch_private_ips',
    logicalId: 'opensearchprivateipslambda3D078D54',
    moduleId: 'analytics',
    moduleName: 'analytics',
  },
  {
    packageName: 'idea_custom_resource_detach_project_boundaries',
    logicalId: 'detachprojectboundarieslambdaBF006DAF',
    moduleId: 'cluster-manager',
    moduleName: 'cluster-manager',
  },
  {
    packageName: 'idea_custom_resource_ensure_log_group',
    logicalId: 'ensurebedrockloggrouplambda051BC7B4',
    moduleId: 'cluster-manager',
    moduleName: 'cluster-manager',
  },
  {
    packageName: 'idea_custom_resource_self_signed_certificate',
    logicalId: 'selfsignedcertificate1A65086D',
    moduleId: 'cluster',
    moduleName: 'cluster',
  },
  {
    packageName: 'idea_custom_resource_update_cluster_settings',
    logicalId: 'clustersettingsBECB5478',
    moduleId: 'cluster',
    moduleName: 'cluster',
  },
  {
    packageName: 'idea_ec2_state_event_transformation_lambda',
    logicalId: 'clusterec2stateeventtransformer918D245B',
    moduleId: 'cluster',
    moduleName: 'cluster',
  },
  {
    packageName: 'idea_custom_resource_cluster_endpoints',
    logicalId: 'clusterendpoints75A9A687',
    moduleId: 'cluster',
    moduleName: 'cluster',
  },
  {
    packageName: 'idea_solution_metrics',
    logicalId: 'solutionmetricsAE489078',
    moduleId: 'cluster',
    moduleName: 'cluster',
  },
  {
    packageName: 'idea_custom_resource_get_ad_security_group',
    logicalId: 'getadsecuritygroupidlambdaA343A275',
    moduleId: 'directoryservice',
    moduleName: 'directoryservice',
  },
  {
    packageName: 'idea_custom_resource_sso_claim_modifier',
    logicalId: 'idtokenclaim18B64AB5',
    moduleId: 'identity-provider',
    moduleName: 'identity-provider',
  },
  {
    packageName: 'idea_controller_scheduled_event_transformer',
    logicalId: 'vdcscheduledeventtransformer32EA7695',
    moduleId: 'vdc',
    moduleName: 'virtual-desktop-controller',
  },
];

/** Every ported handler, for a reader that works from the deployed template rather than a stack. */
export const NODE_HANDLERS: readonly NodeHandler[] = DEPLOYED_HANDLERS;
