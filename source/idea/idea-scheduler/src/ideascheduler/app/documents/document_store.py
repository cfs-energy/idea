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
from typing import Dict, List, Optional, Set
import arrow

DYNAMIC_CONFIGURATION_KEY = 'DYNAMIC_CONFIGURATION'
KEYWORD_SUBFIELD = 'raw'


def find_keyword_subfield_paths(properties: Dict, prefix: str = '') -> Set[str]:
    """
    dotted paths of analysed fields in an index mapping that declare a keyword sub-field.

    a terms query on the analysed field matches tokens rather than the stored value, so
    an exact-match filter has to target the sub-field.
    """
    paths = set()
    if not isinstance(properties, dict):
        return paths
    for name, mapping in properties.items():
        if not isinstance(mapping, dict):
            continue
        path = f'{prefix}{name}'
        subfields = mapping.get('fields')
        if mapping.get('type') == 'text' and isinstance(subfields, dict):
            if KEYWORD_SUBFIELD in subfields:
                paths.add(path)
        nested = mapping.get('properties')
        if isinstance(nested, dict):
            paths.update(find_keyword_subfield_paths(nested, f'{path}.'))
    return paths


class DocumentStore(DocumentStoreProtocol):
    """
    DocumentStore for scheduler documents. primarily nodes and jobs
    """

    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self.logger = context.logger(name='document-store')
        self._is_initialized = False
        self.opensearch_client: Optional[AwsOpenSearchClient] = None
        self._jobs_keyword_subfields: Set[str] = set()
        self._nodes_keyword_subfields: Set[str] = set()

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

    def _read_keyword_subfields(self, template_file: str) -> Set[str]:
        """
        read the index template and return the fields whose exact-match filters must
        target the keyword sub-field. an unreadable template leaves filters unchanged.
        """
        try:
            with open(template_file) as f:
                template = Utils.from_json(f.read())
            mappings = Utils.get_value_as_dict('mappings', template, {})
            properties = Utils.get_value_as_dict('properties', mappings, {})
            return find_keyword_subfield_paths(properties)
        except Exception as e:
            self.logger.warning(
                f'failed to read field mappings from template: {template_file}. '
                f'exact-match filters will target the analysed field. Error: {e}'
            )
            return set()

    def _verify_keyword_subfields(
        self, alias: str, expected: Set[str]
    ) -> Dict[str, List[str]]:
        """
        report any index behind the alias whose live mapping lacks a keyword sub-field
        the filters target. such an index matches nothing on an exact-match filter,
        which reads as an empty listing rather than as an error.

        :return: index name -> the sub-fields it is missing
        """
        missing_by_index: Dict[str, List[str]] = {}
        if len(expected) == 0:
            return missing_by_index
        try:
            mappings = self.opensearch_client.os_client.indices.get_mapping(index=alias)
        except Exception as e:
            self.logger.warning(
                f'unable to read live field mappings for alias: {alias}. '
                f'exact-match filters are derived from the index template. Error: {e}'
            )
            return missing_by_index
        for index_name, index_mapping in Utils.get_as_dict(mappings, {}).items():
            properties = Utils.get_value_as_dict(
                'properties', Utils.get_value_as_dict('mappings', index_mapping, {}), {}
            )
            missing = sorted(expected - find_keyword_subfield_paths(properties))
            if len(missing) == 0:
                continue
            missing_by_index[index_name] = missing
            self.logger.error(
                f'index {index_name} has no {KEYWORD_SUBFIELD} sub-field for: '
                f'{", ".join(missing)}. filters on those fields match nothing in this '
                f'index; it predates the current template and needs reindexing.'
            )
        return missing_by_index

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
            self._jobs_keyword_subfields = self._read_keyword_subfields(
                jobs_template_file
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
            self._nodes_keyword_subfields = self._read_keyword_subfields(
                nodes_template_file
            )

            self._verify_keyword_subfields(
                alias=self._jobs_alias, expected=self._jobs_keyword_subfields
            )
            self._verify_keyword_subfields(
                alias=self._nodes_alias, expected=self._nodes_keyword_subfields
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

    @staticmethod
    def _term_key(key: str, keyword_subfields: Set[str]) -> str:
        """
        the field an exact-match filter has to target. already-qualified keys and fields
        mapped as keyword are left alone.
        """
        if key in keyword_subfields:
            return f'{key}.{KEYWORD_SUBFIELD}'
        return key

    def _search(
        self,
        index: str,
        options: SocaListingPayload,
        default_sort_by: str,
        keyword_subfields: Set[str],
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
                    # a terms query is an exact match, so an analysed field has to be
                    # addressed by its keyword sub-field.
                    term_key = self._term_key(listing_filter.key, keyword_subfields)
                    term_filters.append({'terms': {term_key: value}})

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
            index=self._jobs_alias,
            options=options,
            default_sort_by='queue_time:desc',
            keyword_subfields=self._jobs_keyword_subfields,
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

    def _get_first_job(self, query: Dict) -> Optional[SocaJob]:
        results = self.opensearch_client.os_client.search(
            index=self._jobs_alias,
            sort='queue_time:desc',
            size=1,
            from_=0,
            body={'query': query},
        )
        hits = Utils.get_value_as_dict('hits', results)
        entries = Utils.get_value_as_list('hits', hits, [])
        if len(entries) == 0:
            return None
        source = Utils.get_value_as_dict('_source', entries[0])
        if source is None:
            return None
        return SocaJob(**source)

    def get_job(
        self,
        job_uid: Optional[str] = None,
        job_id: Optional[str] = None,
        owner: Optional[str] = None,
        **kwargs,
    ) -> Optional[SocaJob]:
        """
        read one indexed job.

        job_uid names the job exactly: jobs are indexed under it, so it is matched as
        the document id and does not depend on how the field itself is mapped. a job id
        is a scheduler sequence number that restarts when the scheduler host is
        replaced, so several jobs can carry it and the most recently queued is returned.
        """
        if not self.is_enabled() or not self.is_initialized():
            return None
        if Utils.is_not_empty(job_uid):
            return self._get_first_job({'ids': {'values': [job_uid]}})
        if Utils.is_empty(job_id):
            return None
        subfields = self._jobs_keyword_subfields
        term_filters = [{'terms': {self._term_key('job_id', subfields): [job_id]}}]
        if Utils.is_not_empty(owner):
            term_filters.append(
                {'terms': {self._term_key('owner', subfields): [owner]}}
            )
        return self._get_first_job({'bool': {'must': term_filters}})

    def search_nodes(self, options: ListNodesRequest, **kwargs) -> ListNodesResult:
        if not self.is_enabled() or not self.is_initialized():
            return ListNodesResult(
                listing=[],
                paginator=SocaPaginator(
                    total=0, page_size=options.page_size, start=options.page_start
                ),
            )

        results = self._search(
            index=self._nodes_alias,
            options=options,
            default_sort_by='launch_time:desc',
            keyword_subfields=self._nodes_keyword_subfields,
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
