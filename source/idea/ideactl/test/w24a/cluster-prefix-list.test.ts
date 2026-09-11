import assert from "node:assert/strict";
import test from "node:test";
import type {
    CfnResponse,
    CfnResponseSender,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    PrefixListEc2,
    PrefixListEvent,
} from "../../src/lambda/idea_custom_resource_update_cluster_prefix_list/index.ts";
import { createHandler } from "../../src/lambda/idea_custom_resource_update_cluster_prefix_list/index.ts";

/** Python PHYSICAL_RESOURCE_ID. CloudFormation replacement turns on this string. */
const PYTHON_PHYSICAL_RESOURCE_ID = "cluster-prefix-list";

const context = { logStreamName: "synthetic-log-stream" };

function makeEvent(requestType: PrefixListEvent["RequestType"]): PrefixListEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticPrefixList",
        ResourceProperties: {
            prefix_list_id: "pl-synthetic",
            add_entries: [
                { Cidr: "192.0.2.0/24", Description: "existing" },
                { Cidr: "198.51.100.0/24", Description: "new" },
            ],
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

test("Create paginates existing CIDRs and adds only new entries at the current version", async () => {
    const getInputs: Array<
        Parameters<PrefixListEc2["getManagedPrefixListEntries"]>[0]
    > = [];
    const describeInputs: Array<
        Parameters<PrefixListEc2["describeManagedPrefixLists"]>[0]
    > = [];
    const modifyInputs: Array<
        Parameters<PrefixListEc2["modifyManagedPrefixList"]>[0]
    > = [];
    let pageNumber = 0;
    const ec2: PrefixListEc2 = {
        getManagedPrefixListEntries: async (input) => {
            getInputs.push(input);
            pageNumber += 1;
            return pageNumber === 1
                ? {
                      $metadata: {},
                      Entries: [],
                      NextToken: "synthetic-next-token",
                  }
                : {
                      $metadata: {},
                      Entries: [{ Cidr: "192.0.2.0/24", Description: "existing" }],
                  };
        },
        describeManagedPrefixLists: async (input) => {
            describeInputs.push(input);
            return {
                $metadata: {},
                PrefixLists: [{ PrefixListId: "pl-synthetic", Version: 7 }],
            };
        },
        modifyManagedPrefixList: async (input) => {
            modifyInputs.push(input);
            return { $metadata: {} };
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): PrefixListEc2 => ec2,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.deepEqual(getInputs, [
        { PrefixListId: "pl-synthetic" },
        {
            PrefixListId: "pl-synthetic",
            NextToken: "synthetic-next-token",
        },
    ]);
    assert.deepEqual(describeInputs, [
        { PrefixListIds: ["pl-synthetic"] },
    ]);
    assert.deepEqual(modifyInputs, [
        {
            AddEntries: [
                { Cidr: "198.51.100.0/24", Description: "new" },
            ],
            PrefixListId: "pl-synthetic",
            CurrentVersion: 7,
        },
    ]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
});

test("Update skips describe and modify when every requested CIDR exists", async () => {
    let describeCalls = 0;
    let modifyCalls = 0;
    const ec2: PrefixListEc2 = {
        getManagedPrefixListEntries: async () => ({
            $metadata: {},
            Entries: [
                { Cidr: "192.0.2.0/24" },
                { Cidr: "198.51.100.0/24" },
            ],
        }),
        describeManagedPrefixLists: async () => {
            describeCalls += 1;
            return { $metadata: {}, PrefixLists: [] };
        },
        modifyManagedPrefixList: async () => {
            modifyCalls += 1;
            return { $metadata: {} };
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): PrefixListEc2 => ec2,
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(describeCalls, 0);
    assert.equal(modifyCalls, 0);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("Delete succeeds without constructing an EC2 client", async () => {
    let ec2FactoryCalls = 0;
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): PrefixListEc2 => {
            ec2FactoryCalls += 1;
            throw new Error("EC2 must not be created for Delete");
        },
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.equal(ec2FactoryCalls, 0);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
});

test("reports FAILED with the Python reason when an EC2 operation fails", async () => {
    const ec2: PrefixListEc2 = {
        getManagedPrefixListEntries: async () => {
            throw new Error("synthetic EC2 failure");
        },
        describeManagedPrefixLists: async () => ({
            $metadata: {},
            PrefixLists: [],
        }),
        modifyManagedPrefixList: async () => ({ $metadata: {} }),
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): PrefixListEc2 => ec2,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses.length, 1);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        PYTHON_PHYSICAL_RESOURCE_ID,
    );
    assert.equal(
        recorder.responses[0].reason,
        "failed to update cluster prefix list: synthetic EC2 failure",
    );
});
