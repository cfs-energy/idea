import assert from "node:assert/strict";
import test from "node:test";
import { NoSuchEntityException } from "@aws-sdk/client-iam";
import type { Role } from "@aws-sdk/client-iam";
import type {
    CfnLogger,
    CfnResponse,
    CfnResponseSender,
    SendCfnResponseOptions,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    ProjectRoleBoundariesEvent,
    ProjectRoleBoundariesIam,
} from "../../src/lambda/idea_custom_resource_detach_project_boundaries/index.ts";
import { createHandler } from "../../src/lambda/idea_custom_resource_detach_project_boundaries/index.ts";

/** Python `PHYSICAL_RESOURCE_ID`; CloudFormation replacement turns on this string. */
const PYTHON_PHYSICAL_RESOURCE_ID = "project-role-boundaries";

const context = { logStreamName: "synthetic-log-stream" };
const rolePath = "/idea/sample-cluster/projects/";
const boundaryPolicyArn = "synthetic-boundary-policy";

/** Build a synthetic custom-resource event without embedding live identifiers. */
function makeEvent(
    requestType: ProjectRoleBoundariesEvent["RequestType"],
): ProjectRoleBoundariesEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticProjectRoleBoundaries",
        ResourceProperties: {
            RolePath: rolePath,
            BoundaryPolicyArn: boundaryPolicyArn,
        },
    };
}

/** Record CloudFormation responses instead of issuing HTTP requests. */
function makeResponseRecorder(): {
    responses: CfnResponse[];
    options: Array<SendCfnResponseOptions | undefined>;
    sender: CfnResponseSender;
} {
    const responses: CfnResponse[] = [];
    const options: Array<SendCfnResponseOptions | undefined> = [];
    return {
        responses,
        options,
        sender: async (
            response: CfnResponse,
            senderOptions?: SendCfnResponseOptions,
        ): Promise<void> => {
            responses.push(response);
            options.push(senderOptions);
        },
    };
}

/** Build one complete synthetic IAM role with an optional boundary. */
function makeRole(
    roleName: string,
    permissionsBoundaryArn?: string,
): Role {
    return {
        Path: rolePath,
        RoleName: roleName,
        RoleId: `id-${roleName}`,
        Arn: `identifier-${roleName}`,
        CreateDate: new Date(0),
        PermissionsBoundary:
            permissionsBoundaryArn === undefined
                ? undefined
                : {
                      PermissionsBoundaryType: "PermissionsBoundaryPolicy",
                      PermissionsBoundaryArn: permissionsBoundaryArn,
                  },
    };
}

/** Supply harmless IAM defaults while allowing one operation to vary per test. */
function makeIam(
    overrides: Partial<ProjectRoleBoundariesIam> = {},
): ProjectRoleBoundariesIam {
    return {
        listRoles: async () => ({ $metadata: {}, Roles: [] }),
        getRole: async (input) => {
            if (input.RoleName === undefined) {
                throw new Error("Synthetic GetRole requires RoleName");
            }
            return {
                $metadata: {},
                Role: makeRole(input.RoleName),
            };
        },
        deleteRolePermissionsBoundary: async () => ({ $metadata: {} }),
        ...overrides,
    };
}

/** Build the AWS SDK exception caught by the Python-equivalent branches. */
function noSuchEntity(): NoSuchEntityException {
    return new NoSuchEntityException({
        $metadata: {},
        message: "synthetic role disappeared",
    });
}

test("Create is a no-op and returns the stable physical id", async () => {
    let iamFactoryCalls = 0;
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => {
            iamFactoryCalls += 1;
            return makeIam();
        },
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(iamFactoryCalls, 0);
    assert.equal(recorder.responses.length, 1);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, {});
});

test("Update is a no-op and returns the stable physical id", async () => {
    let iamFactoryCalls = 0;
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => {
            iamFactoryCalls += 1;
            return makeIam();
        },
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(iamFactoryCalls, 0);
    assert.equal(recorder.responses.length, 1);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, {});
});

test("Delete paginates serially, reads each role, and clears only matching boundaries", async () => {
    const listInputs: Array<
        Parameters<ProjectRoleBoundariesIam["listRoles"]>[0]
    > = [];
    const readRoleNames: string[] = [];
    const deletedRoleNames: string[] = [];
    let pageNumber = 0;
    const iam = makeIam({
        listRoles: async (input) => {
            listInputs.push(input);
            pageNumber += 1;
            if (pageNumber === 1) {
                return {
                    $metadata: {},
                    Roles: undefined,
                    IsTruncated: true,
                    Marker: "synthetic-marker",
                };
            }
            return {
                $metadata: {},
                Roles: [
                    {
                        Path: rolePath,
                        RoleName: "missing-role-payload",
                        RoleId: "role-0",
                        Arn: "role-identifier-0",
                        CreateDate: new Date(0),
                    },
                    {
                        Path: rolePath,
                        RoleName: "without-boundary",
                        RoleId: "role-1",
                        Arn: "role-identifier-1",
                        CreateDate: new Date(0),
                    },
                    {
                        Path: rolePath,
                        RoleName: "other-boundary",
                        RoleId: "role-2",
                        Arn: "role-identifier-2",
                        CreateDate: new Date(0),
                    },
                    {
                        Path: rolePath,
                        RoleName: "matching-boundary",
                        RoleId: "role-3",
                        Arn: "role-identifier-3",
                        CreateDate: new Date(0),
                    },
                ],
            };
        },
        getRole: async (input) => {
            if (input.RoleName === undefined) {
                throw new Error("Synthetic GetRole requires RoleName");
            }
            const roleName = input.RoleName;
            readRoleNames.push(roleName);
            if (roleName === "missing-role-payload") {
                return { $metadata: {}, Role: undefined };
            }
            const permissionsBoundaryArn =
                roleName === "without-boundary"
                    ? undefined
                    : roleName === "other-boundary"
                      ? "synthetic-other-boundary"
                      : boundaryPolicyArn;
            return {
                $metadata: {},
                Role: makeRole(roleName, permissionsBoundaryArn),
            };
        },
        deleteRolePermissionsBoundary: async (input) => {
            if (input.RoleName === undefined) {
                throw new Error(
                    "Synthetic DeleteRolePermissionsBoundary requires RoleName",
                );
            }
            deletedRoleNames.push(input.RoleName);
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => iam,
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.deepEqual(listInputs, [
        { PathPrefix: rolePath },
        { PathPrefix: rolePath, Marker: "synthetic-marker" },
    ]);
    assert.deepEqual(readRoleNames, [
        "missing-role-payload",
        "without-boundary",
        "other-boundary",
        "matching-boundary",
    ]);
    assert.deepEqual(deletedRoleNames, ["matching-boundary"]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, { ClearedCount: "1" });
});

test("Delete ignores a role that disappears before GetRole", async () => {
    const deletedRoleNames: string[] = [];
    const iam = makeIam({
        listRoles: async () => ({
            $metadata: {},
            Roles: [
                {
                    Path: rolePath,
                    RoleName: "disappeared-role",
                    RoleId: "role-1",
                    Arn: "role-identifier-1",
                    CreateDate: new Date(0),
                },
                {
                    Path: rolePath,
                    RoleName: "remaining-role",
                    RoleId: "role-2",
                    Arn: "role-identifier-2",
                    CreateDate: new Date(0),
                },
            ],
        }),
        getRole: async (input) => {
            if (input.RoleName === undefined) {
                throw new Error("Synthetic GetRole requires RoleName");
            }
            if (input.RoleName === "disappeared-role") {
                throw noSuchEntity();
            }
            return {
                $metadata: {},
                Role: makeRole(input.RoleName, boundaryPolicyArn),
            };
        },
        deleteRolePermissionsBoundary: async (input) => {
            if (input.RoleName === undefined) {
                throw new Error(
                    "Synthetic DeleteRolePermissionsBoundary requires RoleName",
                );
            }
            deletedRoleNames.push(input.RoleName);
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => iam,
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.deepEqual(deletedRoleNames, ["remaining-role"]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, { ClearedCount: "1" });
});

test("Delete ignores a role that disappears while its boundary is removed", async () => {
    const iam = makeIam({
        listRoles: async () => ({
            $metadata: {},
            Roles: [
                {
                    Path: rolePath,
                    RoleName: "disappeared-role",
                    RoleId: "role-1",
                    Arn: "role-identifier-1",
                    CreateDate: new Date(0),
                },
            ],
        }),
        getRole: async (input) => {
            if (input.RoleName === undefined) {
                throw new Error("Synthetic GetRole requires RoleName");
            }
            return {
                $metadata: {},
                Role: makeRole(input.RoleName, boundaryPolicyArn),
            };
        },
        deleteRolePermissionsBoundary: async () => {
            throw noSuchEntity();
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => iam,
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, { ClearedCount: "0" });
});

test("Delete still succeeds when a malformed ListRoles entry has no role name", async () => {
    const validationFailure = new Error(
        "synthetic GetRole RoleName validation failure",
    );
    const iam = makeIam({
        listRoles: async () => ({
            $metadata: {},
            Roles: [
                {
                    Path: rolePath,
                    RoleName: undefined,
                    RoleId: "role-id",
                    Arn: "role-identifier",
                    CreateDate: new Date(0),
                },
            ],
        }),
        getRole: async (input) => {
            assert.equal(input.RoleName, undefined);
            throw validationFailure;
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => iam,
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, {
        error: validationFailure.message,
    });
});

for (const operation of [
    "ListRoles",
    "GetRole",
    "DeleteRolePermissionsBoundary",
] as const) {
    test(`Delete reports SUCCESS with error data when ${operation} fails`, async () => {
        const failure = new Error(`synthetic ${operation} failure`);
        const matchingRole = makeRole("matching-role");
        const iam = makeIam({
            listRoles: async () => {
                if (operation === "ListRoles") {
                    throw failure;
                }
                return { $metadata: {}, Roles: [matchingRole] };
            },
            getRole: async () => {
                if (operation === "GetRole") {
                    throw failure;
                }
                return {
                    $metadata: {},
                    Role: makeRole("matching-role", boundaryPolicyArn),
                };
            },
            deleteRolePermissionsBoundary: async () => {
                if (operation === "DeleteRolePermissionsBoundary") {
                    throw failure;
                }
                return { $metadata: {} };
            },
        });
        const messages: string[] = [];
        const logger: CfnLogger = {
            info: (message: string): void => {
                messages.push(message);
            },
            error: (message: string): void => {
                messages.push(message);
            },
        };
        const recorder = makeResponseRecorder();

        await createHandler({
            iam: (): ProjectRoleBoundariesIam => iam,
            logger,
            responseSender: recorder.sender,
        })(makeEvent("Delete"), context);

        assert.equal(recorder.responses[0].status, "SUCCESS");
        assert.equal(
            recorder.responses[0].physicalResourceId,
            PYTHON_PHYSICAL_RESOURCE_ID,
        );
        assert.deepEqual(recorder.responses[0].data, {
            error: failure.message,
        });
        assert.ok(
            messages.includes(
                `Failed to clear project role boundaries: ${failure.message}`,
            ),
        );
    });
}

test("Delete stops paginating on IsTruncated even when a Marker is returned", async () => {
    let listCalls = 0;
    const iam = makeIam({
        listRoles: async () => {
            listCalls += 1;
            if (listCalls > 50) {
                throw new Error(`did not terminate: ${listCalls} ListRoles calls`);
            }
            // A final page may still carry a Marker; boto3's paginator ignores it.
            return {
                $metadata: {},
                Roles: [],
                IsTruncated: false,
                Marker: "synthetic-marker",
            };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => iam,
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.equal(listCalls, 1);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, { ClearedCount: "0" });
});

test("Delete swallows NoSuchEntity raised by a second copy of the IAM client", async () => {
    // A duplicated client bundle produces a distinct class, so the swallow has to
    // match the error name rather than the constructor.
    const duplicatedClientError = new Error("synthetic role disappeared");
    duplicatedClientError.name = "NoSuchEntityException";
    const iam = makeIam({
        listRoles: async () => ({
            $metadata: {},
            Roles: [makeRole("disappeared-role")],
        }),
        getRole: async () => {
            throw duplicatedClientError;
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        iam: (): ProjectRoleBoundariesIam => iam,
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.deepEqual(recorder.responses[0].data, { ClearedCount: "0" });
});

test("the response helper receives the injected logger", async () => {
    const messages: string[] = [];
    const logger: CfnLogger = {
        info: (message: string): void => {
            messages.push(message);
        },
        error: (message: string): void => {
            messages.push(message);
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.options.length, 1);
    assert.equal(recorder.options[0]?.logger, logger);
});

// botocore's paginator also stops when a truncated page carries no Marker.
test("Delete stops paginating when IsTruncated is true but no Marker is returned", async () => {
    let listCalls = 0;
    const iam = makeIam({
        listRoles: async () => {
            listCalls += 1;
            if (listCalls > 50) throw new Error(`did not terminate: ${listCalls} ListRoles calls`);
            return { $metadata: {}, Roles: [], IsTruncated: true };
        },
    });
    const recorder = makeResponseRecorder();
    await createHandler({ iam: (): ProjectRoleBoundariesIam => iam, responseSender: recorder.sender })(makeEvent("Delete"), context);
    assert.equal(listCalls, 1);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
});
