import React, {useEffect, useState} from "react";
import {Box, ExpandableSection, Table} from "@cloudscape-design/components";
import {QueueSummary} from "../../client/data-model";
import {AppContext} from "../../common";

const OS_LABELS: {[k: string]: string} = {
    amazonlinux2023: 'Amazon Linux 2023',
    amazonlinux2: 'Amazon Linux 2',
    rhel8: 'RHEL 8', rhel9: 'RHEL 9', rhel10: 'RHEL 10',
    rocky8: 'Rocky Linux 8', rocky9: 'Rocky Linux 9', rocky10: 'Rocky Linux 10',
    ubuntu2204: 'Ubuntu 22.04', ubuntu2404: 'Ubuntu 24.04',
};

/** Which queue runs which processor architecture and operating system, so a script names a
 * queue that can run it. Renders nothing when the list cannot be read. */
export default function QueueReference() {
    const [queues, setQueues] = useState<QueueSummary[]>();
    useEffect(() => {
        AppContext.get().client().scheduler().listQueues()
            .then(result => setQueues(result.listing ?? []))
            .catch(() => setQueues([]));
    }, []);
    if (!queues || queues.length === 0) {
        return null;
    }
    return (
        <ExpandableSection headerText={`Queues you can use (${queues.length})`}
                           headerDescription="Set one with #PBS -q. Instance types must match the queue's architecture.">
            <Table variant="embedded" items={queues} trackBy="name" columnDefinitions={[
                {id: 'name', header: 'Queue', cell: q => <Box variant="code">{q.name}</Box>},
                {id: 'architecture', header: 'Architecture', cell: q => q.architecture ?? '–'},
                {id: 'os', header: 'Operating system', cell: q => (q.base_os && OS_LABELS[q.base_os]) ?? q.base_os ?? '–'},
                {id: 'types', header: 'Default instance types', cell: q => q.instance_types?.join(', ') || '–'},
            ]}/>
        </ExpandableSection>
    );
}
