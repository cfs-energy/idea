# Private parity CI provisioning

Status: workflow and harness implemented; no AWS or GitHub resources created.

Provision through the dev account's enforcing Terraform/Spacelift workflow in
**459724015654 / us-east-2**:

- S3 bucket `idea-ideactl-parity-459724015654-us-east-2`, all four Block Public
  Access settings enabled, Object Ownership `BucketOwnerEnforced`, default
  encryption SSE-S3 (`AES256`), versioning enabled. No public ACL or read policy.
- Use the existing IAM OIDC provider
  `arn:aws:iam::459724015654:oidc-provider/token.actions.githubusercontent.com`,
  or create it with URL `https://token.actions.githubusercontent.com` and client
  ID `sts.amazonaws.com` if absent.
- Role `idea-ideactl-parity-read`, maximum session duration 3600 seconds; the
  workflow requests 900 seconds. Exact trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::459724015654:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:cfs-energy/idea:ref:refs/heads/main"
      }
    }
  }]
}
```

Exact role read policy (no listing, writes, other keys or KMS permissions):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::idea-ideactl-parity-459724015654-us-east-2/captures/v1.tar.gz"
  }]
}
```

Bucket policy requiring TLS:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": [
      "arn:aws:s3:::idea-ideactl-parity-459724015654-us-east-2",
      "arn:aws:s3:::idea-ideactl-parity-459724015654-us-east-2/*"
    ],
    "Condition": {"Bool": {"aws:SecureTransport": "false"}}
  }]
}
```

An authorized capture maintainer uploads `captures/v1.tar.gz`. The CI role must
never get upload permission. Review the bundle for credentials before upload;
these are private configuration captures, not a secret distribution mechanism.
Build it relative to `source/idea/ideactl`, including complete `tools/parity/live`,
`tools/parity/fixtures` (including raw scans, Python CDK assemblies, bootstrap
and w19 oracles), and `docs/port` (including `requests/security-fix.md`). Include
only regular files/directories, no symlinks, and at most 1 GiB uncompressed.
Do not include node_modules, credentials, logs or generated test outputs.

After uploading, set repository variable `IDEACTL_PARITY_SHA256` to the archive's
SHA-256 hex digest, then `IDEACTL_PRIVATE_PARITY=1`. Leave the latter unset until
provisioning and the complete capture bundle are ready. A configured job fails
on authentication, download, checksum, missing captures or failed assertions;
these failures must not become silent skips.

In `.github/workflows/build_push.yaml`, the deps worker must add to the existing
`jobs.checks` reusable-workflow caller (currently lines 29-30):

```yaml
    permissions:
      contents: read
      id-token: write
```

No other build workflow edits are needed. PRs, forks and non-main dispatches skip
`private-parity`; public checks still run. Protect main and workflow edits because
main code can read the captures. The private job uses a fresh GitHub-hosted Ubuntu
runner, with no dependency caches or artifact uploads. Test output is kept in a
0700 temporary directory, only numeric totals are published, and all captures
and logs are deleted on completion. For failures, reproduce locally with the
approved bundle; do not paste capture-derived assertions into public logs.

The private run sets `IDEACTL_REQUIRE_PRIVATE_CAPTURES=1`, so missing required or
optional captures fail even with `IDEACTL_PUBLIC_CHECKOUT=1`. Platform-only tests
(such as macOS executable builds on Linux) retain their declared skips. Python
and DynamoDB are installed before testing. The public run still reports absent capture files.
The private job cannot be integration-verified until the above resources and
bundle exist.

References: [AWS OIDC role trust](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html),
[GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws),
[reusable workflow permission limits](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).
