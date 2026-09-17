/**
 * Provides the five AWS reads that CDK synthesis needs behind one interface, so
 * synthesis can run without credentials.
 *
 * Replay is keyed `service:action:JSON(params)`, where params are the API parameters the
 * callers pass. A missing key throws rather than falling back to a default.
 */

import { readFileSync } from 'node:fs';
import type { Listener } from '@aws-sdk/client-elastic-load-balancing-v2';
import type { UserPoolType } from '@aws-sdk/client-cognito-identity-provider';
import type { Role } from '@aws-sdk/client-iam';
import type { DomainStatus } from '@aws-sdk/client-opensearch';
import { awsClientOptions } from "../cli/aws-client-options.ts";

export type UserPool = UserPoolType;

export interface SynthReads {
  callerIdentity(): Promise<{ account: string; arn: string }>;
  describeListener(listenerArn: string): Promise<Listener>;
  describeUserPool(userPoolId: string): Promise<UserPool>;
  listServiceLinkedRoles(pathPrefix: string): Promise<Role[]>;
  describeDomain(domainName: string): Promise<DomainStatus>;
}

export class SynthReadMiss extends Error {
  readonly key: string;
  constructor(key: string) {
    super(`SynthReadMiss: ${key}`);
    this.name = 'SynthReadMiss';
    this.key = key;
  }
}

/** The replay key for one read. Used by both the replay reader and `capture.ts`. */
export function synthReadKey(service: string, action: string, params: Record<string, unknown>): string {
  return `${service}:${action}:${JSON.stringify(params)}`;
}

export const CALLER_IDENTITY_KEY = synthReadKey('sts', 'GetCallerIdentity', {});
export const listenerKey = (listenerArn: string) => synthReadKey('elbv2', 'DescribeListeners', { ListenerArns: [listenerArn] });
export const userPoolKey = (userPoolId: string) => synthReadKey('cognito-idp', 'DescribeUserPool', { UserPoolId: userPoolId });
export const listRolesKey = (pathPrefix: string) => synthReadKey('iam', 'ListRoles', { PathPrefix: pathPrefix });
export const describeDomainKey = (domainName: string) => synthReadKey('opensearch', 'DescribeDomain', { DomainName: domainName });

/**
 * Replays a `synth-reads.json` fixture. Values are unwrapped interface results:
 * one `Listener`, one `UserPool`, a `Role[]`, or one `DomainStatus`.
 */
export function replaySynthReads(file: string): SynthReads {
  const reads = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>;
  const get = <T>(key: string): T => {
    if (!(key in reads)) throw new SynthReadMiss(key);
    return reads[key] as T;
  };
  return {
    async callerIdentity() {
      return get<{ account: string; arn: string }>(CALLER_IDENTITY_KEY);
    },
    async describeListener(listenerArn) {
      return get<Listener>(listenerKey(listenerArn));
    },
    async describeUserPool(userPoolId) {
      return get<UserPool>(userPoolKey(userPoolId));
    },
    async listServiceLinkedRoles(pathPrefix) {
      return get<Role[]>(listRolesKey(pathPrefix));
    },
    async describeDomain(domainName) {
      return get<DomainStatus>(describeDomainKey(domainName));
    },
  };
}

/** Reads live AWS data. Clients load lazily so replay tests do not load the SDK. */
export function liveSynthReads(region: string, profile?: string): SynthReads {
  // The CDK app loads cluster configuration after this call through a client it does not own.
  if (profile !== undefined && profile.trim() !== "") process.env.AWS_PROFILE = profile;

  const once = <T>(make: () => Promise<T>): (() => Promise<T>) => {
    let p: Promise<T> | undefined;
    return () => (p ??= make());
  };
  const config = once(() => awsClientOptions(region, profile));
  const sts = once(async () => new (await import('@aws-sdk/client-sts')).STSClient(await config()));
  const elbv2 = once(async () => new (await import('@aws-sdk/client-elastic-load-balancing-v2')).ElasticLoadBalancingV2Client(await config()));
  const cognito = once(async () => new (await import('@aws-sdk/client-cognito-identity-provider')).CognitoIdentityProviderClient(await config()));
  const iam = once(async () => new (await import('@aws-sdk/client-iam')).IAMClient(await config()));
  const opensearch = once(async () => new (await import('@aws-sdk/client-opensearch')).OpenSearchClient(await config()));

  return {
    async callerIdentity() {
      const { GetCallerIdentityCommand } = await import('@aws-sdk/client-sts');
      const result = await (await sts()).send(new GetCallerIdentityCommand({}));
      return { account: result.Account ?? '', arn: result.Arn ?? '' };
    },
    async describeListener(listenerArn) {
      const { DescribeListenersCommand } = await import('@aws-sdk/client-elastic-load-balancing-v2');
      const result = await (await elbv2()).send(new DescribeListenersCommand({ ListenerArns: [listenerArn] }));
      const listener = result.Listeners?.[0];
      if (!listener) throw new Error(`no such listener: ${listenerArn}`);
      return listener;
    },
    async describeUserPool(userPoolId) {
      const { DescribeUserPoolCommand } = await import('@aws-sdk/client-cognito-identity-provider');
      const result = await (await cognito()).send(new DescribeUserPoolCommand({ UserPoolId: userPoolId }));
      if (!result.UserPool) throw new Error(`no such user pool: ${userPoolId}`);
      return result.UserPool;
    },
    async listServiceLinkedRoles(pathPrefix) {
      const { ListRolesCommand } = await import('@aws-sdk/client-iam');
      const result = await (await iam()).send(new ListRolesCommand({ PathPrefix: pathPrefix }));
      return result.Roles ?? [];
    },
    async describeDomain(domainName) {
      const { DescribeDomainCommand } = await import('@aws-sdk/client-opensearch');
      const result = await (await opensearch()).send(new DescribeDomainCommand({ DomainName: domainName }));
      if (!result.DomainStatus) throw new Error(`no such opensearch domain: ${domainName}`);
      return result.DomainStatus;
    },
  };
}
