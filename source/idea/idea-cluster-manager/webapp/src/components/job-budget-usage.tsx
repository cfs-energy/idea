import React from 'react';
import {Box, ColumnLayout, Container, Grid, Header, PieChart, SpaceBetween} from '@cloudscape-design/components';
import {SocaJobEstimatedBudgetUsage} from '../client/data-model';
import Utils from '../common/utils';

export function buildBudgetUsage(budget: SocaJobEstimatedBudgetUsage) {
    return (
        <Container>
            <Grid gridDefinition={[{ colspan: { default: 12, xxs: 12 } }]}>
                <Box>
                    <Header variant="h2">Budget Overview</Header>
                    <Box padding={{ top: 'l' }}>
                        <Grid gridDefinition={[
                            { colspan: { default: 7, xxs: 12 } },
                            { colspan: { default: 5, xxs: 12 } }
                        ]}>
                            <Box>
                                <SpaceBetween size="l">
                                    <Container
                                        header={<Header variant="h3">Budget Details</Header>}
                                    >
                                        <ColumnLayout columns={2} variant="text-grid">
                                            <Box>
                                                <Box variant="awsui-key-label">Budget Name</Box>
                                                <Box>{budget.budget_name}</Box>
                                            </Box>
                                            <Box>
                                                <Box variant="awsui-key-label">Budget Limit</Box>
                                                <Box>{Utils.getFormattedAmount(budget.budget_limit)}</Box>
                                            </Box>
                                            <Box>
                                                <Box variant="awsui-key-label">Actual Spend</Box>
                                                <Box>{Utils.getFormattedAmount(budget.actual_spend)}</Box>
                                            </Box>
                                            <Box>
                                                <Box variant="awsui-key-label">Forecasted Spend</Box>
                                                <Box>{Utils.getFormattedAmount(budget.forecasted_spend)}</Box>
                                            </Box>
                                            <Box>
                                                <Box variant="awsui-key-label">Remaining Budget</Box>
                                                <Box>
                                                    {Utils.getFormattedAmount({
                                                        amount: Math.max(0, (budget.budget_limit?.amount || 0) - (budget.forecasted_spend?.amount || 0)),
                                                        unit: budget.budget_limit?.unit
                                                    })}
                                                </Box>
                                            </Box>
                                            <Box>
                                                {budget.job_usage_percent !== undefined && (
                                                    <>
                                                        <Box variant="awsui-key-label">Job Budget Impact</Box>
                                                        <Box>{budget.job_usage_percent.toFixed(2)}%</Box>
                                                    </>
                                                )}
                                            </Box>
                                        </ColumnLayout>
                                    </Container>
                                </SpaceBetween>
                            </Box>
                            <Box>
                                <Container
                                    header={<Header variant="h3">Budget Allocation</Header>}
                                >
                                    <PieChart
                                        hideFilter={true}
                                        data={[
                                            {
                                                title: "Actual Spend",
                                                value: Number((budget.actual_spend?.amount || 0).toFixed(2)),
                                                lastUpdate: new Date().toISOString()
                                            },
                                            {
                                                title: "Forecasted Additional Spend",
                                                value: Number(Math.max(0, (budget.forecasted_spend?.amount || 0) - (budget.actual_spend?.amount || 0)).toFixed(2)),
                                                lastUpdate: new Date().toISOString()
                                            },
                                            {
                                                title: "Remaining Budget",
                                                value: Number(Math.max(0, (budget.budget_limit?.amount || 0) - (budget.forecasted_spend?.amount || 0)).toFixed(2)),
                                                lastUpdate: new Date().toISOString()
                                            }
                                        ]}
                                        segmentDescription={(datum, sum) => {
                                            const percentage = (datum.value / sum * 100).toFixed(1);
                                            return `${datum.title}: ${Utils.getFormattedAmount({ amount: datum.value, unit: budget.budget_limit?.unit })} (${percentage}%)`;
                                        }}
                                    />
                                </Container>
                            </Box>
                        </Grid>
                    </Box>
                </Box>
            </Grid>
        </Container>
    )
}
