/**
 * Builds AWS client options that bind a requested shared configuration profile
 * to the client credential provider.
 */

type ProfileCredentialsProvider = ReturnType<
  typeof import("@aws-sdk/credential-provider-ini")["fromIni"]
>;

export interface AwsClientOptions {
  region: string;
  credentials?: ProfileCredentialsProvider;
}

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
  if (selected === undefined) return { region };

  const { fromIni } = await import("@aws-sdk/credential-provider-ini");
  const resolve = fromIni({ profile: selected });
  return {
    region,
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
