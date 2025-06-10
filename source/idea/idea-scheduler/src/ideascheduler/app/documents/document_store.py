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
import os.path

import ideascheduler

from ideadatamodel import (
    exceptions,
    errorcodes,
    SocaPaginator,
    ListNodesRequest,
    ListNodesResult,
    ListJobsRequest,
    ListJobsResult,
    SocaComputeNode,
    SocaJob,
    SocaListingPayload,
)
from ideasdk.utils import Utils
from ideasdk.aws.opensearch.aws_opensearch_client import AwsOpenSearchClient

from ideascheduler.app.app_protocols import DocumentStoreProtocol
from typing import Dict, List, Optional
import arrow

DYNAMIC_CONFIGURATION_KEY = 'DYNAMIC_CONFIGURATION'


class DocumentStore(DocumentStoreProtocol):
    """
    DocumentStore for scheduler documents. primarily nodes and jobs
    """

    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self.logger = context.logger(name='document-store')
        self._is_initialized = False
        self.opensearch_client: Optional[AwsOpenSearchClient] = None

    def is_enabled(self) -> bool:
        domain_endpoint = self.context.config().get_string(
            'analytics.opensearch.domain_endpoint'
        )
        return Utils.is_not_empty(domain_endpoint)

    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def _jobs_index_pattern(self) -> str:
        cluster_name = self.context.cluster_name()
        module_id = self.context.module_id()
        return f'{cluster_name}_{module_id}_jobs_*'

    @property
    def _jobs_index(self) -> str:
        cluster_name = self.context.cluster_name()
        module_id = self.context.module_id()
        index_suffix = self.context.config().get_string(
            'scheduler.opensearch.jobs_index.suffix', '0'
        )
        return f'{cluster_name}_{module_id}_jobs_{index_suffix}'

    @property
    def _jobs_alias(self) -> str:
        """
        for each cluster, a unique index name for jobs is created.
        using the template_jobs.json, multiple unique indices will be queried using below index_alias.

        e.g. for your cluster_name: idea-prod, and module id: scheduler, the jobs can be searched using alias: idea-prod_scheduler_jobs
        multiple indices can exist under this alias:
        - idea-prod_scheduler_jobs_0
        - idea-prod_scheduler_jobs_1
        :return:
        """
        cluster_name = self.context.cluster_name()
        module_id = self.context.module_id()
        return f'{cluster_name}_{module_id}_jobs'

    @property
    def _nodes_index_pattern(self) -> str:
        cluster_name = self.context.cluster_name()
        module_id = self.context.module_id()
        return f'{cluster_name}_{module_id}_nodes_*'

    @property
    def _nodes_index(self) -> str:
        cluster_name = self.context.cluster_name()
        module_id = self.context.module_id()
        index_suffix = self.context.config().get_string(
            'scheduler.opensearch.jobs.index_suffix', '0'
        )
        return f'{cluster_name}_{module_id}_nodes_{index_suffix}'

    @property
    def _nodes_alias(self) -> str:
        cluster_name = self.context.cluster_name()
        module_id = self.context.module_id()
        return f'{cluster_name}_{module_id}_nodes'

    def _create_or_update_template(
        self,
        template_name: str,
        template_file: str,
        index_pattern: str,
        alias: str,
        number_of_shards: int,
        number_of_replicas: int,
    ):
        """
        create or update the opensearch index template settings

        the template contains below settings:
        - index settings for: number_of_shards, number_of_replicas
        - index patterns: indices for which the template is applicable. this is configured dynamically.
          * index pattern: [cluster_name]_nodes_* or [cluster_name]_jobs_*
          * index name format: [cluster_name]_nodes[_N] [cluster_name]_jobs[_N]
        - mappings: describe explicit field mappings for the document to be indexed.

        Additional Info:
        - idea-scheduler will update the template in opensearch automatically upon app restart or reload, if:
          * the template does not exist in opensearch; or
          * the template version in opensearch is < template version in file.
        - opensearch automatically applies the settings to new indices created, if the index name matches
          the index pattern.
        - for more information, see:
          * https://www.elastic.co/guide/en/elasticsearch/reference/current/index-templates.html
          * ideasdk.documents.DocumentStore

        :param template_name: template name should be prefixed with cluster name to ensure, multiple clusters can share the same
            opensearch cluster
        :param template_file: path to the index template file
        :param index_pattern: the index pattern to match and add index to the alias
        :param alias: the alias name
        :return:
        """
        try:
            with open(template_file) as f:
                template_json = f.read()
                template = Utils.from_json(template_json)

            template_version = Utils.get_value_as_int('version', template, 0)

            # if domain is not ready yet and is still being deployed, this line will fail
            # and scheduler initialization should retry initialization
            get_template_result = self.opensearch_client.get_template(
                name=template_name
            )
            existing_template = Utils.get_value_as_dict(
                template_name, get_template_result
            )
            existing_template_version = Utils.get_value_as_int(
                'version', existing_template, 0
            )

            if template_version <= existing_template_version:
                return

            # update no. of shards and no. of replicas
            settings = Utils.get_value_as_dict('settings', template)
            if settings is None:
                settings = {}
                template['settings'] = settings

            settings['number_of_shards'] = number_of_shards
            settings['number_of_replicas'] = number_of_replicas

            pattern_updated = False
            index_patterns = Utils.get_value_as_list('index_patterns', template)
            if index_patterns and len(index_patterns) > 0:
                for i, pattern in enumerate(index_patterns):
                    if DYNAMIC_CONFIGURATION_KEY != pattern:
                        continue
                    index_patterns[i] = index_pattern
                    pattern_updated = True

            alias_updated = False
            aliases = Utils.get_value_as_dict('aliases', template)
            if aliases and DYNAMIC_CONFIGURATION_KEY in aliases:
                del aliases[DYNAMIC_CONFIGURATION_KEY]
                aliases[alias] = {}
                alias_updated = True

            if not pattern_updated:
                raise exceptions.soca_exception(
                    error_code=errorcodes.CONFIG_ERROR,
                    message=f'opensearch template: {template_file} is invalid. '
                    f'index_patterns not found or invalid.',
                )
            if not alias_updated:
                raise exceptions.soca_exception(
                    error_code=errorcodes.CONFIG_ERROR,
                    message=f'opensearch template: {template_file} is invalid. '
                    f'template.aliases not found or invalid.',
                )

            self.opensearch_client.put_template(name=template_name, body=template)
            self.logger.info(
                f'OpenSearch index template updated. '
                f'TemplateName: {template_name}, '
                f'Version: {template_version}, '
                f'Pattern: {index_pattern}, '
                f'Alias: {alias}'
            )
        except Exception as e:
            self.logger.exception(
                f'failed to create or update template: {template_file}. Error: {e}'
            )
            raise exceptions.soca_exception(
                error_code=errorcodes.DOCUMENT_STORE_CONFIG_ERROR, message=str(e), exc=e
            )

    def initialize(self):
        """
        creates or updates the index templates in opensearch
        """
        try:
            self.opensearch_client = AwsOpenSearchClient(self.context)

            default_number_of_shards = self.context.config().get_int(
                'analytics.opensearch.default_number_of_shards', 2
            )
            default_number_of_replicas = self.context.config().get_int(
                'analytics.opensearch.default_number_of_replicas', 1
            )

            jobs_template_name = (
                f'{self.context.cluster_name()}_{self.context.module_id()}_jobs'
            )
            resources_dir = self.context.get_resources_dir()
            jobs_template_file = os.path.join(
                resources_dir, 'opensearch', 'template_jobs.json'
            )

            jobs_number_of_shards = self.context.config().get_int(
                'scheduler.opensearch.jobs.number_of_shards', default_number_of_shards
            )
            jobs_number_of_replicas = self.context.config().get_int(
                'scheduler.opensearch.jobs.number_of_replicas',
                default_number_of_replicas,
            )
            self._create_or_update_template(
                template_name=jobs_template_name,
                template_file=jobs_template_file,
                index_pattern=self._jobs_index_pattern,
                alias=self._jobs_alias,
                number_of_shards=jobs_number_of_shards,
                number_of_replicas=jobs_number_of_replicas,
            )

            nodes_number_of_shards = self.context.config().get_int(
                'scheduler.opensearch.nodes.number_of_shards', default_number_of_shards
            )
            nodes_number_of_replicas = self.context.config().get_int(
                'scheduler.opensearch.nodes.number_of_replicas',
                default_number_of_replicas,
            )
            nodes_template_name = (
                f'{self.context.cluster_name()}_{self.context.module_id()}_nodes'
            )
            nodes_template_file = os.path.join(
                resources_dir, 'opensearch', 'template_nodes.json'
            )
            self._create_or_update_template(
                template_name=nodes_template_name,
                template_file=nodes_template_file,
                index_pattern=self._nodes_index_pattern,
                alias=self._nodes_alias,
                number_of_shards=nodes_number_of_shards,
                number_of_replicas=nodes_number_of_replicas,
            )

            self._is_initialized = True
            return True

        except Exception as e:
            self.logger.exception(f'failed to initialize opensearch indices: {e}')
            self._is_initialized = False
            return False

    def add_jobs(self, jobs: List[SocaJob], **kwargs) -> bool:
        if not self.is_enabled():
            return False
        if not self.is_initialized():
            self.initialize()

        docs = {}
        for job in jobs:
            docs[job.job_uid] = Utils.to_dict(job)

        return self.opensearch_client.bulk_index(index_name=self._jobs_index, docs=docs)

    def add_nodes(self, nodes: List[SocaComputeNode], **kwargs) -> bool:
        if not self.is_enabled():
            return False
        if not self.is_initialized():
            self.initialize()

        docs = {}
        for node in nodes:
            docs[node.instance_id] = Utils.to_dict(node)

        return self.opensearch_client.bulk_index(
            index_name=self._nodes_index, docs=docs
        )

    def _search(
        self, index: str, options: SocaListingPayload, default_sort_by: str
    ) -> Dict:
        term_filters = []
        if Utils.is_not_empty(options.filters):
            for listing_filter in options.filters:
                if Utils.is_empty(listing_filter.key):
                    continue
                if Utils.is_empty(listing_filter.value):
                    continue
                if listing_filter.key == '$all':
                    term_filters.append(
                        {
                            'query_string': {
                                'query': Utils.get_as_string(listing_filter.value),
                                'fields': [],
                            }
                        }
                    )
                else:
                    if isinstance(listing_filter.value, list):
                        value = listing_filter.value
                    else:
                        value = [listing_filter.value]
                    term_filters.append({'terms': {listing_filter.key: value}})

        filters = []
        if options.date_range:
            date_range = options.date_range
            date_range_key = date_range.key
            date_range_start = arrow.get(date_range.start).isoformat()
            date_range_end = arrow.get(date_range.end).isoformat()
            filters.append(
                {
                    'range': {
                        date_range_key: {'gte': date_range_start, 'lt': date_range_end}
                    }
                }
            )

        query = None
        if len(filters) > 0 or len(term_filters) is not None:
            query = {'bool': {}}
            if len(filters) > 0:
                query['bool']['filter'] = filters
            if len(term_filters) > 0:
                query['bool']['must'] = term_filters

        body = None
        if query:
            body = {'query': query}

        self.logger.info(f'ES Request: {Utils.to_json(body)}')

        if options.sort_by:
            sort_by = f'{options.sort_by.key}:{options.sort_by.order}'
        else:
            sort_by = default_sort_by

        return self.opensearch_client.os_client.search(
            index=index,
            sort=sort_by,
            size=options.page_size,
            from_=options.page_start,
            body=body,
        )

    def search_jobs(self, options: ListJobsRequest, **kwargs) -> ListJobsResult:
        if not self.is_enabled() or not self.is_initialized():
            return ListJobsResult(
                listing=[],
                paginator=SocaPaginator(
                    total=0, page_size=options.page_size, start=options.page_start
                ),
            )

        results = self._search(
            index=self._jobs_alias, options=options, default_sort_by='queue_time:desc'
        )

        hits = Utils.get_value_as_dict('hits', results)
        total = Utils.get_value_as_dict('total', hits)
        entries = Utils.get_value_as_list('hits', hits, [])

        listing = []
        for entry in entries:
            source = Utils.get_value_as_dict('_source', entry)
            listing.append(SocaJob(**source))

        return ListJobsResult(
            listing=listing,
            filters=options.filters,
            date_range=options.date_range,
            paginator=SocaPaginator(
                total=Utils.get_value_as_int('value', total),
                page_size=options.page_size,
                start=options.page_start,
            ),
        )

    def search_nodes(self, options: ListNodesRequest, **kwargs) -> ListNodesResult:
        if not self.is_enabled() or not self.is_initialized():
            return ListNodesResult(
                listing=[],
                paginator=SocaPaginator(
                    total=0, page_size=options.page_size, start=options.page_start
                ),
            )

        results = self._search(
            index=self._nodes_alias, options=options, default_sort_by='launch_time:desc'
        )

        hits = Utils.get_value_as_dict('hits', results)
        total = Utils.get_value_as_dict('total', hits)
        entries = Utils.get_value_as_list('hits', hits, [])

        listing = []
        for entry in entries:
            source = Utils.get_value_as_dict('_source', entry)
            listing.append(SocaComputeNode(**source))

        return ListNodesResult(
            listing=listing,
            filters=options.filters,
            date_range=options.date_range,
            paginator=SocaPaginator(
                total=Utils.get_value_as_int('value', total),
                page_size=options.page_size,
                start=options.page_start,
            ),
        )
