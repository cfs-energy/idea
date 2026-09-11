import {
    DescribeManagedPrefixListsCommand,
    EC2Client,
    GetManagedPrefixListEntriesCommand,
    ModifyManagedPrefixListCommand,
} from "@aws-sdk/client-ec2";
import type {
    DescribeManagedPrefixListsCommandInput,
    DescribeManagedPrefixListsCommandOutput,
    GetManagedPrefixListEntriesCommandInput,
    GetManagedPrefixListEntriesCommandOutput,
    ModifyManagedPrefixListCommandInput,
    ModifyManagedPrefixListCommandOutput,
} from "@aws-sdk/client-ec2";
import type {
    CfnLogger,
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import {
    errorMessage,
    wrapCfnHandler,
} from "../commons/cfn-response.ts";

export const PHYSICAL_RESOURCE_ID = "cluster-prefix-list";

type PrefixListEntry = Record<string, JsonValue> & {
    Cidr: string;
    Description: string;
};

type PrefixListProperties = Record<string, JsonValue> & {
    prefix_list_id: string;
    add_entries: PrefixListEntry[];
};

export type PrefixListEvent = CfnResourceEvent<PrefixListProperties>;

/** Injectable EC2 operations used by the prefix-list handler. */
export interface PrefixListEc2 {
    getManagedPrefixListEntries(
        input: GetManagedPrefixListEntriesCommandInput,
    ): Promise<GetManagedPrefixListEntriesCommandOutput>;
    describeManagedPrefixLists(
        input: DescribeManagedPrefixListsCommandInput,
    ): Promise<DescribeManagedPrefixListsCommandOutput>;
    modifyManagedPrefixList(
        input: ModifyManagedPrefixListCommandInput,
    ): Promise<ModifyManagedPrefixListCommandOutput>;
}

/** Injectable collaborators used by unit tests and the production handler. */
export interface PrefixListHandlerDependencies {
    ec2?: () => PrefixListEc2;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void => console.error(message, error),
};

/** Create the real EC2 command adapter lazily for non-Delete requests. */
function createEc2(): PrefixListEc2 {
    const client = new EC2Client({});
    return {
        getManagedPrefixListEntries: (
            input: GetManagedPrefixListEntriesCommandInput,
        ): Promise<GetManagedPrefixListEntriesCommandOutput> =>
            client.send(new GetManagedPrefixListEntriesCommand(input)),
        describeManagedPrefixLists: (
            input: DescribeManagedPrefixListsCommandInput,
        ): Promise<DescribeManagedPrefixListsCommandOutput> =>
            client.send(new DescribeManagedPrefixListsCommand(input)),
        modifyManagedPrefixList: (
            input: ModifyManagedPrefixListCommandInput,
        ): Promise<ModifyManagedPrefixListCommandOutput> =>
            client.send(new ModifyManagedPrefixListCommand(input)),
    };
}

/**
 * Build the add-only cluster-prefix-list Lambda handler. Existing CIDRs are
 * removed from the requested map before one versioned modify call is made.
 */
export function createHandler(
    dependencies: PrefixListHandlerDependencies = {},
): (event: PrefixListEvent, context: CfnResourceContext) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;

    const failureMessage = (error: unknown): string =>
        `failed to update cluster prefix list: ${errorMessage(error)}`;

    return wrapCfnHandler(
        async (event: PrefixListEvent): Promise<void> => {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);

            // Delete is intentionally add-only cleanup: respond without an EC2 call.
            if (event.RequestType === "Delete") {
                return;
            }

            const resourceProperties = event.ResourceProperties;
            const prefixListId = resourceProperties.prefix_list_id;
            const requestedEntries = new Map<string, PrefixListEntry>();
            for (const entry of resourceProperties.add_entries ?? []) {
                requestedEntries.set(entry.Cidr, entry);
            }

            const ec2 = (dependencies.ec2 ?? createEc2)();
            let nextToken: string | undefined;
            do {
                const input: GetManagedPrefixListEntriesCommandInput =
                    nextToken === undefined
                        ? { PrefixListId: prefixListId }
                        : { PrefixListId: prefixListId, NextToken: nextToken };
                const page = await ec2.getManagedPrefixListEntries(input);
                for (const existingEntry of page.Entries ?? []) {
                    if (existingEntry.Cidr === undefined) {
                        throw new Error(
                            "get_managed_prefix_list_entries returned an entry without Cidr",
                        );
                    }
                    requestedEntries.delete(existingEntry.Cidr);
                }
                nextToken = page.NextToken;
            } while (nextToken !== undefined && nextToken.length > 0);

            const addEntries = [...requestedEntries.values()];
            if (addEntries.length === 0) {
                logger.info("no new entries to add. skip.");
                return;
            }

            for (const entry of addEntries) {
                logger.info(
                    `adding new entry to cluster prefix list: ${JSON.stringify(entry)}`,
                );
            }

            const describeResult = await ec2.describeManagedPrefixLists({
                PrefixListIds: [prefixListId],
            });
            const version = describeResult.PrefixLists?.[0]?.Version;
            if (version === undefined) {
                throw new Error(
                    "describe_managed_prefix_lists returned no prefix-list version",
                );
            }

            await ec2.modifyManagedPrefixList({
                AddEntries: addEntries,
                PrefixListId: prefixListId,
                CurrentVersion: version,
            });
        },
        {
            physicalResourceId: (): string => PHYSICAL_RESOURCE_ID,
            errorMessage: (error: unknown): string => failureMessage(error),
            failureReason: (error: unknown): string => failureMessage(error),
            logger,
            responseSender: dependencies.responseSender,
        },
    );
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
