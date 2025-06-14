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
from typing import Union, Dict, List, Optional

import ideavirtualdesktopcontroller
from ideadatamodel import (
    VirtualDesktopSession,
    VirtualDesktopBaseOS,
    VirtualDesktopSessionType,
    VirtualDesktopSessionState,
    VirtualDesktopWeekSchedule,
    Project,
    ListSessionsRequest,
    SocaFilter,
    ListSessionsResponse,
    SocaPaginator,
    SocaSortBy,
    SocaListingPayload,
    DayOfWeek,
)
from ideadatamodel import exceptions
from ideadatamodel.common.common_model import SocaSortOrder
from ideasdk.aws.opensearch.opensearchable_db import OpenSearchableDB

from ideasdk.utils import Utils, DateTimeUtils
from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_db import (
    VirtualDesktopScheduleDB,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
    VirtualDesktopServerDB,
)
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_notifiable_db import (
    VirtualDesktopNotifiableDB,
)
from ideavirtualdesktopcontroller.app.sessions import constants as sessions_constants


class VirtualDesktopSessionDB(VirtualDesktopNotifiableDB, OpenSearchableDB):
    DEFAULT_PAGE_SIZE = 10

    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        server_db: VirtualDesktopServerDB,
        software_stack_db: VirtualDesktopSoftwareStackDB,
        schedule_db: VirtualDesktopScheduleDB,
    ):
        self.context = context
        self._logger = self.context.logger('virtual-desktop-session-db')

        self._table_obj = None
        self._server_db = server_db
        self._software_stack_db = software_stack_db
        self._schedule_db = schedule_db
        self._ec2_client = self.context.aws().ec2()
        self._controller_utils = VirtualDesktopControllerUtils(self.context)
        self._ddb_client = self.context.aws().dynamodb_table()
        VirtualDesktopNotifiableDB.__init__(
            self, context=context, table_name=self.table_name, logger=self._logger
        )
        OpenSearchableDB.__init__(
            self,
            context=context,
            logger=self._logger,
            term_filter_map={
                sessions_constants.USER_SESSION_DB_FILTER_SOFTWARE_STACK_ID_KEY: 'software_stack.stack_id.raw',
                sessions_constants.USER_SESSION_DB_FILTER_BASE_OS_KEY: 'base_os.raw',
                sessions_constants.USER_SESSION_DB_FILTER_OWNER_KEY: 'owner.raw',
                sessions_constants.USER_SESSION_DB_FILTER_IDEA_SESSION_ID_KEY: 'idea_session_id.raw',
                sessions_constants.USER_SESSION_DB_FILTER_STATE_KEY: 'state.raw',
                sessions_constants.USER_SESSION_DB_FILTER_SESSION_TYPE_KEY: 'session_type.raw',
                sessions_constants.USER_SESSION_DB_FILTER_INSTANCE_TYPE_KEY: 'server.instance_type.raw',
            },
            date_range_filter_map={
                sessions_constants.USER_SESSION_DB_FILTER_CREATED_ON_KEY: 'created_on',
                sessions_constants.USER_SESSION_DB_FILTER_UPDATED_ON_KEY: 'updated_on',
            },
            default_page_size=self.DEFAULT_PAGE_SIZE,
        )

    @property
    def _table(self):
        if Utils.is_empty(self._table_obj):
            self._table_obj = self._ddb_client.Table(self.table_name)
        return self._table_obj

    @property
    def table_name(self) -> str:
        return f'{self.context.cluster_name()}.{self.context.module_id()}.controller.user-sessions'

    @property
    def schedule_db(self):
        return self._schedule_db

    @property
    def server_db(self):
        return self._server_db

    def initialize(self):
        exists = self.context.aws_util().dynamodb_check_table_exists(
            self.table_name, True
        )
        if not exists:
            self.context.aws_util().dynamodb_create_table(
                create_table_request={
                    'TableName': self.table_name,
                    'AttributeDefinitions': [
                        {
                            'AttributeName': sessions_constants.USER_SESSION_DB_HASH_KEY,
                            'AttributeType': 'S',
                        },
                        {
                            'AttributeName': sessions_constants.USER_SESSION_DB_RANGE_KEY,
                            'AttributeType': 'S',
                        },
                    ],
                    'KeySchema': [
                        {
                            'AttributeName': sessions_constants.USER_SESSION_DB_HASH_KEY,
                            'KeyType': 'HASH',
                        },
                        {
                            'AttributeName': sessions_constants.USER_SESSION_DB_RANGE_KEY,
                            'KeyType': 'RANGE',
                        },
                    ],
                    'BillingMode': 'PAY_PER_REQUEST',
                },
                wait=True,
            )

    def convert_db_dict_to_session_object(
        self, db_entry: Dict
    ) -> Optional[VirtualDesktopSession]:
        if Utils.is_empty(db_entry):
            return None

        return VirtualDesktopSession(
            owner=Utils.get_value_as_string(
                sessions_constants.USER_SESSION_DB_HASH_KEY, db_entry
            ),
            idea_session_id=Utils.get_value_as_string(
                sessions_constants.USER_SESSION_DB_RANGE_KEY, db_entry
            ),
            base_os=VirtualDesktopBaseOS(
                Utils.get_value_as_string(
                    sessions_constants.USER_SESSION_DB_BASE_OS_KEY, db_entry
                )
            ),
            created_on=Utils.to_datetime(
                Utils.get_value_as_int(
                    sessions_constants.USER_SESSION_DB_CREATED_ON_KEY, db_entry
                )
            ),
            updated_on=Utils.to_datetime(
                Utils.get_value_as_int(
                    sessions_constants.USER_SESSION_DB_UPDATED_ON_KEY, db_entry
                )
            ),
            locked=Utils.get_value_as_bool(
                sessions_constants.USER_SESSION_DB_SESSION_LOCKED_KEY, db_entry, False
            ),
            server=self._server_db.convert_db_entry_to_server_object(
                Utils.get_value_as_dict(
                    sessions_constants.USER_SESSION_DB_SERVER_KEY, db_entry
                )
            ),
            software_stack=self._software_stack_db.convert_db_dict_to_software_stack_object(
                Utils.get_value_as_dict(
                    sessions_constants.USER_SESSION_DB_SOFTWARE_STACK_KEY, db_entry
                )
            ),
            name=Utils.get_value_as_string(
                sessions_constants.USER_SESSION_DB_NAME_KEY, db_entry
            ),
            description=Utils.get_value_as_string(
                sessions_constants.USER_SESSION_DB_DESCRIPTION_KEY, db_entry, default=''
            ),
            dcv_session_id=Utils.get_value_as_string(
                sessions_constants.USER_SESSION_DB_DCV_SESSION_ID_KEY,
                db_entry,
                default='',
            ),
            type=VirtualDesktopSessionType[
                Utils.get_value_as_string(
                    sessions_constants.USER_SESSION_DB_SESSION_TYPE_KEY, db_entry
                )
            ],
            state=VirtualDesktopSessionState[
                Utils.get_value_as_string(
                    sessions_constants.USER_SESSION_DB_STATE_KEY, db_entry
                )
            ],
            hibernation_enabled=Utils.get_value_as_bool(
                sessions_constants.USER_SESSION_DB_HIBERNATION_KEY, db_entry
            ),
            is_launched_by_admin=Utils.get_value_as_bool(
                sessions_constants.USER_SESSION_DB_IS_LAUNCHED_BY_ADMIN_KEY, db_entry
            ),
            schedule=VirtualDesktopWeekSchedule(
                monday=self._schedule_db.convert_db_dict_to_schedule_object(
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                            DayOfWeek.MONDAY
                        ],
                        db_entry,
                    )
                ),
                tuesday=self._schedule_db.convert_db_dict_to_schedule_object(
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                            DayOfWeek.TUESDAY
                        ],
                        db_entry,
                    )
                ),
                wednesday=self._schedule_db.convert_db_dict_to_schedule_object(
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                            DayOfWeek.WEDNESDAY
                        ],
                        db_entry,
                    )
                ),
                thursday=self._schedule_db.convert_db_dict_to_schedule_object(
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                            DayOfWeek.THURSDAY
                        ],
                        db_entry,
                    )
                ),
                friday=self._schedule_db.convert_db_dict_to_schedule_object(
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                            DayOfWeek.FRIDAY
                        ],
                        db_entry,
                    )
                ),
                saturday=self._schedule_db.convert_db_dict_to_schedule_object(
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                            DayOfWeek.SATURDAY
                        ],
                        db_entry,
                    )
                ),
                sunday=self._schedule_db.convert_db_dict_to_schedule_object(
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                            DayOfWeek.SUNDAY
                        ],
                        db_entry,
                    )
                ),
            ),
            project=Project(
                project_id=Utils.get_value_as_string(
                    sessions_constants.USER_SESSION_DB_PROJECT_ID_KEY,
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_PROJECT_KEY, db_entry, {}
                    ),
                    None,
                ),
                name=Utils.get_value_as_string(
                    sessions_constants.USER_SESSION_DB_PROJECT_NAME_KEY,
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_PROJECT_KEY, db_entry, {}
                    ),
                    None,
                ),
                title=Utils.get_value_as_string(
                    sessions_constants.USER_SESSION_DB_PROJECT_TITLE_KEY,
                    Utils.get_value_as_dict(
                        sessions_constants.USER_SESSION_DB_PROJECT_KEY, db_entry, {}
                    ),
                    None,
                ),
            ),
        )

    def convert_session_object_to_db_dict(self, session: VirtualDesktopSession) -> Dict:
        if Utils.is_empty(session):
            return {}

        if Utils.is_empty(session.schedule):
            session.schedule = VirtualDesktopWeekSchedule()

        db_dict = {
            sessions_constants.USER_SESSION_DB_HASH_KEY: session.owner,
            sessions_constants.USER_SESSION_DB_RANGE_KEY: session.idea_session_id,
            sessions_constants.USER_SESSION_DB_CREATED_ON_KEY: Utils.to_milliseconds(
                session.created_on
            ),
            sessions_constants.USER_SESSION_DB_UPDATED_ON_KEY: Utils.to_milliseconds(
                session.updated_on
            ),
            sessions_constants.USER_SESSION_DB_SERVER_KEY: self._server_db.convert_server_object_to_db_dict(
                session.server
            ),
            sessions_constants.USER_SESSION_DB_SOFTWARE_STACK_KEY: self._software_stack_db.convert_software_stack_object_to_db_dict(
                session.software_stack
            ),
            sessions_constants.USER_SESSION_DB_BASE_OS_KEY: session.software_stack.base_os,
            sessions_constants.USER_SESSION_DB_NAME_KEY: session.name,
            sessions_constants.USER_SESSION_DB_DESCRIPTION_KEY: session.description,
            sessions_constants.USER_SESSION_DB_DCV_SESSION_ID_KEY: session.dcv_session_id,
            sessions_constants.USER_SESSION_DB_SESSION_TYPE_KEY: session.type,
            sessions_constants.USER_SESSION_DB_STATE_KEY: session.state,
            sessions_constants.USER_SESSION_DB_SESSION_LOCKED_KEY: False
            if Utils.is_empty(session.locked)
            else session.locked,
            sessions_constants.USER_SESSION_DB_HIBERNATION_KEY: session.hibernation_enabled,
            sessions_constants.USER_SESSION_DB_IS_LAUNCHED_BY_ADMIN_KEY: session.is_launched_by_admin,
            sessions_constants.USER_SESSION_DB_PROJECT_KEY: {
                sessions_constants.USER_SESSION_DB_PROJECT_ID_KEY: session.project.project_id,
                sessions_constants.USER_SESSION_DB_PROJECT_TITLE_KEY: session.project.title,
                sessions_constants.USER_SESSION_DB_PROJECT_NAME_KEY: session.project.name,
            },
            sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                DayOfWeek.MONDAY
            ]: self._schedule_db.convert_schedule_object_to_db_dict(
                session.schedule.monday
            ),
            sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                DayOfWeek.TUESDAY
            ]: self._schedule_db.convert_schedule_object_to_db_dict(
                session.schedule.tuesday
            ),
            sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                DayOfWeek.WEDNESDAY
            ]: self._schedule_db.convert_schedule_object_to_db_dict(
                session.schedule.wednesday
            ),
            sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                DayOfWeek.THURSDAY
            ]: self._schedule_db.convert_schedule_object_to_db_dict(
                session.schedule.thursday
            ),
            sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                DayOfWeek.FRIDAY
            ]: self._schedule_db.convert_schedule_object_to_db_dict(
                session.schedule.friday
            ),
            sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                DayOfWeek.SATURDAY
            ]: self._schedule_db.convert_schedule_object_to_db_dict(
                session.schedule.saturday
            ),
            sessions_constants.USER_SESSION_DB_SCHEDULE_KEYS[
                DayOfWeek.SUNDAY
            ]: self._schedule_db.convert_schedule_object_to_db_dict(
                session.schedule.sunday
            ),
        }

        return db_dict

    def convert_session_object_to_index_dict(
        self, session: VirtualDesktopSession
    ) -> Dict:
        db_dict = self.convert_session_object_to_db_dict(session)
        response = self._ec2_client.describe_instances(
            InstanceIds=[session.server.instance_id]
        )
        reservation = Utils.get_value_as_list('Reservations', response, default=[])
        if Utils.is_empty(reservation):
            return db_dict

        db_dict['server']['reservation_id'] = Utils.get_value_as_string(
            'ReservationId', reservation[0], 'N/A'
        )
        instances = Utils.get_value_as_list('Instances', reservation[0], default=[])

        if Utils.is_empty(instances):
            return db_dict
        instance = instances[0]
        db_dict['server']['private_ip'] = Utils.get_value_as_string(
            'PrivateIpAddress', instance, default=None
        )
        db_dict['server']['public_ip'] = Utils.get_value_as_string(
            'PublicIpAddress', instance, default=None
        )

        launch_time = Utils.get_value_as_string('LaunchTime', instance, default=None)
        if Utils.is_not_empty(launch_time):
            launch_time = Utils.to_milliseconds(
                DateTimeUtils.to_utc_datetime_from_iso_format(launch_time)
            )
            db_dict['server']['launch_time'] = launch_time

        db_dict['server']['tags'] = []
        tags = Utils.get_value_as_list('Tags', instance, default=[])
        for tag in tags:
            db_dict['server']['tags'].append(
                {
                    'key': Utils.get_value_as_string('Key', tag, default=None),
                    'value': Utils.get_value_as_string('Value', tag, default=None),
                }
            )

        placement = Utils.get_value_as_dict('Placement', instance, default={})
        db_dict['server']['availability_zone'] = Utils.get_value_as_string(
            'AvailabilityZone', placement, default=None
        )
        db_dict['server']['tenancy'] = Utils.get_value_as_string(
            'Tenancy', placement, default=None
        )

        instance_type = Utils.get_value_as_string(
            'InstanceType', instance, default=None
        )
        db_dict['server']['instance_type'] = instance_type

        instance_type_info = self._controller_utils.get_instance_type_info(
            instance_type
        )
        default_vcpus = Utils.get_value_as_int(
            'DefaultVCpus',
            Utils.get_value_as_dict('VCpuInfo', instance_type_info, default={}),
            default=0,
        )
        memory_size_in_mb = Utils.get_value_as_int(
            'SizeInMiB',
            Utils.get_value_as_dict('MemoryInfo', instance_type_info, default={}),
            default=0,
        )
        total_gpu_memory_in_mb = Utils.get_value_as_int(
            'TotalGpuMemoryInMiB',
            Utils.get_value_as_dict('GpuInfo', instance_type_info, default={}),
            default=0,
        )

        db_dict['server']['default_vcpus'] = default_vcpus
        db_dict['server']['memory_size_in_mb'] = memory_size_in_mb
        db_dict['server']['total_gpu_memory_in_mb'] = total_gpu_memory_in_mb
        return db_dict

    def convert_index_dict_to_session_object(
        self, index_entry: Dict
    ) -> VirtualDesktopSession:
        session = self.convert_db_dict_to_session_object(index_entry)
        server_dict = Utils.get_value_as_dict('server', index_entry, {})
        session.server.private_ip = Utils.get_value_as_string(
            'private_ip', server_dict, None
        )
        session.server.public_ip = Utils.get_value_as_string(
            'public_ip', server_dict, None
        )
        return session

    def create(self, session: VirtualDesktopSession) -> VirtualDesktopSession:
        db_entry = self.convert_session_object_to_db_dict(session)
        db_entry[sessions_constants.USER_SESSION_DB_CREATED_ON_KEY] = (
            Utils.current_time_ms()
        )
        db_entry[sessions_constants.USER_SESSION_DB_UPDATED_ON_KEY] = (
            Utils.current_time_ms()
        )
        self._table.put_item(Item=db_entry)
        self.trigger_create_event(
            db_entry[sessions_constants.USER_SESSION_DB_HASH_KEY],
            db_entry[sessions_constants.USER_SESSION_DB_RANGE_KEY],
            new_entry=db_entry,
        )
        return self.convert_db_dict_to_session_object(db_entry)

    def update(self, session: VirtualDesktopSession) -> VirtualDesktopSession:
        db_entry = self.convert_session_object_to_db_dict(session)
        db_entry[sessions_constants.USER_SESSION_DB_UPDATED_ON_KEY] = (
            Utils.current_time_ms()
        )
        update_expression_tokens = []
        expression_attr_names = {}
        expression_attr_values = {}

        for key, value in db_entry.items():
            if key in {
                sessions_constants.USER_SESSION_DB_HASH_KEY,
                sessions_constants.USER_SESSION_DB_RANGE_KEY,
                sessions_constants.USER_SESSION_DB_CREATED_ON_KEY,
            }:
                continue
            update_expression_tokens.append(f'#{key} = :{key}')
            expression_attr_names[f'#{key}'] = key
            expression_attr_values[f':{key}'] = value

        result = self._table.update_item(
            Key={
                sessions_constants.USER_SESSION_DB_HASH_KEY: db_entry[
                    sessions_constants.USER_SESSION_DB_HASH_KEY
                ],
                sessions_constants.USER_SESSION_DB_RANGE_KEY: db_entry[
                    sessions_constants.USER_SESSION_DB_RANGE_KEY
                ],
            },
            UpdateExpression='SET ' + ', '.join(update_expression_tokens),
            ExpressionAttributeNames=expression_attr_names,
            ExpressionAttributeValues=expression_attr_values,
            ReturnValues='ALL_OLD',
        )

        old_db_entry = result['Attributes']
        self.trigger_update_event(
            db_entry[sessions_constants.USER_SESSION_DB_HASH_KEY],
            db_entry[sessions_constants.USER_SESSION_DB_RANGE_KEY],
            old_entry=old_db_entry,
            new_entry=db_entry,
        )
        return self.convert_db_dict_to_session_object(db_entry)

    def delete(self, session: VirtualDesktopSession):
        if Utils.is_empty(session.idea_session_id) or Utils.is_empty(session.owner):
            raise exceptions.invalid_params('idea_session_id and owner is required')

        result = self._table.delete_item(
            Key={
                sessions_constants.USER_SESSION_DB_HASH_KEY: session.owner,
                sessions_constants.USER_SESSION_DB_RANGE_KEY: session.idea_session_id,
            },
            ReturnValues='ALL_OLD',
        )
        old_db_entry = result['Attributes']
        self.trigger_delete_event(
            old_db_entry[sessions_constants.USER_SESSION_DB_HASH_KEY],
            old_db_entry[sessions_constants.USER_SESSION_DB_RANGE_KEY],
            deleted_entry=old_db_entry,
        )

    def get_from_db(
        self, idea_session_owner: str, idea_session_id: str
    ) -> Optional[VirtualDesktopSession]:
        if Utils.is_empty(idea_session_owner) or Utils.is_empty(idea_session_id):
            self._logger.error(
                f'invalid values for owner: {idea_session_owner} and/or idea_session_id: {idea_session_id}'
            )
            session_db_entry = None
        else:
            try:
                self._logger.info(
                    f'Getting DB entry for owner: {idea_session_owner} and idea_session_id: {idea_session_id}'
                )
                result = self._table.get_item(
                    Key={
                        sessions_constants.USER_SESSION_DB_HASH_KEY: idea_session_owner,
                        sessions_constants.USER_SESSION_DB_RANGE_KEY: idea_session_id,
                    }
                )
                session_db_entry = Utils.get_value_as_dict('Item', result)
            except self._ddb_client.exceptions.ResourceNotFoundException as _:
                # in this case we simply need to return None since the resource was not found
                return None
            except Exception as e:
                self._logger.exception(e)
                raise e
        return self.convert_db_dict_to_session_object(session_db_entry)

    def get_from_index(
        self, idea_session_id: str
    ) -> Union[VirtualDesktopSession, None]:
        request = ListSessionsRequest()
        request.add_filter(
            SocaFilter(
                key=sessions_constants.USER_SESSION_DB_FILTER_IDEA_SESSION_ID_KEY,
                value=idea_session_id,
            )
        )
        response = self.list_from_index(request)
        if Utils.is_empty(response.listing):
            return None
        return response.listing[0]

    def list_from_index(self, options: ListSessionsRequest) -> ListSessionsResponse:
        response = self._list_from_index(options)

        sessions: List[VirtualDesktopSession] = []
        session_responses = Utils.get_value_as_list(
            'hits', Utils.get_value_as_dict('hits', response, default={}), default=[]
        )
        for session_response in session_responses:
            index_object = Utils.get_value_as_dict(
                '_source', session_response, default={}
            )
            sessions.append(self.convert_index_dict_to_session_object(index_object))

        return ListSessionsResponse(
            listing=sessions,
            paginator=SocaPaginator(
                page_size=options.paginator.page_size
                if Utils.is_not_empty(options.paginator)
                and Utils.is_not_empty(options.paginator.page_size)
                else self.DEFAULT_PAGE_SIZE,
                start=options.paginator.start
                if Utils.is_not_empty(options.paginator)
                and Utils.is_not_empty(options.paginator.start)
                else 0,
                total=Utils.get_value_as_int(
                    'value',
                    Utils.get_value_as_dict(
                        'total', Utils.get_value_as_dict('hits', response, {}), {}
                    ),
                    0,
                ),
            ),
            filters=options.filters,
            date_range=options.date_range,
            sort_by=options.sort_by,
        )

    def get_default_sort(self) -> SocaSortBy:
        return SocaSortBy(key='created_on', order=SocaSortOrder.ASC)

    def get_index_name(self) -> str:
        return f'{self.context.config().get_string("virtual-desktop-controller.opensearch.dcv_session.alias", required=True)}-{self.context.sessions_template_version}'

    def get_session_count_for_user(self, username: str) -> int:
        count_request = {
            'Select': 'COUNT',
            'KeyConditions': {
                sessions_constants.USER_SESSION_DB_HASH_KEY: {
                    'AttributeValueList': [username],
                    'ComparisonOperator': 'EQ',
                },
            },
        }

        response = self._table.query(**count_request)
        return Utils.get_value_as_int('Count', response)

    def list_all_from_db(self, request: ListSessionsRequest) -> SocaListingPayload:
        list_request = {}

        exclusive_start_key = None
        if Utils.is_not_empty(request.cursor):
            exclusive_start_key = Utils.from_json(Utils.base64_decode(request.cursor))

        if exclusive_start_key is not None:
            list_request['ExclusiveStartKey'] = exclusive_start_key

        list_result = self._table.scan(**list_request)
        session_entries = Utils.get_value_as_list('Items', list_result, [])
        result = []
        for session_entry in session_entries:
            result.append(self.convert_db_dict_to_session_object(session_entry))

        response_cursor = None
        exclusive_start_key = Utils.get_any_value('LastEvaluatedKey', list_result)
        if exclusive_start_key is not None:
            response_cursor = Utils.base64_encode(Utils.to_json(exclusive_start_key))

        return SocaListingPayload(
            listing=result,
            paginator=SocaPaginator(
                page_size=request.page_size, cursor=response_cursor
            ),
        )

    def list_all_for_user(
        self, request: ListSessionsRequest, username: str
    ) -> ListSessionsResponse:
        if Utils.is_empty(request):
            request = ListSessionsRequest()

        if Utils.is_empty(request.filters):
            request.filters = []

        request.filters.append(
            SocaFilter(
                key=sessions_constants.USER_SESSION_DB_FILTER_OWNER_KEY, value=username
            )
        )
        return self.list_from_index(request)

    def _list_from_index(self, options: ListSessionsRequest) -> Dict:
        if Utils.is_not_empty(options.filters):
            new_filters: List[SocaFilter] = []
            for listing_filter in options.filters:
                if (
                    listing_filter.key
                    == sessions_constants.USER_SESSION_DB_FILTER_BASE_OS_KEY
                    and listing_filter.value == '$all'
                ):
                    # needs to see all OS's no point adding a filter for OS at this point
                    continue
                elif (
                    listing_filter.key
                    == sessions_constants.USER_SESSION_DB_FILTER_BASE_OS_KEY
                    and listing_filter.value == 'linux'
                ):
                    listing_filter.value = [
                        VirtualDesktopBaseOS.AMAZON_LINUX2.value,
                        VirtualDesktopBaseOS.AMAZON_LINUX2023.value,
                        VirtualDesktopBaseOS.RHEL8.value,
                        VirtualDesktopBaseOS.RHEL9.value,
                        VirtualDesktopBaseOS.ROCKY8.value,
                        VirtualDesktopBaseOS.ROCKY9.value,
                        VirtualDesktopBaseOS.UBUNTU2204.value,
                        VirtualDesktopBaseOS.UBUNTU2404.value,
                    ]
                elif (
                    listing_filter.key
                    == sessions_constants.USER_SESSION_DB_FILTER_BASE_OS_KEY
                    and listing_filter.value == 'windows'
                ):
                    listing_filter.value = [
                        VirtualDesktopBaseOS.WINDOWS.value,
                        VirtualDesktopBaseOS.WINDOWS2019.value,
                        VirtualDesktopBaseOS.WINDOWS2022.value,
                        VirtualDesktopBaseOS.WINDOWS2025.value,
                    ]

                new_filters.append(listing_filter)

            options.filters = new_filters

        return self.list_from_opensearch(options)
