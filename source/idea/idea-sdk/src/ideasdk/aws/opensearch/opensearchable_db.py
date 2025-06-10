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
from abc import abstractmethod
from logging import Logger
from typing import Dict, Optional

from ideadatamodel import SocaListingPayload, SocaSortOrder, SocaSortBy
from ideasdk.aws.opensearch.aws_opensearch_client import AwsOpenSearchClient
from ideasdk.aws.opensearch.opensearch_filters import (
    FreeTextFilter,
    TermFilter,
    RangeFilter,
    SortOrder,
    SortFilter,
)
from ideasdk.context import SocaContext
from ideasdk.utils import Utils
from ideasdk.utils.datetime_utils import DateTimeUtils


class OpenSearchableDB:
    FREE_TEXT_FILTER_KEY = '$all'

    def __init__(
        self,
        context: SocaContext,
        logger: Logger,
        term_filter_map: Optional[Dict] = None,
        date_range_filter_map: Optional[Dict] = None,
        default_page_size: int = 10,
        free_text_search_support: bool = True,
    ):
        self.context = context
        self._logger = logger
        self._term_filter_map = term_filter_map
        self._date_range_filter_map = date_range_filter_map
        self._default_page_size = default_page_size
        self._os_client = AwsOpenSearchClient(self.context)
        self._free_text_search_support = free_text_search_support

    def list_from_opensearch(self, options: SocaListingPayload):
        # documentation to read - https://opensearch.org/docs/latest/opensearch/query-dsl/

        term_filters = []
        free_text_filter = None
        if Utils.is_not_empty(options.filters):
            for listing_filter in options.filters:
                if Utils.is_empty(listing_filter.key):
                    continue

                if Utils.is_empty(listing_filter.value):
                    continue

                if listing_filter.key == self.FREE_TEXT_FILTER_KEY:
                    # this is free text filter
                    if self._free_text_search_support:
                        free_text_filter = FreeTextFilter(text=listing_filter.value)
                    continue

                if listing_filter.key not in self._term_filter_map.keys():
                    continue

                term_filters.append(
                    TermFilter(
                        key=self._term_filter_map[listing_filter.key],
                        value=listing_filter.value,
                    )
                )

        # Debug date range filter conditions
        self._logger.debug(
            f'Date range debugging - options.date_range: {options.date_range}'
        )
        self._logger.debug(
            f'Date range debugging - self._date_range_filter_map: {self._date_range_filter_map}'
        )

        if Utils.is_not_empty(options.date_range):
            self._logger.debug(
                f'Date range debugging - date_range.key: {options.date_range.key}'
            )
            if Utils.is_not_empty(self._date_range_filter_map):
                self._logger.debug(
                    f'Date range debugging - filter map keys: {self._date_range_filter_map.keys()}'
                )
                self._logger.debug(
                    f'Date range debugging - key in map: {options.date_range.key in self._date_range_filter_map.keys()}'
                )

        range_filter = None
        if Utils.is_not_empty(options.date_range) and Utils.is_not_empty(
            self._date_range_filter_map
        ):
            date_range = options.date_range

            # Fix for missing key in date range
            if Utils.is_empty(date_range.key) and (
                Utils.is_not_empty(date_range.start)
                or Utils.is_not_empty(date_range.end)
            ):
                # Use the first available key in the filter map as default
                default_key = next(iter(self._date_range_filter_map.keys()))
                self._logger.debug(
                    f'Date range key is None, setting default key: {default_key}'
                )
                date_range.key = default_key

            if date_range.key in self._date_range_filter_map.keys():
                start_datetime = (
                    DateTimeUtils.to_utc_datetime_from_iso_format(date_range.start)
                    if isinstance(date_range.start, str)
                    else date_range.start
                )
                end_datetime = (
                    DateTimeUtils.to_utc_datetime_from_iso_format(date_range.end)
                    if isinstance(date_range.end, str)
                    else date_range.end
                )

                start_ms = Utils.to_milliseconds(start_datetime)
                end_ms = Utils.to_milliseconds(end_datetime)

                self._logger.debug(
                    f'Date range filter - Original: {date_range.start} to {date_range.end}'
                )
                self._logger.debug(
                    f'Date range filter - Parsed datetimes: {start_datetime} to {end_datetime}'
                )
                self._logger.debug(
                    f'Date range filter - Converted to MS: {start_ms} to {end_ms}'
                )
                self._logger.debug(
                    f'Date range filter - Field key: {self._date_range_filter_map[date_range.key]}'
                )

                range_filter = RangeFilter(
                    key=self._date_range_filter_map[date_range.key],
                    start=f'{start_ms}',
                    end=f'{end_ms}',
                )

        if Utils.is_empty(options.sort_by):
            options.sort_by = self.get_default_sort()

        sort_filter = SortFilter(
            key=options.sort_by.key,
            order=SortOrder.DESC
            if options.sort_by.order == SocaSortOrder.DESC
            else SortOrder.ASC,
        )

        response = self._os_client.search(
            index=self.get_index_name(),
            term_filters=term_filters,
            range_filter=range_filter,
            sort_filter=sort_filter,
            free_text_filter=free_text_filter,
            start_from=options.paginator.start
            if Utils.is_not_empty(options.paginator)
            and Utils.is_not_empty(options.paginator.start)
            else 0,
            size=options.paginator.page_size
            if Utils.is_not_empty(options.paginator)
            and Utils.is_not_empty(options.paginator.page_size)
            else self._default_page_size,
        )
        return response

    @abstractmethod
    def get_index_name(self) -> str: ...

    @abstractmethod
    def get_default_sort(self) -> SocaSortBy: ...
