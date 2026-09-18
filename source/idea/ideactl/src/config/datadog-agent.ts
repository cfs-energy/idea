/**
 * The Datadog agent image the release ships with.
 *
 * IDEA publishes no agent image of its own. The default is Datadog's official image from their
 * public ECR gallery, pinned to the multi-architecture index digest of one agent version so the
 * same reference serves the Graviton host pool and an x86_64 cost-only task. An operator may
 * point `datadog_agent_image` (values) or `ecs.datadog.image` (settings) at any other
 * digest-pinned reference, at their own risk; a tag is never accepted because the agent runs with
 * the host's Docker socket and process namespace.
 *
 * To move the default: `docker buildx imagetools inspect public.ecr.aws/datadog/agent:<version>`
 * and copy the top-level `Digest`.
 */
export const DATADOG_AGENT_VERSION = "7.83.2";

export const DATADOG_AGENT_IMAGE =
  "public.ecr.aws/datadog/agent@sha256:29baa94e0a1abcadf43b2b2a002ad4406b3c05a46b8cae29ae1803a663795b59";

const DIGEST_PINNED_IMAGE = /^[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:\/[a-z0-9][a-z0-9._-]*)+@sha256:[0-9a-f]{64}$/;

/** True for `<registry>/<repository>@sha256:<64 hex>`; false for any tag or bare digest. */
export function isDigestPinnedImage(image: string): boolean {
  return DIGEST_PINNED_IMAGE.test(image);
}

export function requireDigestPinnedImage(image: string, setting: string): string {
  if (!isDigestPinnedImage(image)) {
    throw new Error(`${setting} must be a digest-pinned image reference (<registry>/<repository>@sha256:<digest>), not a tag`);
  }
  return image;
}
