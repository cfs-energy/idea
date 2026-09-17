/**
 * The defused certificate handler.
 *
 * Two properties matter more than the values it returns. It never deletes anything, because the
 * handler it replaces force-destroyed the two secrets a live load balancer was serving on every
 * stack rollback. And it never creates anything, so a secret that is not there is a failure that
 * names the deploy tool rather than a fresh certificate silently replacing the one in service.
 */

import assert from "node:assert/strict";
import test from "node:test";

import type { CfnResponse, CfnResponseSender } from "../../src/lambda/commons/cfn-response.ts";
import type {
    SelfSignedCertificateAcm,
    SelfSignedCertificateEvent,
    SelfSignedCertificateSecrets,
} from "../../src/lambda/idea_custom_resource_self_signed_certificate/index.ts";
import {
    SECRET_NAME_TAG,
    createHandler,
    handler,
} from "../../src/lambda/idea_custom_resource_self_signed_certificate/index.ts";

const context = { logStreamName: "synthetic-log-stream" };
const CERTIFICATE_NAME = "sample-cluster-external";
const DOMAIN_NAME = "sample-cluster.idea.default";
const CERTIFICATE_ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:cert-AAAAAA";
const PRIVATE_KEY_ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:key-BBBBBB";
const ACM_ARN = "arn:aws:acm:us-east-2:123456789012:certificate/33333333-4444-5555-6666-777777777777";

function makeEvent(
    requestType: SelfSignedCertificateEvent["RequestType"],
    createAcmCertificate = false,
): SelfSignedCertificateEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticExternalCertificate",
        ResourceProperties: {
            domain_name: DOMAIN_NAME,
            certificate_name: CERTIFICATE_NAME,
            create_acm_certificate: createAcmCertificate,
            tags: { "idea:ClusterName": "sample-cluster" },
        },
    };
}

function makeResponseRecorder(): { responses: CfnResponse[]; sender: CfnResponseSender } {
    const responses: CfnResponse[] = [];
    return {
        responses,
        sender: async (response: CfnResponse): Promise<void> => {
            responses.push(response);
        },
    };
}

function makeSecrets(found: Array<{ Name: string; ARN: string }>): {
    secrets: SelfSignedCertificateSecrets;
    calls: string[][];
} {
    const calls: string[][] = [];
    return {
        calls,
        secrets: {
            listSecretsByTagValue: async (tagKey, tagValues) => {
                assert.equal(tagKey, SECRET_NAME_TAG);
                calls.push([...tagValues]);
                return found;
            },
        },
    };
}

const bothSecrets = [
    { Name: `${CERTIFICATE_NAME}-certificate`, ARN: CERTIFICATE_ARN },
    { Name: `${CERTIFICATE_NAME}-private-key`, ARN: PRIVATE_KEY_ARN },
];

const noAcm: SelfSignedCertificateAcm = { listIssuedCertificates: async () => [] };

test("create finds the two secrets by tag and returns their ARNs under the physical id", async () => {
    const recorder = makeResponseRecorder();
    const { secrets, calls } = makeSecrets(bothSecrets);
    await createHandler({ secrets: () => secrets, responseSender: recorder.sender })(
        makeEvent("Create"),
        context,
    );

    assert.equal(recorder.responses.length, 1);
    const response = recorder.responses[0] as CfnResponse;
    assert.equal(response.status, "SUCCESS");
    assert.equal(response.physicalResourceId, CERTIFICATE_NAME);
    assert.deepEqual(response.data, {
        certificate_secret_arn: CERTIFICATE_ARN,
        private_key_secret_arn: PRIVATE_KEY_ARN,
        acm_certificate_arn: null,
    });
    assert.deepEqual(calls, [[`${CERTIFICATE_NAME}-certificate`, `${CERTIFICATE_NAME}-private-key`]]);
});

test("update returns the same three values as create", async () => {
    const recorder = makeResponseRecorder();
    const { secrets } = makeSecrets(bothSecrets);
    await createHandler({ secrets: () => secrets, responseSender: recorder.sender })(
        makeEvent("Update"),
        context,
    );
    assert.equal((recorder.responses[0] as CfnResponse).status, "SUCCESS");
    assert.deepEqual((recorder.responses[0] as CfnResponse).data, {
        certificate_secret_arn: CERTIFICATE_ARN,
        private_key_secret_arn: PRIVATE_KEY_ARN,
        acm_certificate_arn: null,
    });
});

test("create_acm_certificate resolves the ISSUED certificate for the domain", async () => {
    const recorder = makeResponseRecorder();
    const { secrets } = makeSecrets(bothSecrets);
    const acm: SelfSignedCertificateAcm = {
        listIssuedCertificates: async () => [
            { DomainName: "other.idea.default", CertificateArn: "arn:aws:acm:us-east-2:123456789012:certificate/other" },
            { DomainName: DOMAIN_NAME, CertificateArn: ACM_ARN },
        ],
    };
    await createHandler({ secrets: () => secrets, acm: () => acm, responseSender: recorder.sender })(
        makeEvent("Create", true),
        context,
    );
    assert.equal((recorder.responses[0] as CfnResponse).data?.acm_certificate_arn, ACM_ARN);
});

test("delete does nothing and succeeds, without reading Secrets Manager or ACM", async () => {
    const recorder = makeResponseRecorder();
    await createHandler({
        secrets: () => {
            throw new Error("delete must not read Secrets Manager");
        },
        acm: () => {
            throw new Error("delete must not read ACM");
        },
        responseSender: recorder.sender,
    })(makeEvent("Delete", true), context);

    const response = recorder.responses[0] as CfnResponse;
    assert.equal(response.status, "SUCCESS");
    assert.equal(response.physicalResourceId, CERTIFICATE_NAME);
    assert.deepEqual(response.data, {});
});

test("a missing secret fails naming the deploy tool, rather than creating one", async () => {
    const recorder = makeResponseRecorder();
    const { secrets } = makeSecrets([{ Name: `${CERTIFICATE_NAME}-certificate`, ARN: CERTIFICATE_ARN }]);
    await createHandler({ secrets: () => secrets, responseSender: recorder.sender })(
        makeEvent("Create"),
        context,
    );

    const response = recorder.responses[0] as CfnResponse;
    assert.equal(response.status, "FAILED");
    assert.match(response.reason ?? "", new RegExp(`${CERTIFICATE_NAME}-private-key`));
    assert.match(response.reason ?? "", /deploy tool \(ideactl deploy\)/);
});

test("a missing ACM certificate fails naming the deploy tool, rather than importing one", async () => {
    const recorder = makeResponseRecorder();
    const { secrets } = makeSecrets(bothSecrets);
    await createHandler({ secrets: () => secrets, acm: () => noAcm, responseSender: recorder.sender })(
        makeEvent("Create", true),
        context,
    );

    const response = recorder.responses[0] as CfnResponse;
    assert.equal(response.status, "FAILED");
    assert.match(response.reason ?? "", /no ISSUED ACM certificate for domain sample-cluster\.idea\.default/);
    assert.match(response.reason ?? "", /deploy tool \(ideactl deploy\)/);
});

test("the module exports the Lambda entry point the stack names", () => {
    assert.equal(typeof handler, "function");
});
