import assert from "node:assert/strict";
import test from "node:test";
import { DeleteCommand, UpdateCommand } from "@aws-sdk/lib-dynamodb";
import type {
    CfnResponse,
    CfnResponseSender,
    JsonValue,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    ClusterSettingsDynamoDb,
    ClusterSettingsEvent,
} from "../../src/lambda/idea_custom_resource_update_cluster_settings/index.ts";
import { createHandler } from "../../src/lambda/idea_custom_resource_update_cluster_settings/index.ts";

const context = { logStreamName: "synthetic-log-stream" };

function makeEvent(
    requestType: ClusterSettingsEvent["RequestType"],
    settings: Record<string, JsonValue>,
    oldSettings?: Record<string, JsonValue>,
): ClusterSettingsEvent {
    const resourceProperties = {
        cluster_name: "sample-cluster",
        module_id: "sample-module",
        version: "1.2.3",
        settings,
    };
    const baseEvent: ClusterSettingsEvent = {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticSettings",
        ResourceProperties: resourceProperties,
    };

    if (oldSettings === undefined) {
        return baseEvent;
    }
    return {
        ...baseEvent,
        OldResourceProperties: {
            ...resourceProperties,
            settings: oldSettings,
        },
    };
}

function makeResponseRecorder(): {
    responses: CfnResponse[];
    sender: CfnResponseSender;
} {
    const responses: CfnResponse[] = [];
    return {
        responses,
        sender: async (response: CfnResponse): Promise<void> => {
            responses.push(response);
        },
    };
}

test("Create writes every setting with its exact JSON type and marks the module deployed", async () => {
    const commands: Array<UpdateCommand | DeleteCommand> = [];
    const dynamoDb: ClusterSettingsDynamoDb = {
        send: async (command: UpdateCommand | DeleteCommand): Promise<unknown> => {
            commands.push(command);
            return {};
        },
    };
    const recorder = makeResponseRecorder();
    const settings: Record<string, JsonValue> = {
        text: "value",
        count: 3,
        enabled: true,
        empty: null,
        list: ["a", 2],
        object: { nested: "value" },
    };

    await createHandler({
        dynamoDb: (): ClusterSettingsDynamoDb => dynamoDb,
        responseSender: recorder.sender,
    })(makeEvent("Create", settings), context);

    assert.equal(commands.length, Object.keys(settings).length + 1);
    const writtenValues = new Map<string, JsonValue>();
    for (const command of commands.slice(0, -1)) {
        assert.ok(command instanceof UpdateCommand);
        assert.equal(
            command.input.TableName,
            "sample-cluster.cluster-settings",
        );
        assert.equal(
            command.input.UpdateExpression,
            "SET #value=:value, #source=:source ADD #version :version",
        );
        const key = command.input.Key?.key;
        assert.equal(typeof key, "string");
        const value = command.input.ExpressionAttributeValues?.[":value"];
        assert.notEqual(value, undefined);
        writtenValues.set(key, value);
        assert.equal(command.input.ExpressionAttributeValues?.[":source"], "stack");
        assert.equal(command.input.ExpressionAttributeValues?.[":version"], 1);
    }
    assert.deepEqual(
        Object.fromEntries(writtenValues),
        Object.fromEntries(
            Object.entries(settings).map(([key, value]) => [
                `sample-module.${key}`,
                value,
            ]),
        ),
    );

    const moduleCommand = commands.at(-1);
    assert.ok(moduleCommand instanceof UpdateCommand);
    assert.equal(moduleCommand.input.TableName, "sample-cluster.modules");
    assert.deepEqual(moduleCommand.input.Key, { module_id: "sample-module" });
    assert.equal(
        moduleCommand.input.UpdateExpression,
        "SET #status=:status, #stack_name=:stack_name, #version=:version",
    );
    assert.deepEqual(moduleCommand.input.ExpressionAttributeValues, {
        ":status": "deployed",
        ":stack_name": "sample-cluster-sample-module",
        ":version": "1.2.3",
    });
    assert.equal(recorder.responses.length, 1);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        "sample-cluster-sample-module-settings",
    );
});

test("Update writes added and changed values and deletes removed old keys", async () => {
    const commands: Array<UpdateCommand | DeleteCommand> = [];
    const dynamoDb: ClusterSettingsDynamoDb = {
        send: async (command: UpdateCommand | DeleteCommand): Promise<unknown> => {
            commands.push(command);
            return {};
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        dynamoDb: (): ClusterSettingsDynamoDb => dynamoDb,
        responseSender: recorder.sender,
    })(
        makeEvent(
            "Update",
            { changed: 42, added: "new" },
            { changed: 1, removed: "old" },
        ),
        context,
    );

    assert.equal(commands.length, 4);
    const changedCommand = commands[0];
    const addedCommand = commands[1];
    const moduleCommand = commands[2];
    const removedCommand = commands[3];
    assert.ok(changedCommand instanceof UpdateCommand);
    assert.ok(addedCommand instanceof UpdateCommand);
    assert.ok(moduleCommand instanceof UpdateCommand);
    assert.ok(removedCommand instanceof DeleteCommand);
    assert.equal(changedCommand.input.TableName, "sample-cluster.cluster-settings");
    assert.equal(changedCommand.input.Key?.key, "sample-module.changed");
    assert.equal(changedCommand.input.ExpressionAttributeValues?.[":value"], 42);
    assert.equal(addedCommand.input.TableName, "sample-cluster.cluster-settings");
    assert.equal(addedCommand.input.Key?.key, "sample-module.added");
    assert.equal(addedCommand.input.ExpressionAttributeValues?.[":value"], "new");
    assert.equal(removedCommand.input.TableName, "sample-cluster.cluster-settings");
    assert.deepEqual(removedCommand.input.Key, { key: "sample-module.removed" });
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("Delete removes every current setting and marks the module not deployed", async () => {
    const commands: Array<UpdateCommand | DeleteCommand> = [];
    const dynamoDb: ClusterSettingsDynamoDb = {
        send: async (command: UpdateCommand | DeleteCommand): Promise<unknown> => {
            commands.push(command);
            return {};
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        dynamoDb: (): ClusterSettingsDynamoDb => dynamoDb,
        responseSender: recorder.sender,
    })(makeEvent("Delete", { first: "a", second: 2 }), context);

    assert.equal(commands.length, 3);
    assert.ok(commands[0] instanceof DeleteCommand);
    assert.equal(commands[0].input.TableName, "sample-cluster.cluster-settings");
    assert.deepEqual(commands[0].input.Key, { key: "sample-module.first" });
    assert.ok(commands[1] instanceof DeleteCommand);
    assert.equal(commands[1].input.TableName, "sample-cluster.cluster-settings");
    assert.deepEqual(commands[1].input.Key, { key: "sample-module.second" });
    assert.ok(commands[2] instanceof UpdateCommand);
    assert.equal(commands[2].input.TableName, "sample-cluster.modules");
    assert.deepEqual(commands[2].input.Key, { module_id: "sample-module" });
    assert.equal(
        commands[2].input.UpdateExpression,
        "SET #status=:status, #stack_name=:stack_name, #version=:version",
    );
    assert.deepEqual(commands[2].input.ExpressionAttributeValues, {
        ":status": "not-deployed",
        ":stack_name": null,
        ":version": null,
    });
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("reports FAILED with the stable physical id when DynamoDB rejects a write", async () => {
    const dynamoDb: ClusterSettingsDynamoDb = {
        send: async (): Promise<unknown> => {
            throw new Error("synthetic DynamoDB failure");
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        dynamoDb: (): ClusterSettingsDynamoDb => dynamoDb,
        responseSender: recorder.sender,
    })(makeEvent("Create", { key: "value" }), context);

    assert.equal(recorder.responses.length, 1);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        "sample-cluster-sample-module-settings",
    );
    assert.equal(recorder.responses[0].reason, undefined);
});
