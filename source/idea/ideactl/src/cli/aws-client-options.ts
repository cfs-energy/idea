/**
 * Builds AWS client options that bind a requested shared configuration profile
 * to the client credential provider.
 */

/** The SDK's retry strategy contract (RetryStrategyV2), narrowed to what this file uses. */
interface RetryToken { getRetryCount(): number; getRetryDelay(): number }
interface RetryErrorInfo { errorType: "TRANSIENT" | "THROTTLING" | "SERVER_ERROR" | "CLIENT_ERROR"; error?: { name?: string } }
export interface RetryStrategyV2 {
  acquireInitialRetryToken(scope: string): Promise<RetryToken>;
  refreshRetryTokenForRetry(token: RetryToken, errorInfo: RetryErrorInfo): Promise<RetryToken>;
  recordSuccess(token: RetryToken): void;
}

type ProfileCredentialsProvider = ReturnType<
  typeof import("@aws-sdk/credential-provider-ini")["fromIni"]
>;

export interface AwsClientOptions {
  region: string;
  credentials?: ProfileCredentialsProvider;
  retryStrategy: RetryStrategyV2;
}

/** Attempts per AWS call, first try included. The backoff below spans about two and a half minutes. */
export const AWS_CALL_ATTEMPTS = 10;

/** Wait before retry number `retry` (1-based): 1, 2, 4, 8, 16 s, then 20 s each. */
export function retryDelayMs(retry: number): number {
  return Math.min(1000 * 2 ** (retry - 1), 20_000);
}

/**
 * A client network drops a few seconds of new connections now and then. The SDK already classes
 * unreachable networks, failed lookups, resets and throttling as retryable, but its default gives
 * up after three attempts within about a second, so one blip aborted a whole upgrade. This keeps
 * the SDK's classification and only stretches the budget: client errors still fail at once.
 */
export const networkTolerantRetryStrategy: RetryStrategyV2 = {
  async acquireInitialRetryToken(): Promise<RetryToken> {
    return { getRetryCount: () => 0, getRetryDelay: () => 0 };
  },
  async refreshRetryTokenForRetry(token: RetryToken, errorInfo: RetryErrorInfo): Promise<RetryToken> {
    const retry = token.getRetryCount() + 1;
    if (errorInfo.errorType === "CLIENT_ERROR" || retry >= AWS_CALL_ATTEMPTS) {
      throw new Error("no retry left");
    }
    const delay = retryDelayMs(retry);
    const code = (errorInfo.error as { code?: string; name?: string } | undefined)?.code
      ?? errorInfo.error?.name ?? errorInfo.errorType;
    process.stderr.write(`AWS call failed (${code}); retrying in ${delay / 1000} s, attempt ${retry + 1} of ${AWS_CALL_ATTEMPTS}\n`);
    return { getRetryCount: () => retry, getRetryDelay: () => delay };
  },
  recordSuccess(): void {},
};

export interface AwsCallerIdentity {
  account: string;
  arn: string;
}

/** Reports profile resolution failures without allowing another credential source to run. */
export class AwsProfileCredentialsError extends Error {
  readonly profile: string;

  constructor(profile: string, cause: unknown) {
    super(
      `AWS profile ${profile} was not found in the shared config/credentials files. Create the profile, or pass an existing name with --aws-profile. AWS_PROFILE is also read.`,
      { cause },
    );
    this.name = "AwsProfileCredentialsError";
    this.profile = profile;
  }
}

/** Return the explicitly requested profile, including one selected through the environment. */
function requestedProfile(profile: string | undefined): string | undefined {
  const value = profile === undefined || profile.trim() === "" ? process.env.AWS_PROFILE : profile;
  return value === undefined || value.trim() === "" ? undefined : value;
}

/**
 * Build service client options. A named profile uses only that profile and
 * converts every resolution error into a message that names it.
 */
export async function awsClientOptions(
  region: string,
  profile?: string,
): Promise<AwsClientOptions> {
  const selected = requestedProfile(profile);
  if (selected === undefined) return { region, retryStrategy: networkTolerantRetryStrategy };

  const { fromIni } = await import("@aws-sdk/credential-provider-ini");
  const resolve = fromIni({ profile: selected });
  return {
    region,
    retryStrategy: networkTolerantRetryStrategy,
    credentials: async () => {
      try {
        return await resolve();
      } catch (cause) {
        throw new AwsProfileCredentialsError(selected, cause);
      }
    },
  };
}

/** Format the account and principal returned by STS before an account operation starts. */
export function formatAwsIdentity(
  identity: AwsCallerIdentity,
  profile?: string,
): string {
  const selected = requestedProfile(profile);
  const suffix = selected === undefined ? "" : `, profile ${selected}`;
  return `AWS identity: account ${identity.account}, identity ${identity.arn}${suffix}`;
}
