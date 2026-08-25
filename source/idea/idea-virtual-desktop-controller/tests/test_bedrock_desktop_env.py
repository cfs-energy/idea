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

from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    CLAUDE_CODE_PERMISSION_MODE_CONFIG_KEY,
    build_auto_mode_environment,
    build_bedrock_env,
    describe_bedrock_models,
    resolve_claude_code_permission_mode,
)

HAIKU = 'us.anthropic.claude-haiku-4-5-20251001-v1:0'
SONNET = 'us.anthropic.claude-sonnet-5'
OPUS = 'us.anthropic.claude-opus-5'
OPUS_4_1 = 'us.anthropic.claude-opus-4-1-20250805-v1:0'
OPUS_4_5 = 'us.anthropic.claude-opus-4-5-20251101-v1:0'


class FakeClusterConfig:
    """the cluster config collapses an empty value to the default, the same as this"""

    def __init__(self, settings=None):
        self.settings = {} if settings is None else settings

    def get_string(self, key, default=None):
        value = self.settings.get(key)
        return default if value is None or len(value.strip()) == 0 else value


def test_the_most_capable_model_the_project_allows_is_the_main_one():
    env = build_bedrock_env(
        {HAIKU: 'arn/haiku', SONNET: 'arn/sonnet', OPUS: 'arn/opus'}
    )

    assert env['ANTHROPIC_MODEL'] == 'arn/opus'
    assert env['ANTHROPIC_SMALL_FAST_MODEL'] == 'arn/haiku'
    assert env['CLAUDE_CODE_USE_BEDROCK'] == '1'


def test_sonnet_is_the_main_model_when_the_project_has_no_opus():
    env = build_bedrock_env({HAIKU: 'arn/haiku', SONNET: 'arn/sonnet'})

    assert env['ANTHROPIC_MODEL'] == 'arn/sonnet'
    assert env['ANTHROPIC_SMALL_FAST_MODEL'] == 'arn/haiku'


def test_a_haiku_only_project_uses_it_for_both():
    env = build_bedrock_env({HAIKU: 'arn/haiku'})

    assert env['ANTHROPIC_MODEL'] == 'arn/haiku'
    assert env['ANTHROPIC_SMALL_FAST_MODEL'] == 'arn/haiku'


def test_the_fast_model_falls_back_to_the_main_one_without_a_haiku():
    env = build_bedrock_env({SONNET: 'arn/sonnet'})

    assert env['ANTHROPIC_SMALL_FAST_MODEL'] == 'arn/sonnet'


def test_the_newest_model_of_a_class_is_the_one_offered():
    """two opus profiles means the older one is a leftover, not a choice"""
    env = build_bedrock_env({OPUS_4_1: 'arn/opus-4-1', OPUS_4_5: 'arn/opus-4-5'})

    assert env['ANTHROPIC_MODEL'] == 'arn/opus-4-5'


def test_a_model_of_no_known_class_is_still_usable():
    """a model naming scheme we have not seen must not leave the desktop with nothing"""
    env = build_bedrock_env({'vendor.some-new-model': 'arn/new'})

    assert env['ANTHROPIC_MODEL'] == 'arn/new'
    assert env['ANTHROPIC_SMALL_FAST_MODEL'] == 'arn/new'


def test_no_env_at_all_when_nothing_is_provisioned():
    """
    an unprovisioned profile must not be seeded: pointing a client at an arn it cannot invoke is
    worse than leaving it unconfigured, because the failure looks like a permission bug.
    """
    assert build_bedrock_env({SONNET: None}) == {}
    assert build_bedrock_env({SONNET: ''}) == {}
    assert build_bedrock_env({}) == {}
    assert build_bedrock_env(None) == {}


def test_only_the_provisioned_models_are_offered():
    env = build_bedrock_env({OPUS: None, SONNET: 'arn/sonnet', HAIKU: 'arn/haiku'})

    # opus has no profile yet, so sonnet is the best actually-invocable model
    assert env['ANTHROPIC_MODEL'] == 'arn/sonnet'
    assert env['ANTHROPIC_SMALL_FAST_MODEL'] == 'arn/haiku'


def test_the_banner_names_the_models_not_the_arns():
    """a profile arn tells the reader nothing, so the banner has to name what they got"""
    messages = describe_bedrock_models(
        {HAIKU: 'arn/haiku', SONNET: 'arn/sonnet', OPUS: 'arn/opus'}
    )

    assert any(OPUS in m for m in messages)
    assert any(HAIKU in m for m in messages)
    assert not any('arn/' in m for m in messages)


def test_the_banner_does_not_repeat_itself_for_a_single_model():
    messages = describe_bedrock_models({HAIKU: 'arn/haiku'})

    assert len(messages) == 1


def test_no_banner_when_the_project_has_no_model_access():
    assert describe_bedrock_models({}) == []
    assert describe_bedrock_models({SONNET: None}) == []


def test_auto_mode_environment_keeps_the_builtin_defaults():
    """
    omitting $defaults would discard every built-in classifier rule, which is the documented
    footgun: it would silently drop the force-push and exfiltration protections.
    """
    entries = build_auto_mode_environment(
        cluster_name='idea-test',
        aws_region='us-east-2',
        cluster_s3_bucket='idea-test-cluster-us-east-2-123456789012',
    )

    assert entries[0] == '$defaults'
    assert any('idea-test' in e for e in entries)
    assert any('us-east-2' in e for e in entries)
    assert any('s3://idea-test-cluster-us-east-2-123456789012' in e for e in entries)


def test_the_seeded_permission_mode_is_the_one_the_cluster_carries():
    config = FakeClusterConfig({CLAUDE_CODE_PERMISSION_MODE_CONFIG_KEY: 'plan'})

    assert resolve_claude_code_permission_mode(config) == 'plan'


def test_a_cluster_without_the_setting_seeds_auto():
    for settings in ({}, {CLAUDE_CODE_PERMISSION_MODE_CONFIG_KEY: ''}):
        assert (
            resolve_claude_code_permission_mode(FakeClusterConfig(settings)) == 'auto'
        )


def test_the_none_value_seeds_no_permissions_block():
    """the template writes no permissions block, leaving the client on its own default"""
    for value in ('none', 'None', ' none '):
        config = FakeClusterConfig({CLAUDE_CODE_PERMISSION_MODE_CONFIG_KEY: value})
        assert resolve_claude_code_permission_mode(config) == ''
