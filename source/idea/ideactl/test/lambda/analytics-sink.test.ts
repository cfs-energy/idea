/**
 * The analytics sink turns one Kinesis batch into one OpenSearch `_bulk` request.
 *
 * The three actions render different lines, the batch is ordered by the record timestamp rather
 * than by delivery order, and a failed request is logged and swallowed: throwing would make the
 * stream event source retry the batch and block the shard behind it.
 */

import assert from "node:assert/strict";
import test from "node:test";

import type {
    AnalyticsSinkEvent,
    AnalyticsSinkLogger,
    BulkResponse,
} from "../../src/lambda/idea_analytics_sink/index.ts";
import { bulkUrl, createHandler } from "../../src/lambda/idea_analytics_sink/index.ts";

const ENDPOINT = "vpc-idea-analytics.us-east-2.es.amazonaws.com";

interface SinkEntry {
    document_id: string;
    index_id: string;
    action: string;
    entry?: unknown;
    timestamp: number | string;
}

/** One Kinesis record carrying a base64 payload, the way the stream delivers it. */
function record(entry: SinkEntry): AnalyticsSinkEvent["Records"][number] {
    return { kinesis: { data: Buffer.from(JSON.stringify(entry), "utf8").toString("base64") } };
}

function makeLogger(): { logger: AnalyticsSinkLogger; infos: string[]; errors: string[] } {
    const infos: string[] = [];
    const errors: string[] = [];
    return {
        infos,
        errors,
        logger: {
            info: (message: string): void => {
                infos.push(message);
            },
            error: (message: string): void => {
                errors.push(message);
            },
        },
    };
}

/** Run the handler over one batch and return what the request function was handed. */
async function run(
    entries: SinkEntry[],
    response: BulkResponse | Error = { status: 200, body: '{"took":3,"errors":false,"items":[]}' },
): Promise<{ calls: Array<{ url: string; body: string }>; infos: string[]; errors: string[] }> {
    const calls: Array<{ url: string; body: string }> = [];
    const logs = makeLogger();
    await createHandler({
        endpoint: ENDPOINT,
        logger: logs.logger,
        request: async (input): Promise<BulkResponse> => {
            calls.push(input);
            if (response instanceof Error) throw response;
            return response;
        },
    })({ Records: entries.map(record) });
    return { calls, infos: logs.infos, errors: logs.errors };
}

test("the three actions render their exact bulk lines", async () => {
    const { calls } = await run([
        {
            document_id: "job-1",
            index_id: "idea-jobs",
            action: "CREATE_ENTRY",
            entry: { job_id: "job-1", state: "queued" },
            timestamp: 1,
        },
        {
            document_id: "job-2",
            index_id: "idea-jobs",
            action: "UPDATE_ENTRY",
            entry: { state: "running" },
            timestamp: 2,
        },
        {
            document_id: "job-3",
            index_id: "idea-jobs",
            action: "DELETE_ENTRY",
            timestamp: 3,
        },
    ]);

    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.url, `https://${ENDPOINT}/_bulk`);
    assert.deepEqual(calls[0]?.body.split("\n"), [
        '{"create":{"_index":"idea-jobs","_id":"job-1"}}',
        '{"job_id":"job-1","state":"queued"}',
        '{"update":{"_index":"idea-jobs","_id":"job-2"}}',
        '{"doc":{"state":"running"}}',
        '{"delete":{"_index":"idea-jobs","_id":"job-3"}}',
        "",
    ]);
});

test("the batch is ordered by the record timestamp, not by delivery order", async () => {
    const { calls } = await run([
        { document_id: "c", index_id: "i", action: "DELETE_ENTRY", timestamp: 30 },
        { document_id: "a", index_id: "i", action: "DELETE_ENTRY", timestamp: 10 },
        { document_id: "b", index_id: "i", action: "DELETE_ENTRY", timestamp: 20 },
    ]);

    assert.deepEqual(
        (calls[0]?.body ?? "").trim().split("\n"),
        [
            '{"delete":{"_index":"i","_id":"a"}}',
            '{"delete":{"_index":"i","_id":"b"}}',
            '{"delete":{"_index":"i","_id":"c"}}',
        ],
    );
});

test("records sharing a timestamp keep the order the stream delivered them in", async () => {
    const { calls } = await run([
        { document_id: "first", index_id: "i", action: "DELETE_ENTRY", timestamp: 7 },
        { document_id: "second", index_id: "i", action: "DELETE_ENTRY", timestamp: 7 },
    ]);

    assert.deepEqual(
        (calls[0]?.body ?? "").trim().split("\n"),
        ['{"delete":{"_index":"i","_id":"first"}}', '{"delete":{"_index":"i","_id":"second"}}'],
    );
});

test("the response summary is logged", async () => {
    const { infos, errors } = await run(
        [{ document_id: "a", index_id: "i", action: "DELETE_ENTRY", timestamp: 1 }],
        { status: 200, body: '{"took":3,"errors":true,"items":[{"delete":{"status":404}}]}' },
    );

    assert.deepEqual(errors, []);
    assert.equal(infos.length, 1);
    assert.match(infos[0] ?? "", /status 200/);
    assert.match(infos[0] ?? "", /items 1/);
    assert.match(infos[0] ?? "", /errors true/);
});

test("a failed request is logged and swallowed so the shard is not blocked", async () => {
    const { infos, errors } = await run(
        [{ document_id: "a", index_id: "i", action: "DELETE_ENTRY", timestamp: 1 }],
        new Error("synthetic bulk failure"),
    );

    assert.deepEqual(infos, []);
    assert.equal(errors.length, 1);
    assert.match(errors[0] ?? "", /synthetic bulk failure/);
});

test("an undecodable record is logged and swallowed, and nothing is posted", async () => {
    const calls: Array<{ url: string; body: string }> = [];
    const logs = makeLogger();

    await createHandler({
        endpoint: ENDPOINT,
        logger: logs.logger,
        request: async (input): Promise<BulkResponse> => {
            calls.push(input);
            return { status: 200, body: "{}" };
        },
    })({ Records: [{ kinesis: { data: Buffer.from("not json", "utf8").toString("base64") } }] });

    assert.deepEqual(calls, []);
    assert.equal(logs.errors.length, 1);
});

test("an empty batch posts nothing", async () => {
    const { calls, errors } = await run([]);
    assert.deepEqual(calls, []);
    assert.deepEqual(errors, []);
});

test("an endpoint that already carries a scheme is not given a second one", () => {
    assert.equal(bulkUrl(ENDPOINT), `https://${ENDPOINT}/_bulk`);
    assert.equal(bulkUrl(`https://${ENDPOINT}`), `https://${ENDPOINT}/_bulk`);
});
