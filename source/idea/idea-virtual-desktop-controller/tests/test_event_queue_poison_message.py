"""
One failing desktop event must not hold the whole controller queue.

A message whose handler raises is not deleted, so SQS redelivers it indefinitely and
every other event in its message group waits behind it. The poll loop bounds the
redelivery, and the scheduled-stop handler no longer raises for a host SSM does not
know about.
"""

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    VirtualDesktopBaseOS,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    errorcodes,
)
from ideadatamodel.exceptions import SocaException
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
    VirtualDesktopEventType,
)
from ideavirtualdesktopcontroller.app.events.handlers.idea_session_state_event_handlers.idea_session_scheduled_stop_event_handler import (
    IDEASessionScheduledStopEventHandler,
)
from ideavirtualdesktopcontroller.app.events.service.events_handler_thread import (
    EventsHandlerThread,
)

QUEUE_URL = 'https://sqs.test.amazonaws.com/1234/cluster-vdc-events.fifo'
STOP_EVENT = VirtualDesktopEventType.IDEA_SESSION_SCHEDULED_STOP_EVENT
CONTROLLER_ROLE_ID = 'AROACONTROLLER'


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get_string(self, key, **_kwargs):
        return self.values.get(key)

    def get_int(self, key, default=None):
        return self.values.get(key, default)


class FakeContext:
    """context.aws().sqs() and context.config(), which is all the loop reaches for."""

    def __init__(self, sqs=None, values=None):
        self._sqs = sqs
        self._config = FakeConfig(values or {})

    def aws(self):
        return self

    def sqs(self):
        return self._sqs

    def config(self):
        return self._config


class FakeSQS:
    def __init__(self, batches):
        self.batches = list(batches)
        self.deleted = []

    def receive_message(self, **_kwargs):
        if not self.batches:
            return {}
        return {'Messages': self.batches.pop(0)}

    def delete_message_batch(self, **kwargs):
        self.deleted.extend(entry['Id'] for entry in kwargs['Entries'])
        return {}


class FakeHandler:
    """Raises for the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times=0, error=None):
        self.fail_times = fail_times
        self.error = error or RuntimeError('SSM has no such instance')
        self.calls = []

    def handle_event(self, message_id, sender_id, event):
        self.calls.append(message_id)
        if len(self.calls) <= self.fail_times:
            raise self.error

    def log_info(self, message_id, message):
        pass

    def log_exception(self, message_id, exception):
        pass


def a_message(message_id, idea_session_id='sess-1', receive_count=1):
    body = Utils.to_json(
        {
            'event_group_id': idea_session_id,
            'event_type': STOP_EVENT.value,
            'detail': {
                'idea_session_id': idea_session_id,
                'idea_session_owner': 'someuser',
            },
        }
    )
    return {
        'MessageId': message_id,
        'Body': body,
        'MD5OfBody': Utils.md5(body),
        'ReceiptHandle': f'receipt-{message_id}-{receive_count}',
        'Attributes': {
            'MessageGroupId': idea_session_id,
            'SenderId': f'{CONTROLLER_ROLE_ID}:vdc',
            'ApproximateReceiveCount': str(receive_count),
        },
    }


def build_thread(handler, sqs, max_receive_count=3):
    # the real constructor builds every event handler and its AWS clients; the poll
    # loop only needs the queue, the config and the handler map
    thread = object.__new__(EventsHandlerThread)
    thread.context = FakeContext(
        sqs=sqs,
        values={
            'virtual-desktop-controller.events_sqs_queue_url': QUEUE_URL,
            'virtual-desktop-controller.events.max_receive_count': max_receive_count,
        },
    )
    thread._logger = Mock()
    thread.NUM_OF_MESSAGES = 10
    thread.WAIT_TIME = 0
    thread.EVENT_HANDLER_MAP = {STOP_EVENT: handler}
    return thread


def test_a_failing_message_is_left_on_the_queue_for_the_first_two_receives():
    handler = FakeHandler(fail_times=99)
    sqs = FakeSQS(
        [[a_message('msg-1', receive_count=1)], [a_message('msg-1', receive_count=2)]]
    )
    thread = build_thread(handler, sqs)

    thread.poll_and_process_queue()
    thread.poll_and_process_queue()

    assert sqs.deleted == [], 'a transient failure must still get its retries'
    assert handler.calls == ['msg-1', 'msg-1']


def test_the_third_receive_deletes_the_message_that_never_succeeds():
    handler = FakeHandler(fail_times=99)
    sqs = FakeSQS([[a_message('msg-1', receive_count=3)]])
    thread = build_thread(handler, sqs)

    thread.poll_and_process_queue()

    assert sqs.deleted == ['msg-1']


def test_the_dropped_message_is_logged_with_its_id_event_type_and_session():
    handler = FakeHandler(fail_times=99)
    sqs = FakeSQS([[a_message('msg-1', idea_session_id='sess-42', receive_count=3)]])
    thread = build_thread(handler, sqs)

    thread.poll_and_process_queue()

    logged = ' '.join(str(call) for call in thread._logger.error.call_args_list)
    assert 'msg-1' in logged
    assert 'IDEA_SESSION_SCHEDULED_STOP_EVENT' in logged
    assert 'sess-42' in logged


def test_a_configured_max_receive_count_is_honored():
    handler = FakeHandler(fail_times=99)
    sqs = FakeSQS([[a_message('msg-1', receive_count=3)]])
    thread = build_thread(handler, sqs, max_receive_count=5)

    thread.poll_and_process_queue()

    assert sqs.deleted == [], 'the operator raised the bound, so keep retrying'


def test_other_messages_in_the_same_batch_are_still_handled():
    # one poison event must not stop every other session's events from being processed
    class OneBadSession(FakeHandler):
        def handle_event(self, message_id, sender_id, event):
            self.calls.append(message_id)
            if event.detail['idea_session_id'] == 'poison':
                raise RuntimeError('SSM has no such instance')

    handler = OneBadSession()
    sqs = FakeSQS(
        [
            [
                a_message('msg-poison', idea_session_id='poison', receive_count=1),
                a_message('msg-healthy', idea_session_id='healthy', receive_count=1),
            ]
        ]
    )
    thread = build_thread(handler, sqs)

    thread.poll_and_process_queue()

    assert handler.calls == ['msg-poison', 'msg-healthy']
    assert sqs.deleted == ['msg-healthy']


def test_a_transient_failure_recovers_and_the_message_is_deleted():
    handler = FakeHandler(fail_times=1)
    sqs = FakeSQS(
        [[a_message('msg-1', receive_count=1)], [a_message('msg-1', receive_count=2)]]
    )
    thread = build_thread(handler, sqs)

    thread.poll_and_process_queue()
    assert sqs.deleted == []

    thread.poll_and_process_queue()
    assert sqs.deleted == ['msg-1']


def test_an_intentional_retry_is_never_dropped_by_the_bound():
    # DO_NOT_DELETE_MESSAGE is how a handler waits for a condition it expects to
    # arrive, so the bound must leave those alone however many times they come back
    handler = FakeHandler(
        fail_times=99,
        error=SocaException(
            error_code=errorcodes.DO_NOT_DELETE_MESSAGE, message='not ready yet'
        ),
    )
    sqs = FakeSQS([[a_message('msg-1', receive_count=9)]])
    thread = build_thread(handler, sqs)

    thread.poll_and_process_queue()

    assert sqs.deleted == []


class FakeSessionDB:
    def __init__(self, session):
        self.session = session

    def get_from_db(self, idea_session_id, idea_session_owner):
        return self.session


class FakeSSMCommandsUtils:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def submit_ssm_command_to_get_cpu_utilization(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


def a_session(state=VirtualDesktopSessionState.RESUMING):
    return VirtualDesktopSession(
        idea_session_id='sess-1',
        name='desktop-1',
        owner='someuser',
        state=state,
        base_os=VirtualDesktopBaseOS.AMAZON_LINUX2023,
        server=VirtualDesktopServer(instance_id='i-0123456789abcdef0'),
    )


def build_stop_handler(session, ssm_commands_utils):
    handler = object.__new__(IDEASessionScheduledStopEventHandler)
    handler.context = FakeContext(
        values={'virtual-desktop-controller.controller_iam_role_id': CONTROLLER_ROLE_ID}
    )
    handler._logger = Mock()
    handler.session_db = FakeSessionDB(session)
    handler.ssm_commands_utils = ssm_commands_utils
    return handler


def a_stop_event():
    from ideavirtualdesktopcontroller.app.clients.events_client.events_client import (
        VirtualDesktopEvent,
    )

    return VirtualDesktopEvent(
        event_group_id='sess-1',
        event_type=STOP_EVENT,
        detail={'idea_session_id': 'sess-1', 'idea_session_owner': 'someuser'},
    )


def test_a_host_ssm_does_not_know_about_does_not_raise():
    not_registered = ClientError(
        {
            'Error': {
                'Code': 'InvalidInstanceId',
                'Message': 'Instances not in a valid state for account',
            }
        },
        'SendCommand',
    )
    session = a_session()
    ssm = FakeSSMCommandsUtils(error=not_registered)
    handler = build_stop_handler(session, ssm)

    handler.handle_event('msg-1', f'{CONTROLLER_ROLE_ID}:vdc', a_stop_event())

    assert len(ssm.calls) == 1, 'the check is still attempted'
    assert session.state == VirtualDesktopSessionState.RESUMING, (
        'the timeout owns the state, not this handler'
    )
    handler._logger.warning.assert_called_once()


def test_any_other_ssm_error_still_surfaces():
    other = ClientError(
        {'Error': {'Code': 'AccessDeniedException', 'Message': 'nope'}}, 'SendCommand'
    )
    handler = build_stop_handler(a_session(), FakeSSMCommandsUtils(error=other))

    with pytest.raises(ClientError):
        handler.handle_event('msg-1', f'{CONTROLLER_ROLE_ID}:vdc', a_stop_event())


def test_a_healthy_session_still_gets_its_cpu_check():
    ssm = FakeSSMCommandsUtils()
    session = a_session(state=VirtualDesktopSessionState.READY)
    handler = build_stop_handler(session, ssm)

    handler.handle_event('msg-1', f'{CONTROLLER_ROLE_ID}:vdc', a_stop_event())

    assert ssm.calls == [
        {
            'instance_id': 'i-0123456789abcdef0',
            'idea_session_id': 'sess-1',
            'idea_session_owner': 'someuser',
            'base_os': VirtualDesktopBaseOS.AMAZON_LINUX2023,
        }
    ]
