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

"""
Test Cases for DocumentStore term filters

A terms query is an exact match, so a filter targeting an analysed field matches
tokens rather than the stored value. These cases pin the key that is sent to
OpenSearch and the mapping the key set is derived from.
"""

from ideadatamodel import SocaFilter
from ideadatamodel.scheduler import ListJobsRequest
from ideasdk.utils import Utils
from ideascheduler.app.documents.document_store import (
    DocumentStore,
    find_keyword_subfield_paths,
)

import os
import pytest


JOBS_TEMPLATE = os.path.join(
    os.path.dirname(__file__), '..', 'resources', 'opensearch', 'template_jobs.json'
)
NODES_TEMPLATE = os.path.join(
    os.path.dirname(__file__), '..', 'resources', 'opensearch', 'template_nodes.json'
)


def _template_properties(template_file: str):
    with open(template_file) as f:
        template = Utils.from_json(f.read())
    return template['mappings']['properties']


class _FakeOsClient:
    def __init__(self, hits=None):
        self.body = None
        self.size = None
        self.sort = None
        self._hits = hits or []

    def search(self, index, sort, size, from_, body):
        self.body = body
        self.size = size
        self.sort = sort
        return {'hits': {'total': {'value': len(self._hits)}, 'hits': self._hits}}


class _FakeOpenSearchClient:
    def __init__(self, hits=None):
        self.os_client = _FakeOsClient(hits=hits)


def _run_search(context, filters, keyword_subfields):
    store = DocumentStore(context=context)
    store.opensearch_client = _FakeOpenSearchClient()
    options = ListJobsRequest(filters=filters, page_size=10, page_start=0)
    store._search(
        index='jobs',
        options=options,
        default_sort_by='queue_time:desc',
        keyword_subfields=keyword_subfields,
    )
    body = store.opensearch_client.os_client.body
    return body['query']['bool']['must']


# --- key resolution ---


def test_search_targets_keyword_subfield_for_analysed_field(context):
    """
    owner is mapped as text with a raw keyword sub-field, so 'jane-doe' is indexed as
    ['jane', 'doe']. the term filter has to address owner.raw to match the value.
    """
    term_filters = _run_search(
        context,
        filters=[SocaFilter(key='owner', value='jane-doe')],
        keyword_subfields={'owner'},
    )
    assert term_filters == [{'terms': {'owner.raw': ['jane-doe']}}]


def test_search_leaves_keyword_mapped_field_unchanged(context):
    """job_id is mapped as keyword, so it is already exact"""
    term_filters = _run_search(
        context,
        filters=[SocaFilter(key='job_id', value='42')],
        keyword_subfields={'owner'},
    )
    assert term_filters == [{'terms': {'job_id': ['42']}}]


def test_search_leaves_already_qualified_key_unchanged(context):
    """resolution is idempotent: a caller may pass owner.raw itself"""
    term_filters = _run_search(
        context,
        filters=[SocaFilter(key='owner.raw', value='jane-doe')],
        keyword_subfields={'owner'},
    )
    assert term_filters == [{'terms': {'owner.raw': ['jane-doe']}}]


def test_search_resolves_list_values(context):
    term_filters = _run_search(
        context,
        filters=[SocaFilter(key='owner', value=['jane-doe', 'jane'])],
        keyword_subfields={'owner'},
    )
    assert term_filters == [{'terms': {'owner.raw': ['jane-doe', 'jane']}}]


def test_search_all_filter_is_not_a_term_filter(context):
    """$all renders a query_string and is unaffected by key resolution"""
    term_filters = _run_search(
        context,
        filters=[SocaFilter(key='$all', value='jane-doe')],
        keyword_subfields={'owner'},
    )
    assert term_filters == [{'query_string': {'query': 'jane-doe', 'fields': []}}]


def test_search_without_keyword_subfields_is_unchanged(context):
    """
    an unreadable template yields an empty key set. filters then behave as before the
    change rather than silently matching nothing.
    """
    term_filters = _run_search(
        context,
        filters=[SocaFilter(key='owner', value='jane-doe')],
        keyword_subfields=set(),
    )
    assert term_filters == [{'terms': {'owner': ['jane-doe']}}]


# --- key set derived from the shipped index templates ---


def test_jobs_template_keyword_subfields():
    paths = find_keyword_subfield_paths(_template_properties(JOBS_TEMPLATE))
    # analysed fields a filter can target
    assert {'owner', 'project', 'name', 'queue', 'queue_type'} <= paths
    # nested properties are reached
    assert 'params.instance_types' in paths
    assert 'execution_hosts.instance_id' in paths
    # keyword mapped fields are already exact
    assert 'job_id' not in paths
    assert 'state' not in paths
    # text with no keyword sub-field has nothing to resolve to
    assert 'error_message' not in paths
    assert 'comment' not in paths


def test_nodes_template_has_no_analysed_filter_targets():
    """the nodes mapping declares no text field, so node filters are unaffected"""
    assert find_keyword_subfield_paths(_template_properties(NODES_TEMPLATE)) == set()


@pytest.mark.parametrize(
    'properties',
    [
        {},
        {'owner': 'not-a-mapping'},
        {'owner': {'type': 'text'}},
        {'owner': {'type': 'text', 'fields': {}}},
        {'owner': {'type': 'keyword', 'fields': {'raw': {'type': 'keyword'}}}},
    ],
)
def test_find_keyword_subfield_paths_ignores_non_candidates(properties):
    assert find_keyword_subfield_paths(properties) == set()


def test_read_keyword_subfields_reads_the_shipped_jobs_template(context):
    """
    the key set search actually uses comes from this method, not from the parsing
    helper alone. a read that quietly returned nothing would restore the old keys.
    """
    store = DocumentStore(context=context)
    paths = store._read_keyword_subfields(JOBS_TEMPLATE)
    assert 'owner' in paths
    assert paths == find_keyword_subfield_paths(_template_properties(JOBS_TEMPLATE))


def test_read_keyword_subfields_returns_empty_on_missing_template(context):
    """a template that cannot be read must not break search"""
    store = DocumentStore(context=context)
    assert store._read_keyword_subfields('/nonexistent/template_jobs.json') == set()


# --- reading one job ---


def _store_for_get_job(context, monkeypatch, hits=None) -> DocumentStore:
    store = DocumentStore(context=context)
    monkeypatch.setattr(DocumentStore, 'is_enabled', lambda _self: True)
    store._is_initialized = True
    store._jobs_keyword_subfields = {'owner'}
    store.opensearch_client = _FakeOpenSearchClient(hits=hits)
    return store


def test_get_job_matches_a_job_uid_as_the_document_id(context, monkeypatch):
    """
    jobs are indexed under job_uid, so an ids query is exact. job_uid has no explicit
    mapping: a term filter on it would be matched against analysed tokens instead.
    """
    hit = {'_source': {'job_id': '41', 'job_uid': 'Tw8fKy2', 'owner': 'jane-doe'}}
    store = _store_for_get_job(context, monkeypatch, hits=[hit])

    job = store.get_job(job_uid='Tw8fKy2')

    assert job.job_uid == 'Tw8fKy2'
    body = store.opensearch_client.os_client.body
    assert body == {'query': {'ids': {'values': ['Tw8fKy2']}}}


def test_get_job_scopes_a_job_id_to_an_owner_on_the_keyword_subfield(
    context, monkeypatch
):
    """
    several documents can carry one job id, so the owner term decides which of them the
    caller meant. against the analysed owner field a hyphenated username matches none.
    """
    store = _store_for_get_job(context, monkeypatch)

    assert store.get_job(job_id='41', owner='jane-doe') is None

    os_client = store.opensearch_client.os_client
    assert os_client.body == {
        'query': {
            'bool': {
                'must': [
                    {'terms': {'job_id': ['41']}},
                    {'terms': {'owner.raw': ['jane-doe']}},
                ]
            }
        }
    }
    # newest first, so the id resolves to the job that holds it now
    assert os_client.sort == 'queue_time:desc'
    assert os_client.size == 1


def test_get_job_without_an_owner_is_unscoped(context, monkeypatch):
    """an elevated read is cluster wide, as the listing is"""
    store = _store_for_get_job(context, monkeypatch)

    store.get_job(job_id='41')

    assert store.opensearch_client.os_client.body == {
        'query': {'bool': {'must': [{'terms': {'job_id': ['41']}}]}}
    }


def test_get_job_needs_an_identifier(context, monkeypatch):
    store = _store_for_get_job(context, monkeypatch)

    assert store.get_job() is None
    assert store.opensearch_client.os_client.body is None


# --- live mapping check ---


class _MappingOsClient:
    """stands in for the opensearch indices api behind an alias."""

    def __init__(self, mappings, raises: bool = False):
        self.indices = self
        self._mappings = mappings
        self._raises = raises
        self.requested_alias = None

    def get_mapping(self, index):
        self.requested_alias = index
        if self._raises:
            raise RuntimeError('opensearch unavailable')
        return self._mappings


def _store_with_mappings(context, mappings, raises: bool = False) -> DocumentStore:
    store = DocumentStore(context=context)
    store.opensearch_client = _FakeOpenSearchClient()
    store.opensearch_client.os_client = _MappingOsClient(mappings, raises=raises)
    return store


def test_verify_keyword_subfields_reports_an_index_without_the_subfield(context):
    """
    an index created before the current template maps owner as plain text. filters on
    owner.raw then match nothing in it, and the owner sees an empty listing.
    """
    store = _store_with_mappings(
        context,
        {
            'jobs-2024': {'mappings': {'properties': {'owner': {'type': 'text'}}}},
            'jobs-2026': {
                'mappings': {
                    'properties': {
                        'owner': {
                            'type': 'text',
                            'fields': {'raw': {'type': 'keyword'}},
                        }
                    }
                }
            },
        },
    )

    missing = store._verify_keyword_subfields(alias='jobs', expected={'owner'})

    assert missing == {'jobs-2024': ['owner']}
    assert store.opensearch_client.os_client.requested_alias == 'jobs'


def test_verify_keyword_subfields_is_quiet_when_every_index_agrees(context):
    store = _store_with_mappings(
        context,
        {
            'jobs-2026': {
                'mappings': {
                    'properties': {
                        'owner': {
                            'type': 'text',
                            'fields': {'raw': {'type': 'keyword'}},
                        }
                    }
                }
            }
        },
    )

    assert store._verify_keyword_subfields(alias='jobs', expected={'owner'}) == {}


def test_verify_keyword_subfields_survives_an_unreadable_mapping(context):
    """a mapping read failure must not stop the module from starting"""
    store = _store_with_mappings(context, {}, raises=True)

    assert store._verify_keyword_subfields(alias='jobs', expected={'owner'}) == {}
