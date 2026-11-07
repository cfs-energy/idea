from ideasdk.aws.opensearch.opensearch_filters import (
    FreeTextFilter,
    SortFilter,
    SortOrder,
)


def test_sort_filter_names_the_order_opensearch_understands():
    # anything but asc or desc is ignored by OpenSearch without an error
    assert SortFilter('created_on', SortOrder.DESC).get_sort_filter() == (
        'created_on:desc'
    )


def test_free_text_filter_cannot_fail_on_special_characters():
    assert FreeTextFilter('run:1 (test/2').get_free_text_filter() == {
        'simple_query_string': {'query': 'run:1 (test/2', 'lenient': True}
    }
