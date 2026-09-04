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

from typing import List, Optional

import ideavirtualdesktopcontroller
from ideadatamodel import (
    BaseSoftwareStackAmiRefreshResult,
    ListSoftwareStackRequest,
    SocaPaginator,
    VirtualDesktopSoftwareStack,
    VirtualDesktopSession,
)
from ideasdk.analytics.analytics_service import (
    AnalyticsEntry,
    EntryAction,
    EntryContent,
)
from ideasdk.aws.image_builds import is_built_image
from ideasdk.aws.stock_amis import (
    find_latest_image,
    get_ami_pattern_for_stack,
    stack_architecture,
    stock_unsupported_reason,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.events.events_utils import EventsUtils
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_db import (
    VirtualDesktopSSMCommandsDB,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)


def expected_architecture(software_stack: VirtualDesktopSoftwareStack) -> Optional[str]:
    """the stack's declared architecture, else the one its base stack id encodes"""
    declared = software_stack.architecture
    if declared is not None:
        return getattr(declared, 'value', declared)
    return stack_architecture(software_stack.stack_id)


class VirtualDesktopSoftwareStackUtils:
    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        db: VirtualDesktopSoftwareStackDB,
    ):
        self.context = context
        self._software_stack_db = db
        self._controller_utils = VirtualDesktopControllerUtils(self.context)
        self._ssm_commands_db = VirtualDesktopSSMCommandsDB(self.context)
        self.events_utils = EventsUtils(context=self.context)
        self._logger = context.logger('virtual-desktop-software-stack-utils')

    def refresh_base_software_stack_amis(
        self,
        stack_ids: Optional[List[str]] = None,
    ) -> List[BaseSoftwareStackAmiRefreshResult]:
        """
        resolve the newest AMI for every ss-base-* software stack, update the rows that
        changed and reindex them. server-side twin of `ideactl update-base-stacks`. a
        requested id that is not an existing ss-base-* row returns an error result.
        """
        ec2_client = self.context.aws().ec2()
        requested = None if stack_ids is None else set(stack_ids)
        matched: set = set()
        results: List[BaseSoftwareStackAmiRefreshResult] = []
        request = ListSoftwareStackRequest()
        request.disabled_also = True
        response = self._software_stack_db.list_all_from_db(request)
        while True:
            for software_stack in response.listing:
                stack_id = software_stack.stack_id
                if Utils.is_empty(stack_id) or not stack_id.startswith('ss-base-'):
                    continue
                if requested is not None and stack_id not in requested:
                    continue
                matched.add(stack_id)
                result = BaseSoftwareStackAmiRefreshResult(
                    stack_id=stack_id, old_ami=software_stack.ami_id
                )
                results.append(result)
                try:
                    _, ami_pattern, owners = get_ami_pattern_for_stack(stack_id)
                    if ami_pattern is None:
                        result.status = 'error'
                        result.message = 'no AMI pattern for this stack id'
                        continue
                    parts = stack_id[len('ss-base-') :].split('-')
                    ami_type = parts[0] if len(parts) >= 2 else None
                    unsupported = stock_unsupported_reason(
                        ami_type, ec2_client.meta.region_name
                    )
                    if unsupported:
                        result.status = 'error'
                        result.message = unsupported
                        continue
                    latest = find_latest_image(
                        ec2_client, ami_pattern, owners, self._logger, ami_type
                    )
                    if not latest:
                        result.status = 'error'
                        result.message = 'no matching AMI found'
                        continue
                    latest_ami_id = latest['ImageId']
                    expected = expected_architecture(software_stack)
                    found = latest.get('Architecture')
                    if expected and found and found != expected:
                        # windows patterns carry no architecture token, so the newest
                        # match can be the other architecture; never repoint across it
                        result.status = 'error'
                        result.message = f'newest image {latest_ami_id} is {found}, the stack is {expected}; not updated'
                        continue
                    # the newest stock image always becomes the base the next build
                    # starts from. ami_id follows it only while it is itself a stock
                    # image, so a refresh can never undo a build
                    launches_from_build = is_built_image(
                        ec2_client, software_stack.ami_id
                    )
                    base_changed = software_stack.base_ami_id != latest_ami_id
                    if launches_from_build:
                        if not base_changed:
                            result.status = 'up_to_date'
                            result.message = f'launches from built image {software_stack.ami_id}; base {latest_ami_id} is already the newest'
                            continue
                        software_stack.base_ami_id = latest_ami_id
                        updated_stack = self._software_stack_db.update(software_stack)
                        result.new_base_ami = latest_ami_id
                        result.status = 'base_updated'
                        result.message = (
                            f'base image updated to {latest_ami_id}; stack still launches from '
                            f'built image {software_stack.ami_id}; rebuild to pick up the new base'
                        )
                    else:
                        if latest_ami_id == software_stack.ami_id:
                            result.status = 'up_to_date'
                            continue
                        software_stack.ami_id = latest_ami_id
                        software_stack.base_ami_id = latest_ami_id
                        updated_stack = self._software_stack_db.update(software_stack)
                        # the row is committed from here on: the result says so even if
                        # the index write below fails, which the next start reconciles
                        result.new_ami = latest_ami_id
                        result.new_base_ami = latest_ami_id
                        result.status = 'updated'
                    try:
                        self.index_software_stack_entry_to_opensearch(
                            software_stack=updated_stack
                        )
                    except Exception as e:
                        self._logger.error(
                            f'{stack_id}: row updated to {latest_ami_id} but the index write failed: {e}'
                        )
                        result.message = (
                            (result.message + '; ' if result.message else '')
                            + 'the search index write failed and the index reconciles at the next controller start'
                        )
                except Exception as e:
                    self._logger.error(f'failed to refresh AMI for {stack_id}: {e}')
                    result.status = 'error'
                    result.message = str(e)
            if Utils.is_empty(response.cursor):
                break
            request.paginator = response.paginator
            response = self._software_stack_db.list_all_from_db(request)
        if requested is not None:
            for missing_id in sorted(requested - matched):
                results.append(
                    BaseSoftwareStackAmiRefreshResult(
                        stack_id=missing_id,
                        status='error',
                        message='not a refreshable base stack',
                    )
                )
        return results

    def create_software_stack(
        self, software_stack: VirtualDesktopSoftwareStack
    ) -> VirtualDesktopSoftwareStack:
        software_stack.stack_id = Utils.uuid()
        return self._software_stack_db.create(software_stack)

    def create_software_stack_from_session_when_ready(
        self,
        session: VirtualDesktopSession,
        new_software_stack: VirtualDesktopSoftwareStack,
    ) -> VirtualDesktopSoftwareStack:
        pass

    def delete_software_stack_entry_from_opensearch(self, software_stack_id: str):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.software_stack.alias", required=True)}-{self.context.software_stack_template_version}'
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=software_stack_id,
                entry_action=EntryAction.DELETE_ENTRY,
                entry_content=EntryContent(index_id=index_name),
            )
        )

    def update_software_stack_entry_to_opensearch(
        self, software_stack: VirtualDesktopSoftwareStack
    ):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.software_stack.alias", required=True)}-{self.context.software_stack_template_version}'
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=software_stack.stack_id,
                entry_action=EntryAction.UPDATE_ENTRY,
                entry_content=EntryContent(
                    index_id=index_name,
                    entry_record=self._software_stack_db.convert_software_stack_object_to_index_dict(
                        software_stack
                    ),
                ),
            )
        )

    def index_software_stack_entry_to_opensearch(
        self, software_stack: VirtualDesktopSoftwareStack
    ):
        index_name = f'{self.context.config().get_string("virtual-desktop-controller.opensearch.software_stack.alias", required=True)}-{self.context.software_stack_template_version}'
        index_dict = (
            self._software_stack_db.convert_software_stack_object_to_index_dict(
                software_stack
            )
        )
        self.context.analytics_service().post_entry(
            AnalyticsEntry(
                entry_id=software_stack.stack_id,
                entry_action=EntryAction.CREATE_ENTRY,
                entry_content=EntryContent(
                    index_id=index_name, entry_record=index_dict
                ),
            )
        )

    def reindex_from_db(self):
        """
        Rebuild the software stack search index from the DynamoDB table.

        The upgrade preflight deletes end of life stacks from a container with no route
        to OpenSearch, leaving index entries for stacks that no longer exist and a portal
        listing stacks it cannot act on. Reconciling both ways keeps the index self
        healing without an operator running ideactl reindex-software-stacks.
        """
        # the index snapshot must be the older read: a stack created in the gap is then
        # re-indexed (harmless) rather than dropped as stale, and one deleted in the gap
        # is never re-indexed as a ghost
        indexed_stack_ids = self._indexed_stack_ids()
        request = ListSoftwareStackRequest(disabled_also=True)
        software_stacks: List[VirtualDesktopSoftwareStack] = []
        while True:
            response = self._software_stack_db.list_all_from_db(request)
            software_stacks.extend(response.listing or [])
            if Utils.is_empty(response.cursor):
                break
            request.paginator = response.paginator

        already_indexed = set(indexed_stack_ids)
        live_stack_ids = {software_stack.stack_id for software_stack in software_stacks}

        missing = [
            software_stack
            for software_stack in software_stacks
            if software_stack.stack_id not in already_indexed
        ]
        stale = [
            stack_id for stack_id in indexed_stack_ids if stack_id not in live_stack_ids
        ]
        refreshed = [
            software_stack
            for software_stack in software_stacks
            if software_stack.stack_id in already_indexed
        ]

        for stack_id in stale:
            self.delete_software_stack_entry_from_opensearch(stack_id)
        for software_stack in missing:
            self.index_software_stack_entry_to_opensearch(software_stack=software_stack)
        # rows present in both stores are rewritten too: the admin container edits
        # DynamoDB directly with no route to this socket, so the index doc keeps the old
        # values until something rewrites it. an update rather than delete and create,
        # so the portal never sees a blank listing while it lands.
        for software_stack in refreshed:
            self.update_software_stack_entry_to_opensearch(
                software_stack=software_stack
            )

        self._logger.info(
            f'reconciled the software stack index against {len(software_stacks)} table '
            f'row(s): deleted {len(stale)} ghost(s), indexed {len(missing)} missing, '
            f'refreshed {len(refreshed)} existing'
        )

    def _indexed_stack_ids(self) -> List[str]:
        page_size = 100
        stack_ids: List[str] = []
        while True:
            listing = (
                self._software_stack_db.list_from_index(
                    ListSoftwareStackRequest(
                        paginator=SocaPaginator(
                            start=len(stack_ids), page_size=page_size
                        )
                    )
                ).listing
                or []
            )
            stack_ids.extend(software_stack.stack_id for software_stack in listing)
            if len(listing) < page_size:
                return stack_ids
