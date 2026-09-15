import assert from "node:assert/strict";
import { test } from "node:test";

import type { CfnResourceContext } from "../../src/lambda/commons/cfn-response.ts";
import {
    createHandler,
    type ReleaseScaleInProtectionAutoScaling,
    type ReleaseScaleInProtectionEvent,
} from "../../src/lambda/idea_custom_resource_release_scale_in_protection/index.ts";

const context = { logStreamName: "stream" } as CfnResourceContext;
const silent = { info: () => undefined, error: () => undefined };

function event(requestType: "Create" | "Update" | "Delete"): ReleaseScaleInProtectionEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "stack",
        RequestId: "request",
        LogicalResourceId: "release",
        ResourceType: "Custom::ReleaseScaleInProtection",
        ResourceProperties: { ServiceToken: "token", AutoScalingGroupName: "hosts" },
    } as ReleaseScaleInProtectionEvent;
}

function fakeAutoScaling(members: string[] | undefined) {
    const calls: string[] = [];
    const autoScaling: ReleaseScaleInProtectionAutoScaling = {
        describeGroup: async (name) => {
            calls.push(`describe ${name}`);
            return members === undefined ? undefined : { protectedInstanceIds: members };
        },
        clearGroupDefault: async (name) => {
            calls.push(`group ${name}`);
        },
        clearInstanceProtection: async (name, ids) => {
            calls.push(`instances ${name} ${ids.join(",")}`);
        },
    };
    return { autoScaling, calls };
}

async function run(requestType: "Create" | "Update" | "Delete", members: string[] | undefined) {
    const { autoScaling, calls } = fakeAutoScaling(members);
    const responses: Array<{ status: string; physicalResourceId?: string }> = [];
    const handler = createHandler({
        autoScaling: () => autoScaling,
        logger: silent,
        responseSender: async (response) => {
            responses.push({ status: response.status, physicalResourceId: response.physicalResourceId });
        },
    });
    await handler(event(requestType), context);
    return { calls, responses };
}

test("delete clears the group default and only the members that carry the flag", async () => {
    const { calls, responses } = await run("Delete", ["i-1", "i-3"]);
    assert.deepEqual(calls, ["describe hosts", "group hosts", "instances hosts i-1,i-3"]);
    assert.deepEqual(responses, [{ status: "SUCCESS", physicalResourceId: "scale-in-protection-hosts" }]);
});

test("delete of a group with no protected member touches no instance", async () => {
    const { calls } = await run("Delete", []);
    assert.deepEqual(calls, ["describe hosts", "group hosts"]);
});

test("delete of a group that is gone is not an error", async () => {
    const { calls, responses } = await run("Delete", undefined);
    assert.deepEqual(calls, ["describe hosts"]);
    assert.equal(responses[0]?.status, "SUCCESS");
});

test("create and update release nothing", async () => {
    for (const requestType of ["Create", "Update"] as const) {
        const { calls, responses } = await run(requestType, ["i-1"]);
        assert.deepEqual(calls, []);
        assert.deepEqual(responses, [{ status: "SUCCESS", physicalResourceId: "scale-in-protection-hosts" }]);
    }
});

test("a failed release reports FAILED with the error", async () => {
    const responses: Array<{ status: string; data?: unknown }> = [];
    const handler = createHandler({
        autoScaling: () => ({
            describeGroup: async () => ({ protectedInstanceIds: ["i-1"] }),
            clearGroupDefault: async () => {
                throw new Error("AccessDenied");
            },
            clearInstanceProtection: async () => undefined,
        }),
        logger: silent,
        responseSender: async (response) => {
            responses.push({ status: response.status, data: response.data });
        },
    });
    await handler(event("Delete"), context);
    assert.equal(responses[0]?.status, "FAILED");
    assert.match(JSON.stringify(responses[0]?.data), /AccessDenied/);
});
