#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

from ideadatamodel import (
    exceptions, constants,
    SocaPaginator, SocaSortBy, SocaDateRange,
    ListJobsRequest, ListJobsResult,
    SocaJobState, SocaJob
)
from ideasdk.utils import Utils
from ideasdk.context import SocaCliContext

from typing import List, Any, Tuple, Dict, Optional
import os
import click
from rich.table import Table
from rich.text import Text
from rich import box
import arrow
from datetime import datetime

duration = Utils.duration

DISPLAY_DATE_FORMAT = arrow.FORMAT_W3C
DEFAULT_PAGE_SIZE = 20
DEFAULT_FROM = 0
DEFAULT_DATE_FIELD = 'end_time'
DEFAULT_PERIOD = f'{DEFAULT_DATE_FIELD}:1w'
DEFAULT_SORT_ORDER = 'desc'
DEFAULT_SORT_BY = f'{DEFAULT_DATE_FIELD}:{DEFAULT_SORT_ORDER}'


class JobQuery:
    def __init__(self, context: SocaCliContext, **kwargs):
        self.context = context
        self.kwargs = kwargs

    @property
    def show_exec_hosts(self) -> bool:
        return Utils.get_value_as_bool('execution_hosts', self.kwargs, False)

    @property
    def show_licenses(self) -> bool:
        return Utils.get_value_as_bool('licenses', self.kwargs, False)

    @property
    def show_bom(self) -> bool:
        if not self.is_history:
            return False
        return Utils.get_value_as_bool('bom', self.kwargs, False)

    @property
    def show_comment(self) -> bool:
        return Utils.get_value_as_bool('comment', self.kwargs, False)

    @property
    def is_history(self) -> bool:
        return Utils.get_value_as_bool('history', self.kwargs, False)

    @property
    def page_size(self) -> int:
        return Utils.get_value_as_int('page_size', self.kwargs, DEFAULT_PAGE_SIZE)

    @property
    def start(self) -> int:
        return Utils.get_value_as_int('start', self.kwargs, DEFAULT_FROM)

    @property
    def queue_type(self) -> Optional[str]:
        return Utils.get_value_as_string('queue_type', self.kwargs)

    @property
    def queue(self) -> Optional[str]:
        return Utils.get_value_as_string('queue', self.kwargs)

    @property
    def period(self) -> Tuple[str, arrow.Arrow, arrow.Arrow]:
        token = Utils.get_value_as_string('period', self.kwargs, DEFAULT_PERIOD)
        token = token.lower()

        def date(val=None) -> arrow.Arrow:
            if val:
                result = arrow.get(val)
            else:
                result = arrow.utcnow()
            return result.to(self.context.cluster_timezone())

        kv = token.split(':')
        if len(kv) == 2:
            key = self.get_date_field_key(kv[0])
            period = kv[1]
        else:
            key = DEFAULT_DATE_FIELD
            period = kv[0]

        if period == 'today':
            return key, date().floor('day'), date().ceil('day')
        elif period == 'yesterday':
            return key, date().shift(days=-1).floor('day'), date().shift(days=-1).ceil('day')
        elif period == 'this-week':
            return key, date().floor('week'), date().ceil('week')
        elif period == 'last-week':
            return key, date().shift(weeks=-1).floor('week'), date().shift(weeks=-1).ceil('week')
        elif period == 'this-month':
            return key, date().floor('month'), date().ceil('month')
        elif period == 'last-month':
            return key, date().shift(months=-1).floor('month'), date().shift(months=-1).ceil('month')
        elif period == 'this-year':
            return key, date().floor('year'), date().ceil('year')
        elif period == 'last-year':
            return key, date().shift(years=-1).floor('year'), date().shift(years=-1).ceil('year')
        elif period.endswith(('d', 'w', 'm', 'y')):
            token = period.strip().lower()
            if token.endswith('y'):
                years = Utils.get_as_int(period[:-1])
                start = date().shift(years=-years)
            elif token.endswith('m'):
                months = Utils.get_as_int(period[:-1])
                start = date().shift(months=-months)
            elif token.endswith('w'):
                weeks = Utils.get_as_int(period[:-1])
                start = date().shift(weeks=-weeks)
            else:
                days = Utils.get_as_int(period[:-1])
                start = date().shift(days=-days)

            return key, start, date()
        else:
            tokens = period.split(',')
            start = None
            end = None
            for token in tokens:
                if token.startswith(('start', 's')):
                    start = date(token.split('=')[1]).floor('day')
                if token.startswith(('end', 'e')):
                    end = date(token.split('=')[1]).ceil('day')
            if start and end:
                return key, start, end
            if start:
                return key, start, date()
            if end:
                return key, end.shift(days=-1), end
            raise exceptions.invalid_params(f'invalid period: {period}')

    @staticmethod
    def get_date_field_key(token: str) -> str:
        if token is None:
            return DEFAULT_DATE_FIELD
        if token.startswith(('queue', 'q')):
            return 'queue_time'
        elif token.startswith(('start', 's')):
            return 'start_time'
        elif token.startswith(('end', 'e')):
            return 'end_time'
        elif token.startswith(('provision', 'p')):
            return 'provisioning_time'
        return DEFAULT_DATE_FIELD

    @property
    def sort_by(self):
        sort_by = Utils.get_value_as_string('sort_by', self.kwargs, DEFAULT_SORT_BY)
        if ':' not in sort_by:
            sort_by = f'{sort_by}:{DEFAULT_SORT_ORDER}'
        tokens = sort_by.lower().strip().split(':')
        if len(tokens) != 2:
            return DEFAULT_SORT_BY
        key = self.get_date_field_key(tokens[0])
        order = tokens[1]
        if order not in ('asc', 'desc'):
            order = 'desc'
        return f'{key}:{order}'

    def build(self) -> ListJobsRequest:
        sort_by = self.sort_by
        sort_by_key = sort_by.split(':')[0]
        sort_by_order = sort_by.split(':')[1]
        period_key, period_start, period_end = self.period
        return ListJobsRequest(
            queue_type=self.queue_type,
            paginator=SocaPaginator(
                page_size=self.page_size,
                start=self.start
            ),
            sort_by=SocaSortBy(
                key=sort_by_key,
                order=sort_by_order
            ),
            date_range=SocaDateRange(
                key=period_key,
                start=period_start.datetime,
                end=period_end.datetime
            )
        )

    def __str__(self):
        params = []
        queue_type = self.queue_type
        if queue_type:
            params.append(f'QueueType: {queue_type}')

        if not self.is_history:
            return ', '.join(params)

        period_key, period_start, period_end = self.period
        if period_start and period_end:
            params.append(f'Period: {period_key} [{period_start.format(DISPLAY_DATE_FORMAT)} - '
                          f'{period_end.format(DISPLAY_DATE_FORMAT)}]')

        sort_by = self.sort_by
        if sort_by:
            params.append(f'SortBy: {sort_by}')

        return ', '.join(params)


class JobRow:
    def __init__(self, context: SocaCliContext, query: JobQuery, job: Optional[SocaJob] = None):
        self.context = context
        self.query = query
        self.job = job

    def _format(self, value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        return arrow.get(value).to(self.context.cluster_timezone()).format(DISPLAY_DATE_FORMAT)

    @property
    def job_info(self) -> str:
        job_info = f'JobId: {self.job.job_id}{os.linesep}'
        if self.job.is_shared_capacity():
            job_info += f'JobGroup: {self.job.get_job_group()}{os.linesep}'
        job_info += f'{os.linesep}'
        job_info += f'Queue: {self.job.queue}{os.linesep}'

        if self.job.is_ephemeral_capacity():
            fleet = 'ephemeral'
        elif self.job.is_persistent_capacity():
            fleet = 'persistent'
        else:
            fleet = 'shared'

        job_info += f'Type: {self.job.queue_type} ({fleet}){os.linesep}'
        job_info += f'ScalingMode: {self.job.scaling_mode}{os.linesep}'
        job_info += f'{os.linesep}'
        job_info += f'Name: {self.job.name}{os.linesep}'
        job_info += f'Project: {self.job.project}{os.linesep}'
        job_info += f'Owner: {self.job.owner}'

        return job_info

    @property
    def job_state(self) -> Text:
        state = self.job.state
        if self.job.state == SocaJobState.RUNNING:
            style = 'bold green'
        elif self.job.is_provisioned() and self.job.state == SocaJobState.QUEUED:
            style = 'bold yellow'
            state = 'provisioning'
        elif self.job.state == SocaJobState.FINISHED:
            style = 'bold green'
        else:
            style = 'dark_orange'
        return self.context.get_label(state, style)

    @property
    def timings(self) -> str:
        datetime_info = ''

        s_queue_time = self._format(self.job.queue_time)
        s_provisioning_time = self._format(self.job.provisioning_time)
        s_start_time = self._format(self.job.start_time)
        s_end_time = self._format(self.job.end_time)
        datetime_info += f'Timestamps:{os.linesep}'
        datetime_info += f'- Queue: {s_queue_time}{os.linesep}'
        if s_provisioning_time:
            datetime_info += f'- Provision: {s_provisioning_time}{os.linesep}'
        if s_start_time:
            datetime_info += f'- Start: {s_start_time}{os.linesep}'
        if s_end_time:
            datetime_info += f'- End: {s_end_time}{os.linesep}'

        datetime_info += f'{os.linesep}'

        datetime_info += f'Durations:{os.linesep}'
        if self.job.queue_time and self.job.provisioning_time:
            pending = (self.job.provisioning_time - self.job.queue_time).seconds
            datetime_info += f'- Pending: {duration(pending)}{os.linesep}'
        elif self.job.queue_time:
            pending = (arrow.utcnow() - self.job.queue_time).seconds
            datetime_info += f'- Queued: {duration(pending, absolute=False)}{os.linesep}'

        if self.job.start_time and self.job.provisioning_time:
            pending = (self.job.start_time - self.job.provisioning_time).seconds
            datetime_info += f'- Provision: {duration(pending)}{os.linesep}'
        elif self.job.provisioning_time:
            provisioning = (arrow.utcnow() - self.job.provisioning_time).seconds
            datetime_info += f'- ProvStarted: {duration(provisioning, absolute=False)}{os.linesep}'

        if self.job.end_time and self.job.start_time:
            execution = (self.job.end_time - self.job.start_time).seconds
            datetime_info += f'- Execution: {duration(execution)}{os.linesep}'
        elif self.job.start_time:
            execution = (arrow.utcnow() - self.job.start_time).seconds
            datetime_info += f'- ExecStarted: {duration(execution, absolute=False)}{os.linesep}'

        if self.job.queue_time and self.job.end_time:
            total_time = self.job.end_time - self.job.queue_time
            datetime_info += f'- Total: {duration(total_time.seconds)}'

        return datetime_info

    @property
    def capacity_info(self) -> str:
        capacity = ''
        if self.job.ondemand_capacity() > 0:
            capacity += f'On-Demand: {self.job.ondemand_nodes()} ({self.job.desired_capacity()}){os.linesep}'
        if self.job.spot_capacity() > 0:
            capacity += f'Spot: {self.job.spot_nodes()} ({self.job.desired_capacity()}){os.linesep}'
            if self.job.params.spot_price is None:
                capacity += f'SpotPrice: auto{os.linesep}'
            else:
                capacity += f'SpotPrice: {self.job.params.spot_price.formatted()}{os.linesep}'
        return capacity

    @property
    def compute_stack(self) -> str:
        compute_stack = f'CapacityType: {self.job.capacity_type()}{os.linesep}'
        compute_stack += f'{self.capacity_info}'
        compute_stack += f'InstanceTypes:{os.linesep}'
        for index, option in enumerate(self.job.provisioning_options.instance_types):
            compute_stack += f'- {option.name}, weight={option.weighted_capacity}{os.linesep}'
        compute_stack += f'BaseOS: {self.job.params.base_os}{os.linesep}'
        if self.job.params.instance_ami:
            compute_stack += f'AMI: {self.job.params.instance_ami}{os.linesep}'
        compute_stack += f'Hyper-threading: {self.job.params.enable_ht_support}{os.linesep}'
        compute_stack += f'EFA: {self.job.params.enable_efa_support}{os.linesep}'
        compute_stack += f'PlacementGroup: {self.job.params.enable_placement_group}'
        return compute_stack

    @property
    def root_storage(self) -> str:
        root_storage = f'- Size: {self.job.params.root_storage_size}{os.linesep}'
        root_storage += f'- KeepEBS: {self.job.params.keep_ebs_volumes}{os.linesep}'
        return root_storage

    @property
    def scratch_storage(self) -> str:
        if self.job.params.fsx_lustre.enabled:
            fsx_lustre = self.job.params.fsx_lustre
            scratch_storage = f'- FSxLustre: {fsx_lustre.enabled}{os.linesep}'
            if fsx_lustre.existing_fsx:
                scratch_storage += f'- Existing: {fsx_lustre.existing_fsx}{os.linesep}'
            else:
                scratch_storage += f'- Size: {fsx_lustre.size}{os.linesep}'
                scratch_storage += f'- PerUnitThru: {fsx_lustre.per_unit_throughput}{os.linesep}'
                scratch_storage += f'- DeployType: {fsx_lustre.deployment_type}{os.linesep}'
        else:
            scratch_storage = f'- Size: {self.job.params.scratch_storage_size}{os.linesep}'
            scratch_storage += f'- KeepEBS: {self.job.params.keep_ebs_volumes}{os.linesep}'
            if self.job.params.scratch_storage_iops > 0:
                scratch_storage += f'{os.linesep}- IOPS: {self.job.params.scratch_storage_size}{os.linesep}'
        return scratch_storage

    @property
    def storage(self) -> str:
        storage = f'Root: {os.linesep}'
        storage += f'{self.root_storage}'
        storage += f'{os.linesep}'
        storage += f'Scratch: {os.linesep}'
        storage += f'{self.scratch_storage}'
        return storage

    @property
    def flags(self) -> str:
        flags = f'Metrics: {os.linesep}'
        flags += f'- System: {self.job.params.enable_system_metrics}{os.linesep}'
        flags += f'- Anonymous: {self.job.params.enable_anonymous_metrics}'
        if self.job.notifications:
            flags += f'{os.linesep}{os.linesep}'
            flags += f'Notifications:{os.linesep}' \
                     f'- Started: {self.job.notifications.started}{os.linesep}' \
                     f'- Completed: {self.job.notifications.completed}'
        return flags

    @property
    def execution_hosts(self) -> str:
        if self.job.execution_hosts and len(self.job.execution_hosts) > 0:
            execution_hosts = ''
            show_max = 4
            for index, host in enumerate(self.job.execution_hosts):
                if index == show_max:
                    execution_hosts += f'({len(self.job.execution_hosts) - show_max} more.'
                    break
                execution_hosts += f'Host: {host.host}{os.linesep}'
                execution_hosts += f'- {host.instance_id}{os.linesep}'
                execution_hosts += f'- {host.instance_type}{os.linesep}'
        else:
            execution_hosts = 'not available'
        return execution_hosts

    @property
    def license_info(self) -> str:
        if not self.job.has_licenses():
            return '-'
        license_info = ''
        return license_info

    @property
    def comment(self) -> str:
        return self.job.comment

    @property
    def estimated_costs(self) -> str:
        if self.job.estimated_bom_cost is None:
            return 'not applicable'

        estimated_costs = ''
        total = self.job.estimated_bom_cost.line_items_total
        potential_savings = self.job.estimated_bom_cost.savings_total
        estimated_costs += f'EstimatedCost: {total.formatted()}{os.linesep}'
        if self.job.estimated_bom_cost.savings_total:
            estimated_costs += f'PotentialSavings: '
            savings_pct = self.job.estimated_bom_cost.savings_percent()
            estimated_costs += f'{potential_savings.formatted()} ({savings_pct}%){os.linesep}'
        if self.job.estimated_budget_usage:
            estimated_costs += f'{os.linesep}'
            budget_usage = self.job.estimated_budget_usage
            estimated_costs += f'BudgetUsage: {budget_usage.job_usage_percent}%{os.linesep}'
            estimated_costs += f'- AllocatedBudget: {budget_usage.budget_limit.formatted()}{os.linesep}'
            estimated_costs += f'- ActualSpend: {budget_usage.actual_spend.formatted()}{os.linesep}'
            estimated_costs += f'- ForecastedSpend: {budget_usage.forecasted_spend.formatted()}'

        return estimated_costs

    def columns(self) -> List[Tuple[List, Optional[Dict]]]:
        cols = [(['Job'], None),
                (['State'], None),
                (['Timing'], None),
                (['ComputeStack'], None),
                (['Storage'], None),
                (['Flags'], None)]
        if self.query.show_exec_hosts:
            cols.append((['Execution Hosts'], None))
        if self.query.show_licenses:
            cols.append((['Licenses'], None))
        if self.query.show_bom:
            cols.append((['Estimated Cost'], {'width': 40}))
        if self.query.show_comment:
            cols.append((['Comment'], {'width': 40}))
        return cols

    def build(self) -> List[Any]:
        row = [self.job_info,
               self.job_state,
               self.timings,
               self.compute_stack,
               self.storage,
               self.flags]
        if self.query.show_exec_hosts:
            row.append(self.execution_hosts)
        if self.query.show_licenses:
            row.append(self.license_info)
        if self.query.show_bom:
            row.append(self.estimated_costs)
        if self.query.show_comment:
            row.append(self.comment)
        return row


class JobListing:
    def __init__(self, context: SocaCliContext, query: JobQuery):
        context.check_root_access()
        self.context = context
        self.query = query

    def invoke(self) -> ListJobsResult:
        if self.query.is_history:
            namespace = 'SchedulerAdmin.ListCompletedJobs'
        else:
            namespace = 'SchedulerAdmin.ListActiveJobs'

        return self.context.unix_socket_client.invoke_alt(
            namespace=namespace,
            payload=self.query.build(),
            result_as=ListJobsResult
        )


@click.group()
def jobs():
    """
    jobs
    """


@jobs.command('list', context_settings=constants.CLICK_SETTINGS)
@click.option('--queue-type', '-q', help='Get jobs for a SOCA QueueType.')
@click.option('--queue', '-Q', help='Get jobs for a scheduler queue name.')
@click.option('--owner', '-o', default='all', help='List jobs for a specific user. Default: all')
@click.option('--jobs', '-j', help='Show listing for a JobId or multiple JobIds (separated by comma)')
@click.option('--job-groups', '-g', help='Show listing for a JobGroup or multiple JobGroups (separated by comma)')
@click.option('--projects', '-p', help='Show listing for a JobProject or multiple JobProjects (separated by comma)')
@click.option('--state', '-s', help='Filter active jobs listing for a specific job state. '
                                    'Must be one of: [queued, provisioning, running]. '
                                    'Applicable only when listing active jobs.')
@click.option('--licenses', '-l', is_flag=True, help='Show licenses requested by the Job.')
@click.option('--comment', '-c', is_flag=True, help='Show Job comments.')
@click.option('--execution-hosts', '-e', is_flag=True, help='Show execution hosts. Limits to 4.')
@click.option('--history', '-H', is_flag=True, help='List finished jobs from OpenSearch.')
@click.option('--bom', '-b', is_flag=True, help='(History Only) Show Estimated BOM Costs and Budget Usage.')
@click.option('--period', '-P', help='(History Only) Show listing for a specific time period. Default: end:1w.')
@click.option('--sort-by', '-S', help='(History Only) Show listing sorted by specified time property. '
                                      'One of: [q: queue, p: provisioning, s: start, e: end]. '
                                      'See examples for more options.')
@click.option('--page-size', '-n', help='Limit the no. of results displayed. Default: 20')
@click.option('--start', '-ps', help='Used for paging next batch of results. Default: 0')
@click.option('--file-export', '-x', help='Export listing results to a file. Export format is the '
                                          'listing format provided with -f option.')
def list_jobs(**kwargs):
    """
    list active jobs or finished jobs.

    Example:

    \b
    * list active jobs
    $ ideactl jobs list

    \b
    * list running jobs with execution hosts for a project and a specific job owner
    $ socideactlactl jobs list -e -p sample_project -o user1 -s running

    \b
    * list finished jobs for this week, sorted by end time in descending order
    $ ideactl jobs list -H

    \b
    * list finished jobs for a time period.
    (all date ranges are based on cluster timezone configured in soca.cluster.timezone)
        - for last 30 days:
          $ ideactl jobs list -H --period 30d
        - for today:
          $ ideactl jobs list -H --period today
        - for this week (week starts Sunday):
          $ ideactl jobs list -H --period this-week
        - for this month:
          $ ideactl jobs list -H --period this-month
        - for last month:
          $ ideactl jobs list -H --period last-month
        - for this year:
          $ ideactl jobs list -H --period this-year
        - for a date range:
          $ ideactl jobs list -H --period start=2021-06-05,end=2021-11-10

    \b
    * list finished jobs for last month, sorted by end time in ascending order
    $ ideactl jobs list -H --sort-by q:asc --sort-order asc --period last-month
    """
    context = SocaCliContext(api_context_path='/scheduler/api/v1')
    query = JobQuery(context=context, **kwargs)
    result = JobListing(context=context, query=query).invoke()
    listing = result.listing

    if len(listing) == 0:
        context.print('No Jobs found.')
        return

    if query.is_history:
        title = f'Finished Jobs'
    else:
        title = f'Active Jobs'

    title += ' ('
    title += f'Total: {result.paginator.total}'
    query_string = str(query)
    if not Utils.is_empty(query_string):
        title += f', {query_string}'
    title += f', PageSize: {result.paginator.page_size}, Start: {result.paginator.start}'
    title += ')'

    table = Table(title=title, box=box.HEAVY_HEAD, show_lines=True)
    columns = JobRow(context=context, query=query).columns()
    for args, kwargs in columns:
        if kwargs:
            table.add_column(*args, **kwargs)
        else:
            table.add_column(*args)

    for job in listing:
        row = JobRow(context=context, query=query, job=job).build()
        table.add_row(
            *row
        )
    context.print(table)
