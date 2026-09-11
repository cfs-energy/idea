import assert from "node:assert/strict";
import test from "node:test";
import { DeleteCommand, UpdateCommand } from "@aws-sdk/lib-dynamodb";
import type { CfnResponse, CfnResponseSender } from "../../src/lambda/commons/cfn-response.ts";
import type { ClusterSettingsDynamoDb, ClusterSettingsEvent } from "../../src/lambda/idea_custom_resource_update_cluster_settings/index.ts";
import { createHandler } from "../../src/lambda/idea_custom_resource_update_cluster_settings/index.ts";

// Error-path parity with the Python handler.
const context = { logStreamName: "synthetic-log-stream" };

function run(event: ClusterSettingsEvent): Promise<{
    responses: CfnResponse[];
    commands: Array<UpdateCommand | DeleteCommand>;
}> {
    const responses: CfnResponse[] = [];
    const commands: Array<UpdateCommand | DeleteCommand> = [];
    const sender: CfnResponseSender = async (response: CfnResponse): Promise<void> => {
        responses.push(response);
    };
    const dynamoDb: ClusterSettingsDynamoDb = {
        send: async (command: UpdateCommand | DeleteCommand): Promise<unknown> => {
            commands.push(command);
            return {};
        },
    };
    return createHandler({ dynamoDb: (): ClusterSettingsDynamoDb => dynamoDb, responseSender: sender })(event, context).then(() => ({
        responses,
        commands,
    }));
}

const base = {
    RequestType: "Update" as const,
    ResponseURL: "https://example.invalid/response",
    StackId: "synthetic-stack",
    RequestId: "synthetic-request",
    LogicalResourceId: "SyntheticSettings",
};
const props = { cluster_name: "sample-cluster", module_id: "sample-module", version: "1.2.3", settings: { a: "1" } };

test("a malformed event (no ResourceProperties) still answers FAILED with Python's None-None physical id", async () => {
    const { responses } = await run({ ...base, ResourceProperties: undefined } as unknown as ClusterSettingsEvent);
    assert.equal(responses.length, 1);
    assert.equal(responses[0].status, "FAILED");
    assert.equal(responses[0].physicalResourceId, "None-None-settings");
});

test("OldResourceProperties without a settings map is an empty delta, not a failure", async () => {
    const { responses, commands } = await run({
        ...base,
        ResourceProperties: props,
        OldResourceProperties: {
            cluster_name: "sample-cluster",
            module_id: "sample-module",
            version: "1.2.2",
        },
    } as unknown as ClusterSettingsEvent);
    assert.equal(responses[0].status, "SUCCESS");
    assert.equal(commands.length, 2);
    assert.ok(commands[0] instanceof UpdateCommand);
    assert.equal(commands[0].input.TableName, "sample-cluster.cluster-settings");
    assert.deepEqual(commands[0].input.Key, { key: "sample-module.a" });
    assert.ok(commands[1] instanceof UpdateCommand);
    assert.equal(commands[1].input.TableName, "sample-cluster.modules");
    assert.deepEqual(commands[1].input.Key, { module_id: "sample-module" });
    assert.equal(
        commands.filter((command) => command instanceof DeleteCommand).length,
        0,
    );
});

test("OldResourceProperties null is an empty delta, not a failure", async () => {
    const { responses } = await run({ ...base, ResourceProperties: props, OldResourceProperties: null } as unknown as ClusterSettingsEvent);
    assert.equal(responses[0].status, "SUCCESS");
});
