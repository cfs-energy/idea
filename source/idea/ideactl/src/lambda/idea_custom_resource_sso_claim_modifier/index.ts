import type { JsonValue } from "../commons/cfn-response.ts";

/** JSON object shape accepted by the Cognito pre-token-generation trigger. */
export type SsoClaimModifierEvent = Record<string, JsonValue>;

/** Four custom attributes copied into Cognito ID-token claims. */
export type SsoClaimOverrides = {
    "custom:aws_region": JsonValue;
    "custom:cluster_name": JsonValue;
    "custom:password_last_set": JsonValue;
    "custom:password_max_age": JsonValue;
};

/** Response shape required by Cognito trigger event version 1. */
export type SsoClaimModifierResponse = {
    claimsOverrideDetails: {
        claimsToAddOrOverride: SsoClaimOverrides;
    };
};

/** Narrow a runtime value to the mapping shape used by Python dict.get. */
function requireMapping(
    value: JsonValue,
    fieldName: string,
): Record<string, JsonValue> {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        throw new TypeError(`${fieldName} must be an object`);
    }
    return value;
}

/** Read a mapping key with the same missing-key default as Python dict.get. */
function mappingGet(
    mapping: Record<string, JsonValue>,
    key: string,
    defaultValue: JsonValue,
): JsonValue {
    return Object.hasOwn(mapping, key) ? mapping[key] : defaultValue;
}

/**
 * Copy IDEA custom user attributes into Cognito ID-token claims.
 *
 * Return a promise so Cognito receives the mutated event.
 */
export async function handler(
    event: SsoClaimModifierEvent,
    _context: unknown,
): Promise<SsoClaimModifierEvent> {
    // Match event.get("request", {}).get("userAttributes", {}) exactly.
    const request = requireMapping(
        mappingGet(event, "request", {}),
        "request",
    );
    const userAttributes = requireMapping(
        mappingGet(request, "userAttributes", {}),
        "request.userAttributes",
    );

    // Cognito trigger V1 accepts these values directly in the response.
    const response: SsoClaimModifierResponse = {
        claimsOverrideDetails: {
            claimsToAddOrOverride: {
                "custom:aws_region": mappingGet(
                    userAttributes,
                    "custom:aws_region",
                    null,
                ),
                "custom:cluster_name": mappingGet(
                    userAttributes,
                    "custom:cluster_name",
                    null,
                ),
                "custom:password_last_set": mappingGet(
                    userAttributes,
                    "custom:password_last_set",
                    null,
                ),
                "custom:password_max_age": mappingGet(
                    userAttributes,
                    "custom:password_max_age",
                    null,
                ),
            },
        },
    };

    event.response = response;
    return event;
}
