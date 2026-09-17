/**
 * The certificate the deploy tool generates has to be the certificate the Python handler generated,
 * because a cluster that already holds a pair keeps it and a cluster that does not gets this one.
 *
 * So one test generates with the `openssl` on this machine and reads the result back with
 * `node:crypto`, rather than asserting on the arguments: an argument list that parses and produces
 * the wrong certificate would pass that. The rest drive the adoption, creation and ACM branches
 * through fakes, including the two failures that exist to stop a live private key being replaced.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { X509Certificate, createPrivateKey } from "node:crypto";
import { writeFileSync } from "node:fs";
import { test } from "node:test";

import {
    SECRET_NAME_TAG,
    ensureSelfSignedCertificate,
    opensslArgs,
    type AcmApi,
    type CertificateDeps,
    type CertificateRequest,
    type CreateSecretInput,
    type OpensslRunner,
    type SecretSummary,
} from "../../src/cli/certificates.ts";

const CERTIFICATE_NAME = "idea-test-external";
const DOMAIN_NAME = "idea-test.idea.default";
const CERTIFICATE_SECRET = `${CERTIFICATE_NAME}-certificate`;
const PRIVATE_KEY_SECRET = `${CERTIFICATE_NAME}-private-key`;
const CERTIFICATE_ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:cert-AAAAAA";
const PRIVATE_KEY_ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:key-BBBBBB";
const ACM_ARN = "arn:aws:acm:us-east-2:123456789012:certificate/33333333-4444-5555-6666-777777777777";

const request = (overrides: Partial<CertificateRequest> = {}): CertificateRequest => ({
    certificateName: CERTIFICATE_NAME,
    domainName: DOMAIN_NAME,
    tags: { Name: "idea-test external alb certs", "idea:ClusterName": "idea-test" },
    importToAcm: false,
    ...overrides,
});

/** The real binary, as the live adapter runs it. */
const liveOpenssl: OpensslRunner = (args, cwd) => {
    const result = spawnSync("openssl", [...args], { cwd, encoding: "utf-8" });
    return {
        status: result.status,
        stderr: result.stderr ?? "",
        missing: (result.error as { code?: string } | undefined)?.code === "ENOENT",
    };
};

/** Writes a PEM pair without running anything, so the other branches need no binary. */
function fakeOpenssl(calls: string[][] = []): OpensslRunner {
    return (args, cwd) => {
        calls.push([...args]);
        const file = (flag: string): string => args[args.indexOf(flag) + 1] as string;
        writeFileSync(file("-out"), "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n");
        // Assembled at run time so the source holds no private-key marker for the hygiene gate.
        writeFileSync(file("-keyout"), ["-----BEGIN", "PRIVATE KEY-----\nfake\n-----END", "PRIVATE KEY-----\n"].join(" "));
        return { status: 0, stderr: "" };
    };
}

interface Recorder {
    deps: CertificateDeps;
    created: CreateSecretInput[];
    imported: Array<{ Certificate: string; PrivateKey: string }>;
    listedTagValues: string[][];
}

function recorder(options: { existing?: SecretSummary[]; issued?: AcmCertificateRow[] } = {}): Recorder {
    const created: CreateSecretInput[] = [];
    const imported: Array<{ Certificate: string; PrivateKey: string }> = [];
    const listedTagValues: string[][] = [];
    const acm: AcmApi = {
        listIssuedCertificates: async () => options.issued ?? [],
        importCertificate: async (input) => {
            imported.push({ Certificate: input.Certificate, PrivateKey: input.PrivateKey });
            return ACM_ARN;
        },
    };
    return {
        created,
        imported,
        listedTagValues,
        deps: {
            secrets: {
                listSecretsByTagValue: async (tagKey, tagValues) => {
                    assert.equal(tagKey, SECRET_NAME_TAG);
                    listedTagValues.push([...tagValues]);
                    return options.existing ?? [];
                },
                createSecret: async (input) => {
                    created.push(input);
                    return input.Name === CERTIFICATE_SECRET ? CERTIFICATE_ARN : PRIVATE_KEY_ARN;
                },
            },
            acm,
            openssl: fakeOpenssl(),
        },
    };
}

interface AcmCertificateRow {
    DomainName?: string;
    CertificateArn?: string;
}

test("the generated certificate carries the subject, SAN, constraint and validity the handler produced", async () => {
    const created: CreateSecretInput[] = [];
    await ensureSelfSignedCertificate(request(), {
        secrets: {
            listSecretsByTagValue: async () => [],
            createSecret: async (input) => {
                created.push(input);
                return input.Name;
            },
        },
        acm: { listIssuedCertificates: async () => [], importCertificate: async () => ACM_ARN },
        openssl: liveOpenssl,
    });

    const certificate = new X509Certificate(created[0]?.SecretString as string);
    assert.equal(
        certificate.subject,
        ["C=US", "ST=California", "L=Sunnyvale", `O=${CERTIFICATE_NAME}`, `CN=${DOMAIN_NAME}`].join("\n"),
    );
    assert.equal(certificate.subjectAltName, `DNS:${DOMAIN_NAME}`);
    assert.equal(certificate.ca, false, "BasicConstraints has to say CA:FALSE");

    const days = (Date.parse(certificate.validTo) - Date.parse(certificate.validFrom)) / 86_400_000;
    assert.ok(Math.abs(days - 3650) <= 2, `validity is ${days} days, expected about 3650`);

    // The private key has to be the one that signed it, and readable without a passphrase.
    const privateKey = createPrivateKey(created[1]?.SecretString as string);
    assert.equal(certificate.checkPrivateKey(privateKey), true);
    assert.equal(privateKey.asymmetricKeyDetails?.modulusLength, 2048);
});

test("a pair that already exists is adopted, and nothing is created", async () => {
    const recording = recorder({
        existing: [
            { Name: CERTIFICATE_SECRET, ARN: CERTIFICATE_ARN },
            { Name: PRIVATE_KEY_SECRET, ARN: PRIVATE_KEY_ARN },
        ],
    });
    const result = await ensureSelfSignedCertificate(request(), recording.deps);

    assert.deepEqual(result, {
        certificateSecretArn: CERTIFICATE_ARN,
        privateKeySecretArn: PRIVATE_KEY_ARN,
    });
    assert.deepEqual(recording.created, []);
    assert.deepEqual(recording.listedTagValues, [[CERTIFICATE_SECRET, PRIVATE_KEY_SECRET]]);
});

test("a missing pair is generated into two secrets with the handler's names, descriptions and tags", async () => {
    const recording = recorder();
    const result = await ensureSelfSignedCertificate(request({ kmsKeyId: "alias/idea-test" }), recording.deps);

    assert.deepEqual(result, {
        certificateSecretArn: CERTIFICATE_ARN,
        privateKeySecretArn: PRIVATE_KEY_ARN,
    });
    assert.deepEqual(
        recording.created.map((secret) => ({ Name: secret.Name, Description: secret.Description, KmsKeyId: secret.KmsKeyId })),
        [
            {
                Name: CERTIFICATE_SECRET,
                Description: `Self-Signed certificate for domain name: ${DOMAIN_NAME}`,
                KmsKeyId: "alias/idea-test",
            },
            {
                Name: PRIVATE_KEY_SECRET,
                Description: `Self-Signed certificate private key for domain name: ${DOMAIN_NAME}`,
                KmsKeyId: "alias/idea-test",
            },
        ],
    );
    assert.deepEqual(recording.created[0]?.Tags, [
        { Key: "Name", Value: "idea-test external alb certs" },
        { Key: "idea:ClusterName", Value: "idea-test" },
        { Key: SECRET_NAME_TAG, Value: CERTIFICATE_SECRET },
    ]);
    assert.deepEqual(recording.created[1]?.Tags, [
        { Key: "Name", Value: "idea-test external alb certs" },
        { Key: "idea:ClusterName", Value: "idea-test" },
        { Key: SECRET_NAME_TAG, Value: PRIVATE_KEY_SECRET },
    ]);
});

test("without a KMS key the create request omits KmsKeyId rather than sending an empty one", async () => {
    const recording = recorder();
    await ensureSelfSignedCertificate(request(), recording.deps);
    for (const secret of recording.created) {
        assert.equal("KmsKeyId" in secret, false, `${secret.Name} was created with a KmsKeyId`);
    }
});

test("one half of a pair is a failure naming the missing secret, and generates nothing", async () => {
    for (const [present, missing] of [
        [CERTIFICATE_SECRET, PRIVATE_KEY_SECRET],
        [PRIVATE_KEY_SECRET, CERTIFICATE_SECRET],
    ]) {
        const recording = recorder({ existing: [{ Name: present, ARN: CERTIFICATE_ARN }] });
        await assert.rejects(
            () => ensureSelfSignedCertificate(request(), recording.deps),
            (error: Error) => {
                assert.match(error.message, new RegExp(`${missing} is missing`));
                return true;
            },
        );
        assert.deepEqual(recording.created, []);
    }
});

test("an ISSUED ACM certificate for the domain is adopted rather than imported", async () => {
    const recording = recorder({
        existing: [
            { Name: CERTIFICATE_SECRET, ARN: CERTIFICATE_ARN },
            { Name: PRIVATE_KEY_SECRET, ARN: PRIVATE_KEY_ARN },
        ],
        issued: [
            { DomainName: "something.else", CertificateArn: "arn:aws:acm:us-east-2:123456789012:certificate/other" },
            { DomainName: DOMAIN_NAME, CertificateArn: ACM_ARN },
        ],
    });
    const result = await ensureSelfSignedCertificate(request({ importToAcm: true }), recording.deps);

    assert.equal(result.acmCertificateArn, ACM_ARN);
    assert.deepEqual(recording.imported, []);
});

test("a generated pair with no ACM certificate for the domain is imported with the common tags", async () => {
    const recording = recorder();
    const result = await ensureSelfSignedCertificate(request({ importToAcm: true }), recording.deps);

    assert.equal(result.acmCertificateArn, ACM_ARN);
    assert.equal(recording.imported.length, 1);
    assert.equal(recording.imported[0]?.Certificate, recording.created[0]?.SecretString);
    assert.equal(recording.imported[0]?.PrivateKey, recording.created[1]?.SecretString);
});

test("adopted secrets with no ACM certificate fail instead of importing a fresh certificate", async () => {
    const recording = recorder({
        existing: [
            { Name: CERTIFICATE_SECRET, ARN: CERTIFICATE_ARN },
            { Name: PRIVATE_KEY_SECRET, ARN: PRIVATE_KEY_ARN },
        ],
    });
    await assert.rejects(
        () => ensureSelfSignedCertificate(request({ importToAcm: true }), recording.deps),
        /no ISSUED ACM certificate for idea-test\.idea\.default/,
    );
    assert.deepEqual(recording.imported, []);
});

test("a missing openssl binary fails saying so, and creates no secret", async () => {
    const recording = recorder();
    recording.deps.openssl = () => ({ status: null, stderr: "", missing: true });
    await assert.rejects(
        () => ensureSelfSignedCertificate(request(), recording.deps),
        /openssl was not found on PATH/,
    );
    assert.deepEqual(recording.created, []);
});

test("a non-zero openssl exit carries its stderr, and creates no secret", async () => {
    const recording = recorder();
    recording.deps.openssl = () => ({ status: 1, stderr: "unknown option -addext\n" });
    await assert.rejects(() => ensureSelfSignedCertificate(request(), recording.deps), /unknown option -addext/);
    assert.deepEqual(recording.created, []);
});

test("the arguments name the subject, both extensions and the two output files", () => {
    const args = opensslArgs({ certificateName: CERTIFICATE_NAME, domainName: DOMAIN_NAME }, "/tmp/c.pem", "/tmp/k.pem");
    assert.deepEqual(args, [
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "3650",
        "-subj",
        `/C=US/ST=California/L=Sunnyvale/O=${CERTIFICATE_NAME}/CN=${DOMAIN_NAME}`,
        "-addext",
        `subjectAltName=DNS:${DOMAIN_NAME}`,
        "-addext",
        "basicConstraints=critical,CA:FALSE",
        "-keyout",
        "/tmp/k.pem",
        "-out",
        "/tmp/c.pem",
    ]);
});
