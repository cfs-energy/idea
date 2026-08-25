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

import botocore.exceptions
import pytest

from ideaadministrator import app_main


def client_error(code: str, message: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {'Error': {'Code': code, 'Message': message}}, 'ListStackResources'
    )


class FakeContext:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    warning = info


class FakePaginator:
    def __init__(self, resources_by_stack):
        self.resources_by_stack = resources_by_stack

    def paginate(self, StackName):
        resources = self.resources_by_stack.get(StackName)
        if isinstance(resources, Exception):
            raise resources
        if resources is None:
            raise client_error(
                'ValidationError', f'Stack with id {StackName} does not exist'
            )
        return [{'StackResourceSummaries': resources}]


class FakeCfn:
    def __init__(self, resources_by_stack):
        self.paginator = FakePaginator(resources_by_stack)

    def get_paginator(self, _name):
        return self.paginator


class FakeEc2:
    def __init__(self, protected, alive=None):
        self.protected = dict(protected)
        self.alive = protected.keys() if alive is None else alive
        self.modified = []

    def describe_instance_attribute(self, InstanceId, Attribute):
        return {'DisableApiTermination': {'Value': self.protected[InstanceId]}}

    def modify_instance_attribute(self, InstanceId, DisableApiTermination):
        self.modified.append((InstanceId, DisableApiTermination['Value']))

    def describe_instances(self, Filters):
        requested = Filters[0]['Values']
        return {
            'Reservations': [
                {'Instances': [{'InstanceId': i} for i in requested if i in self.alive]}
            ]
        }


def instance(instance_id):
    return {
        'ResourceType': 'AWS::EC2::Instance',
        'PhysicalResourceId': instance_id,
    }


def stub_modules(monkeypatch, modules):
    class FakeDb:
        def __init__(self, **_kwargs):
            pass

        def get_cluster_modules(self):
            return modules

    monkeypatch.setattr(app_main, 'ClusterConfigDB', FakeDb)


def test_discovery_covers_every_module_not_a_hardcoded_list(monkeypatch):
    # directoryservice and a non-default module id are exactly what the hardcoded list missed
    stub_modules(
        monkeypatch,
        [
            {'module_id': 'scheduler', 'stack_name': 'c-scheduler'},
            {'module_id': 'directoryservice', 'stack_name': 'c-directoryservice'},
            {'module_id': 'vdc-two'},
        ],
    )
    cfn = FakeCfn(
        {
            'c-scheduler': [instance('i-sched'), {'ResourceType': 'AWS::EC2::Volume'}],
            'c-directoryservice': [instance('i-ds')],
            'c-vdc-two': [instance('i-vdc2')],
        }
    )

    found = app_main.get_module_instance_ids('c', 'us-east-2', None, cfn)

    assert found == [
        ('c-scheduler', 'i-sched'),
        ('c-directoryservice', 'i-ds'),
        ('c-vdc-two', 'i-vdc2'),
    ]


def test_undeployed_module_is_skipped_not_fatal(monkeypatch):
    stub_modules(
        monkeypatch,
        [
            {'module_id': 'scheduler', 'stack_name': 'c-scheduler'},
            {'module_id': 'analytics', 'stack_name': 'c-analytics'},
        ],
    )
    cfn = FakeCfn({'c-scheduler': [instance('i-sched')]})

    assert app_main.get_module_instance_ids('c', 'us-east-2', None, cfn) == [
        ('c-scheduler', 'i-sched')
    ]


def test_access_denied_is_not_treated_as_an_undeployed_module(monkeypatch):
    # swallowing this skips instances that are protected, and cfn deletes them silently
    stub_modules(monkeypatch, [{'module_id': 'scheduler', 'stack_name': 'c-scheduler'}])
    cfn = FakeCfn(
        {'c-scheduler': client_error('AccessDenied', 'not authorized to perform')}
    )

    with pytest.raises(botocore.exceptions.ClientError) as exc_info:
        app_main.get_module_instance_ids('c', 'us-east-2', None, cfn)

    assert exc_info.value.response['Error']['Code'] == 'AccessDenied'


def test_only_protected_instances_are_cleared():
    ec2 = FakeEc2({'i-on': True, 'i-off': False})

    cleared = app_main.clear_termination_protection(
        [('c-scheduler', 'i-on'), ('c-vdc', 'i-off')], ec2, FakeContext()
    )

    assert cleared == [('c-scheduler', 'i-on')]
    assert ec2.modified == [('i-on', False)]


def test_surviving_instance_is_re_protected_and_replaced_one_is_not():
    # i-gone is what the upgrade replaced: re-protecting it would fail, and it must not be tried
    ec2 = FakeEc2({'i-live': True, 'i-gone': True}, alive={'i-live'})

    app_main.restore_termination_protection(
        [('c-scheduler', 'i-live'), ('c-vdc', 'i-gone')], ec2, FakeContext()
    )

    assert ec2.modified == [('i-live', True)]


def test_a_replaced_instance_is_reported_rather_than_skipped_silently():
    # without this the log cannot tell "ran and skipped" from "never ran at all"
    ec2 = FakeEc2({'i-gone': True}, alive=set())
    context = FakeContext()

    app_main.restore_termination_protection([('c-scheduler', 'i-gone')], ec2, context)

    assert ec2.modified == []
    assert any(
        'i-gone' in message and 'replaced' in message for message in context.messages
    )


def test_an_instance_that_was_never_protected_does_not_come_back_protected():
    ec2 = FakeEc2({'i-off': False})
    context = FakeContext()

    cleared = app_main.clear_termination_protection([('c-vdc', 'i-off')], ec2, context)
    app_main.restore_termination_protection(cleared, ec2, context)

    assert ec2.modified == []
