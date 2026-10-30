"""Exercise the pinned HTTP client and bulk helper without an OpenSearch server."""

from unittest.mock import Mock

from ideasdk.aws.opensearch.aws_opensearch_client import AwsOpenSearchClient


def test_http_client_supports_the_sdk_operations():
    context = Mock()
    context.config().get_string.return_value = 'search.example.invalid'
    client = AwsOpenSearchClient(context)
    request = Mock()
    client.os_client.transport.perform_request = request

    request.return_value = {'result': 'created'}
    assert client.add_index_entry('jobs', 'job-1', '{"state":"R"}')
    assert request.call_args.args[:2] == ('PUT', '/jobs/_doc/job-1')

    request.return_value = {'items': [{'index': {'status': 201}}]}
    assert client.bulk_index('jobs', {'job-1': {'state': 'R'}})
    assert request.call_args.args[:2] == ('POST', '/_bulk')
    assert '"_id":"job-1"' in request.call_args.kwargs['body']

    request.return_value = {'hits': {'total': {'value': 1}, 'hits': []}}
    assert client.search('jobs', size=10)['hits']['total']['value'] == 1
    assert client.exists('jobs', {'query': {'match_all': {}}})
    assert request.call_args.args[:2] == ('POST', '/jobs/_search')

    request.return_value = {'jobs': {'index_patterns': ['jobs-*']}}
    assert client.get_template('jobs')['jobs']['index_patterns'] == ['jobs-*']
    assert request.call_args.args[:2] == ('GET', '/_template/jobs')
    client.put_template('jobs', {'index_patterns': ['jobs-*']})
    assert request.call_args.args[:2] == ('PUT', '/_template/jobs')
    client.delete_template('jobs')
    assert request.call_args.args[:2] == ('DELETE', '/_template/jobs')

    request.reset_mock()
    request.return_value = {'jobs-1': {}}
    client.delete_alias_and_index('jobs')
    assert [call.args[:2] for call in request.call_args_list] == [
        ('GET', '/_alias/jobs'),
        ('DELETE', '/jobs-1/_alias/jobs'),
        ('DELETE', '/jobs-1'),
    ]
