import assert from "node:assert/strict";
import test from "node:test";
import type { JsonValue } from "../../src/lambda/commons/cfn-response.ts";
import type {
    SsoClaimModifierEvent,
} from "../../src/lambda/idea_custom_resource_sso_claim_modifier/index.ts";
import {
    handler,
} from "../../src/lambda/idea_custom_resource_sso_claim_modifier/index.ts";

const completeAttributes: Record<string, JsonValue> = {
    "custom:aws_region": "us-east-1",
    "custom:cluster_name": "synthetic-cluster",
    "custom:password_last_set": "2026-09-10T12:00:00Z",
    "custom:password_max_age": "90",
};

/** Build a synthetic Cognito pre-token-generation event. */
function makeEvent(
    userAttributes: JsonValue = completeAttributes,
): SsoClaimModifierEvent {
    return {
        version: "1",
        triggerSource: "TokenGeneration_Authentication",
        request: {
            userAttributes,
        },
        response: {
            stale: true,
        },
    };
}

test("returns a thenable so the Node runtime forwards the event to Cognito", async () => {
    // The runtime interface client only reports a handler's return value when it
    // is a promise; a plain object is dropped and Cognito is answered with null.
    const result = handler(makeEvent(), undefined);

    assert.equal(typeof (result as { then?: unknown }).then, "function");
    assert.ok(result instanceof Promise);
    await result;
});

test("copies all four attributes and returns the same mutated event", async () => {
    const event = makeEvent();

    const result = await handler(event, { ignored: true });

    assert.strictEqual(result, event);
    assert.deepEqual(result, {
        version: "1",
        triggerSource: "TokenGeneration_Authentication",
        request: {
            userAttributes: completeAttributes,
        },
        response: {
            claimsOverrideDetails: {
                claimsToAddOrOverride: completeAttributes,
            },
        },
    });
});

for (const missingClaim of Object.keys(completeAttributes)) {
    test(`writes null when ${missingClaim} is absent`, async () => {
        const attributes = { ...completeAttributes };
        delete attributes[missingClaim];
        const event = makeEvent(attributes);

        await handler(event, undefined);

        assert.deepEqual(event.response, {
            claimsOverrideDetails: {
                claimsToAddOrOverride: {
                    ...completeAttributes,
                    [missingClaim]: null,
                },
            },
        });
    });
}

test("writes four null claims when request is absent", async () => {
    const event: SsoClaimModifierEvent = {
        response: {
            stale: true,
        },
    };

    await handler(event, undefined);

    assert.deepEqual(event.response, {
        claimsOverrideDetails: {
            claimsToAddOrOverride: {
                "custom:aws_region": null,
                "custom:cluster_name": null,
                "custom:password_last_set": null,
                "custom:password_max_age": null,
            },
        },
    });
});

test("writes four null claims when userAttributes is absent", async () => {
    const event: SsoClaimModifierEvent = {
        request: {
            clientMetadata: null,
        },
    };

    await handler(event, undefined);

    assert.deepEqual(event.response, {
        claimsOverrideDetails: {
            claimsToAddOrOverride: {
                "custom:aws_region": null,
                "custom:cluster_name": null,
                "custom:password_last_set": null,
                "custom:password_max_age": null,
            },
        },
    });
});

test("preserves present false, zero, and empty values like Python dict.get", async () => {
    const event = makeEvent({
        "custom:aws_region": "",
        "custom:cluster_name": false,
        "custom:password_last_set": 0,
        "custom:password_max_age": null,
    });

    await handler(event, undefined);

    assert.deepEqual(event.response, {
        claimsOverrideDetails: {
            claimsToAddOrOverride: {
                "custom:aws_region": "",
                "custom:cluster_name": false,
                "custom:password_last_set": 0,
                "custom:password_max_age": null,
            },
        },
    });
});

test("rejects when request is null as Python raises", async () => {
    await assert.rejects(
        handler({ request: null }, undefined),
        /request must be an object/,
    );
});

test("rejects when request is not a mapping as Python raises", async () => {
    await assert.rejects(
        handler({ request: "not-a-mapping" }, undefined),
        /request must be an object/,
    );
});

test("rejects when userAttributes is null as Python raises", async () => {
    await assert.rejects(
        handler(makeEvent(null), undefined),
        /request\.userAttributes must be an object/,
    );
});

test("rejects when userAttributes is not a mapping as Python raises", async () => {
    await assert.rejects(
        handler(makeEvent([]), undefined),
        /request\.userAttributes must be an object/,
    );
});
