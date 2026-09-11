/**
 * Operator error-message rewrites.
 * Drives throw sites and the CLI display layer with synthetic identifiers only.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ExitWithCode } from "../../src/cli/cdk-invoker.ts";
import { customTagsToKeyValuePairs } from "../../src/cli/commands/bootstrap.ts";
import { DeleteClusterAbort } from "../../src/cli/commands/delete-cluster.ts";
import { run } from "../../src/cli/main.ts";
import { AwsProfileCredentialsError } from "../../src/cli/aws-client-options.ts";
import {
  ClusterConfig,
  ClusterConfigError,
  ConfigKeyNotFound,
  GeneralException,
} from "../../src/config/cluster-config.ts";
import { buildContext, InvalidParams, type UserValues } from "../../src/config/values.ts";
import { createHandler } from "../../src/lambda/idea_ec2_state_event_transformation_lambda/index.ts";
import { postMetrics } from "../../src/lambda/idea_solution_metrics/index.ts";
import { getTargetGroupName } from "../../src/util/names.ts";
import { fakeDeps } from "../w20a/harness.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";

function argv(...rest: string[]): string[] {
  return [...rest, "--cluster-name", CLUSTER, "--aws-region", REGION];
}

function requiredValues(overrides: UserValues = {}): UserValues {
  return {
    cluster_name: CLUSTER,
    administrator_email: "admin@example.invalid",
    aws_account_id: "123456789012",
    aws_dns_suffix: "amazonaws.com",
    aws_partition: "aws",
    aws_region: REGION,
    ssh_key_pair_name: "sample-key",
    vpc_cidr_block: "203.0.113.0/24",
    instance_ami: "ami-0123456789abcdef0",
    dcv_connection_gateway_instance_ami: "ami-0123456789abcdef0",
    dcv_broker_instance_ami: "ami-0123456789abcdef0",
    ...overrides,
  };
}

describe("display layer (R1, R2, R3, R4, R5, R6, R7, R12)", () => {
  it("prints a GeneralException as one line and does not rethrow (R1)", async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new GeneralException("deployment failed. could not deploy module: cluster");
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.deepEqual(deps.stderr, ["deployment failed. could not deploy module: cluster"]);
  });

  it("maps a missing DynamoDB table onto the install sentence (R2)", async () => {
    const missing = new Error("Requested resource not found");
    missing.name = "ResourceNotFoundException";
    await assert.rejects(
      () =>
        ClusterConfig.fromDynamoDb(CLUSTER, REGION, {
          scan: async () => {
            throw missing;
          },
        }),
      (error: Error) =>
        error instanceof ClusterConfigError &&
        error.message ===
          `No configuration tables for cluster ${CLUSTER} in ${REGION} (looked for ${CLUSTER}.modules and ${CLUSTER}.cluster-settings). Install with ideactl quick-setup, or run ideactl config update --cluster-name ${CLUSTER} --aws-region ${REGION}. If the cluster already exists, check --aws-region and --aws-profile.`,
    );

    const deps = fakeDeps();
    deps.scan = async () => {
      throw missing;
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.equal(
      deps.stderr[0],
      `No configuration tables for cluster ${CLUSTER} in ${REGION} (looked for ${CLUSTER}.modules and ${CLUSTER}.cluster-settings). Install with ideactl quick-setup, or run ideactl config update --cluster-name ${CLUSTER} --aws-region ${REGION}. If the cluster already exists, check --aws-region and --aws-profile.`,
    );
  });

  it("rewrites an uninitialised ClusterConfigDbError and names the region (R3)", async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      const error = new Error(`Configuration tables not found for cluster: ${CLUSTER}`);
      error.name = "ClusterConfigDbError";
      throw error;
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.equal(
      deps.stderr[0],
      `Configuration tables not found for cluster ${CLUSTER} in ${REGION}. Create them with ideactl config update --cluster-name ${CLUSTER} --aws-region ${REGION}, or confirm the cluster was installed in this account and region.`,
    );
  });

  it("prints a non-empty ExitWithCode message (R4)", async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new ExitWithCode(
        1,
        `Stack ${CLUSTER}-cluster ended UPDATE_ROLLBACK_COMPLETE: Stack rollback was requested. No further modules were deployed. Open the stack events in CloudFormation, fix the failing resource, then re-run the same deploy.`,
      );
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.match(deps.stderr[0] ?? "", /Stack sample-cluster-cluster ended UPDATE_ROLLBACK_COMPLETE/);
  });

  it("prints the full missing configuration key (R5)", async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new ConfigKeyNotFound("'cluster_s3_bucket', key: cluster.cluster_s3_bucket");
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.equal(
      deps.stderr[0],
      `Configuration key cluster.cluster_s3_bucket is missing for this cluster. Show nearby keys with ideactl config show --cluster-name ${CLUSTER} --aws-region ${REGION}, or set it with ideactl config set.`,
    );
  });

  it("names a missing AWS profile (R6)", async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new AwsProfileCredentialsError("does-not-exist-ideactl-audit", new Error("not found"));
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.equal(
      deps.stderr[0],
      "AWS profile does-not-exist-ideactl-audit was not found in the shared config/credentials files. Create the profile, or pass an existing name with --aws-profile. AWS_PROFILE is also read.",
    );
  });

  it("says when no credentials loaded (R7)", async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      const error = new Error("Could not load credentials from any providers");
      error.name = "CredentialsProviderError";
      throw error;
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.equal(
      deps.stderr[0],
      `No AWS credentials were loaded for region ${REGION}. Export keys, start a federated session, or pass --aws-profile. Then retry.`,
    );
  });

  it("prints a DeleteClusterAbort as one line (R12)", async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new DeleteClusterAbort(
        `CloudFormation stacks for cluster ${CLUSTER} did not all delete. Later delete steps were not run. Check the stack events, then re-run ideactl delete-cluster --cluster-name ${CLUSTER} --aws-region ${REGION}.`,
      );
    };
    assert.equal(await run(argv("list-modules"), deps), 1);
    assert.match(deps.stderr[0] ?? "", /CloudFormation stacks for cluster sample-cluster did not all delete/);
  });
});

describe("values.yml messages (R8, R9, R10, R11)", () => {
  it("names use_existing_apps_fs when the apps file-system id is missing (R8)", () => {
    assert.throws(
      () =>
        buildContext(
          requiredValues({
            use_existing_vpc: true,
            vpc_id: "vpc-000000000000000a1",
            private_subnet_ids: ["subnet-000000000000000b1"],
            use_existing_apps_fs: true,
          }),
        ),
      (error: Error) =>
        error instanceof InvalidParams &&
        error.message ===
          "existing_apps_fs_id is required when use_existing_apps_fs = True. Set the file-system id in values.yml, then re-run.",
    );
  });

  it("names the username secret key, not the password secret key (R9)", () => {
    assert.throws(
      () =>
        buildContext(
          requiredValues({
            use_existing_vpc: true,
            vpc_id: "vpc-000000000000000a1",
            private_subnet_ids: ["subnet-000000000000000b1"],
            use_existing_directory_service: true,
            directory_id: "d-0000000001",
            directory_service_root_password_secret_arn:
              "arn:aws:secretsmanager:us-east-2:123456789012:secret:sample-password",
          }),
        ),
      (error: Error) =>
        error instanceof InvalidParams &&
        error.message ===
          "directory_service_root_username_secret_arn is required when use_existing_directory_service = True. Set that key in values.yml, then re-run.",
    );
  });

  it("requires the data file-system id from the data flag, not the apps flag (R10)", () => {
    assert.throws(
      () =>
        buildContext(
          requiredValues({
            use_existing_vpc: true,
            vpc_id: "vpc-000000000000000a1",
            private_subnet_ids: ["subnet-000000000000000b1"],
            use_existing_data_fs: true,
          }),
        ),
      (error: Error) =>
        error instanceof InvalidParams &&
        error.message ===
          "existing_data_fs_id is required when use_existing_data_fs = True. Set the file-system id in values.yml, then re-run.",
    );
    assert.doesNotThrow(() =>
      buildContext(
        requiredValues({
          use_existing_vpc: true,
          vpc_id: "vpc-000000000000000a1",
          private_subnet_ids: ["subnet-000000000000000b1"],
          use_existing_apps_fs: true,
          existing_apps_fs_id: "fs-000000000000000d1",
        }),
      ),
    );
  });

  it("says both subnet lists are empty when an existing VPC has neither (R11)", () => {
    assert.throws(
      () =>
        buildContext(
          requiredValues({
            use_existing_vpc: true,
            vpc_id: "vpc-000000000000000a1",
          }),
        ),
      (error: Error) =>
        error instanceof InvalidParams &&
        error.message ===
          "use_existing_vpc is True, but both private_subnet_ids and public_subnet_ids are empty in values.yml. Set at least one list, then re-run.",
    );
  });
});

describe("other throw sites (R16, R18, R19, R20)", () => {
  it("rejects a malformed bootstrap tag (R16)", () => {
    assert.throws(
      () => customTagsToKeyValuePairs(["malformed"]),
      (error: Error) =>
        error.name === "IndexError" &&
        error.message ===
          "Custom tag malformed is not in Key=k,Value=v form. Fix the tag and re-run bootstrap.",
    );
  });

  it("rejects a target group name longer than 32 characters (R18)", () => {
    assert.throws(
      () => getTargetGroupName("idea-dev27", "cluster-manager", "a-very-long-identifier"),
      /Target group name .+ is longer than 32 characters/,
    );
  });

  it("names a missing EC2 tag field (R19)", async () => {
    const errors: string[] = [];
    await createHandler({
      ec2: {
        describeInstances: async () => ({
          $metadata: {},
          Reservations: [{ Instances: [{ Tags: [{ Value: "synthetic-cluster" }] }] }],
        }),
      },
      env: {
        IDEA_CLUSTER_NAME_TAG_KEY: "idea:ClusterName",
        IDEA_CLUSTER_NAME_TAG_VALUE: "synthetic-cluster",
        IDEA_TAG_PREFIX: "idea:",
      },
      logger: {
        info: () => {},
        error: (message: string) => {
          errors.push(message);
        },
      },
    })(
      {
        "detail-type": "EC2 Instance State-change Notification",
        detail: { "instance-id": "i-synthetic", state: "running" },
      },
      undefined,
    );
    assert.match(
      errors[0] ?? "",
      /EC2 tag is missing field Key\. The event was EC2 Instance State-change Notification for instance i-synthetic in cluster synthetic-cluster/,
    );
  });

  it("names a missing custom-resource event field (R19)", async () => {
    const errors: Array<{ message: string }> = [];
    await postMetrics(
      { RequestType: "Create", ResourceProperties: {} },
      {
        logger: {
          info: () => {},
          error: (message: string) => {
            errors.push({ message });
          },
        },
        post: async () => ({ status: 200 }),
        timestamp: () => "2026-09-10 00:00:00.000000",
      },
    );
    assert.equal(errors[0]?.message, "failed to post metrics: Custom resource event is missing field RequestId.");
  });
});
