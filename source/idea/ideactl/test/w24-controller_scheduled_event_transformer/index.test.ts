import assert from "node:assert/strict";
import test from "node:test";
import type {
    ScheduledEventLogger,
    ScheduledEventSqs,
    SendMessageInput,
} from "../../src/lambda/idea_controller_scheduled_event_transformer/index.ts";
import {
    createHandler,
    handler,
} from "../../src/lambda/idea_controller_scheduled_event_transformer/index.ts";

interface LogError {
    message: string;
    error?: unknown;
}

/** Create a logger that records both Python info and exception branches. */
function makeLogger(): {
    logger: ScheduledEventLogger;
    infos: unknown[];
    errors: LogError[];
} {
    const infos: unknown[] = [];
    const errors: LogError[] = [];
    return {
        infos,
        errors,
        logger: {
            info: (message: unknown): void => {
                infos.push(message);
            },
            error: (message: string, error?: unknown): void => {
                errors.push({ message, error });
            },
        },
    };
}

test("exports the Lambda entry point as handler", () => {
    assert.equal(typeof handler, "function");
});

test("forwards a Scheduled Event to the controller FIFO queue", async () => {
    const inputs: SendMessageInput[] = [];
    const response = { MessageId: "synthetic-message" };
    const sqs: ScheduledEventSqs = {
        sendMessage: async (input: SendMessageInput): Promise<unknown> => {
            inputs.push(input);
            return response;
        },
    };
    const logs = makeLogger();

    await createHandler({
        sqs: (): ScheduledEventSqs => sqs,
        environment: {
            IDEA_CONTROLLER_EVENTS_QUEUE_URL:
                "https://example.invalid/controller-events.fifo",
        },
        logger: logs.logger,
    })(
        {
            "detail-type": "Scheduled Event",
            time: "2026-09-10T12:00:00Z",
        },
        {},
    );

    assert.deepEqual(inputs, [
        {
            QueueUrl: "https://example.invalid/controller-events.fifo",
            MessageBody:
                "{\"event_group_id\": \"SCHEDULED_EVENT\", \"event_type\": \"SCHEDULED_EVENT\", \"detail\": {\"time\": \"2026-09-10T12:00:00Z\"}}",
            MessageGroupId: "SCHEDULED_EVENT",
        },
    ]);
    assert.deepEqual(logs.infos, [
        "Forwarding scheduled event to Controller",
        response,
    ]);
    assert.deepEqual(logs.errors, []);
});

test("uses Python json.dumps ASCII escaping in the forwarded body", async () => {
    const inputs: SendMessageInput[] = [];
    const sqs: ScheduledEventSqs = {
        sendMessage: async (input: SendMessageInput): Promise<unknown> => {
            inputs.push(input);
            return {};
        },
    };

    await createHandler({
        sqs: (): ScheduledEventSqs => sqs,
        environment: {
            IDEA_CONTROLLER_EVENTS_QUEUE_URL:
                "https://example.invalid/controller-events.fifo",
        },
        logger: makeLogger().logger,
    })({ "detail-type": "Scheduled Event", time: "é😀" }, {});

    assert.equal(
        inputs[0]?.MessageBody,
        "{\"event_group_id\": \"SCHEDULED_EVENT\", \"event_type\": \"SCHEDULED_EVENT\", \"detail\": {\"time\": \"\\u00e9\\ud83d\\ude00\"}}",
    );
});

test("ignores events whose detail-type is not Scheduled Event", async () => {
    let sqsFactoryCalls = 0;
    const logs = makeLogger();

    await createHandler({
        sqs: (): ScheduledEventSqs => {
            sqsFactoryCalls += 1;
            throw new Error("SQS must not be created");
        },
        logger: logs.logger,
    })({ "detail-type": "Instance State-change Notification" }, {});

    assert.equal(sqsFactoryCalls, 0);
    assert.deepEqual(logs.infos, []);
    assert.deepEqual(logs.errors, []);
});

test("logs and swallows an event missing detail-type", async () => {
    let sqsFactoryCalls = 0;
    const logs = makeLogger();

    await createHandler({
        sqs: (): ScheduledEventSqs => {
            sqsFactoryCalls += 1;
            throw new Error("SQS must not be created");
        },
        logger: logs.logger,
    })({ time: "2026-09-10T12:00:00Z" }, {});

    assert.equal(sqsFactoryCalls, 0);
    assert.equal(logs.errors.length, 1);
    assert.match(logs.errors[0]?.message ?? "", /missing event key: detail-type/);
});

test("logs and swallows a Scheduled Event missing time", async () => {
    let sqsFactoryCalls = 0;
    const logs = makeLogger();

    await createHandler({
        sqs: (): ScheduledEventSqs => {
            sqsFactoryCalls += 1;
            throw new Error("SQS must not be created");
        },
        logger: logs.logger,
    })({ "detail-type": "Scheduled Event" }, {});

    assert.equal(sqsFactoryCalls, 0);
    assert.equal(logs.errors.length, 1);
    assert.match(logs.errors[0]?.message ?? "", /missing event key: time/);
});

test("passes an absent queue URL to SQS and swallows its validation error", async () => {
    const inputs: SendMessageInput[] = [];
    const logs = makeLogger();
    const sqs: ScheduledEventSqs = {
        sendMessage: async (input: SendMessageInput): Promise<unknown> => {
            inputs.push(input);
            throw new Error("QueueUrl is required");
        },
    };

    await createHandler({
        sqs: (): ScheduledEventSqs => sqs,
        environment: {},
        logger: logs.logger,
    })(
        {
            "detail-type": "Scheduled Event",
            time: "2026-09-10T12:00:00Z",
        },
        {},
    );

    assert.equal(inputs.length, 1);
    assert.deepEqual(inputs[0], {
        QueueUrl: undefined,
        MessageBody:
            "{\"event_group_id\": \"SCHEDULED_EVENT\", \"event_type\": \"SCHEDULED_EVENT\", \"detail\": {\"time\": \"2026-09-10T12:00:00Z\"}}",
        MessageGroupId: "SCHEDULED_EVENT",
    });
    assert.equal(logs.errors.length, 1);
    assert.match(logs.errors[0]?.message ?? "", /QueueUrl is required/);
});

test("logs and swallows an SQS send failure", async () => {
    const logs = makeLogger();
    const sqs: ScheduledEventSqs = {
        sendMessage: async (): Promise<unknown> => {
            throw new Error("synthetic SQS failure");
        },
    };

    await assert.doesNotReject(
        createHandler({
            sqs: (): ScheduledEventSqs => sqs,
            environment: {
                IDEA_CONTROLLER_EVENTS_QUEUE_URL:
                    "https://example.invalid/controller-events.fifo",
            },
            logger: logs.logger,
        })(
            {
                "detail-type": "Scheduled Event",
                time: "2026-09-10T12:00:00Z",
            },
            {},
        ),
    );

    assert.equal(logs.errors.length, 1);
    assert.match(logs.errors[0]?.message ?? "", /synthetic SQS failure/);
});
