/**
 * The description of the cluster stack's VPC-wide rule on the broker client listener. The stack
 * writes it, and the upgrade reads it back from the deployed template; one constant keeps the two
 * from drifting. No CDK import, so the command line can load it cheaply.
 */
export const WIDE_BROKER_CLIENT_RULE = 'Allow HTTPS traffic from DCV Clients to DCV Broker';
