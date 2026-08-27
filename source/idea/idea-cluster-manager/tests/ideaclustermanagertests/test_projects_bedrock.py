"""
Test Cases for the project bedrock block

The validators are called unbound against a stub holding only the collaborators
they touch, so these tests exercise catalog validation and the DAO round-trip
without DynamoDB - ProjectsService.__init__ creates tables.
"""

from ideaclustermanager.app.api.projects_api import ProjectsAPI
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO
from ideaclustermanager.app.projects.bedrock_provisioner import BedrockProvisioner
from ideaclustermanager.app.projects.projects_service import ProjectsService

from ideadatamodel import exceptions, Project, ProjectBedrockConfig

from types import SimpleNamespace
import pytest

CATALOG = ['vendor-a.model-1', 'vendor-a.model-2', 'vendor-b.model-9']

ROLE_ARN = 'arn:aws:iam::123456789012:role/idea-mock-project-role'
INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::123456789012:instance-profile/idea-mock-project-profile'
)
INFERENCE_PROFILE_ARNS = {
    'vendor-a.model-1': 'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/aip-1'
}


class FakeConfig:
    def __init__(self, values):
        self.values = values
        self.requested_keys = []

    def get_list(self, key, default=None):
        self.requested_keys.append(key)
        return self.values.get(key, default)


def stub_service(config_values):
    config = FakeConfig(config_values)
    service = SimpleNamespace(
        context=SimpleNamespace(
            config=lambda: config,
            module_id=lambda: 'cluster-manager',
        )
    )
    return service, config


def bedrock_project(**kwargs) -> Project:
    return Project(
        project_id='project-1',
        name='sampleproject',
        bedrock=ProjectBedrockConfig(**kwargs),
    )


def test_catalog_is_read_from_the_module_settings_key():
    """the catalog lives under the cluster-manager module settings"""
    service, config = stub_service({'cluster-manager.bedrock.model_ids': CATALOG})

    assert BedrockProvisioner.get_model_catalog(service) == CATALOG
    assert config.requested_keys == ['cluster-manager.bedrock.model_ids']


def test_catalog_is_empty_when_not_configured():
    """a deployment that never set a catalog approves nothing"""
    service, _ = stub_service({})

    assert BedrockProvisioner.get_model_catalog(service) == []


def test_model_ids_in_catalog_are_accepted():
    project = bedrock_project(
        enabled=True, model_ids=['vendor-a.model-1', 'vendor-b.model-9']
    )

    ProjectsService.validate_bedrock_config(project, CATALOG)


def test_model_id_outside_catalog_is_rejected():
    """the offending model id is named in the error"""
    project = bedrock_project(
        enabled=True, model_ids=['vendor-a.model-1', 'vendor-c.model-0']
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        ProjectsService.validate_bedrock_config(project, CATALOG)

    assert 'vendor-c.model-0' in str(exc_info.value)


def test_model_ids_are_rejected_when_the_catalog_is_empty():
    project = bedrock_project(enabled=True, model_ids=['vendor-a.model-1'])

    with pytest.raises(exceptions.SocaException):
        ProjectsService.validate_bedrock_config(project, [])


def test_model_ids_are_validated_even_when_bedrock_is_disabled():
    """a disabled project must not be able to stage unapproved model ids"""
    project = bedrock_project(enabled=False, model_ids=['vendor-c.model-0'])

    with pytest.raises(exceptions.SocaException):
        ProjectsService.validate_bedrock_config(project, CATALOG)


def test_a_global_model_id_is_rejected_even_when_it_is_in_the_catalog():
    """an administrator can add any id to the catalog, a project still refuses it"""
    global_id = 'global.vendor-a.model-1'
    project = bedrock_project(enabled=True, model_ids=[global_id])

    with pytest.raises(exceptions.SocaException) as exc_info:
        ProjectsService.validate_bedrock_config(
            project, CATALOG + [global_id], 'aws', 'us-east-1'
        )

    message = str(exc_info.value)
    assert global_id in message
    assert 'us.vendor-a.model-1' in message


def test_a_cross_region_model_id_in_the_catalog_is_accepted():
    model_id = 'us.vendor-a.model-1'
    project = bedrock_project(enabled=True, model_ids=[model_id])

    ProjectsService.validate_bedrock_config(
        project, CATALOG + [model_id], 'aws', 'us-east-1'
    )


def test_project_without_bedrock_block_is_not_validated():
    project = Project(project_id='project-1', name='sampleproject')

    ProjectsService.validate_bedrock_config(project, CATALOG)


def test_empty_model_ids_are_accepted():
    project = bedrock_project(enabled=True)

    ProjectsService.validate_bedrock_config(project, CATALOG)


def test_caller_supplied_provisioner_fields_are_discarded_on_create():
    project = bedrock_project(
        enabled=True,
        model_ids=['vendor-a.model-1'],
        role_arn='arn:aws:iam::123456789012:role/attacker-supplied',
        instance_profile_arn='arn:aws:iam::123456789012:instance-profile/attacker-supplied',
        inference_profile_arns={
            'vendor-a.model-1': 'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/other'
        },
    )

    ProjectsService.apply_bedrock_provisioner_fields(project)

    assert project.bedrock.role_arn is None
    assert project.bedrock.instance_profile_arn is None
    assert project.bedrock.inference_profile_arns is None
    assert project.bedrock.model_ids == ['vendor-a.model-1']


def test_stored_provisioner_fields_are_carried_forward_on_update():
    """the block is written whole, so provisioner state must survive an update"""
    existing = {
        'project_id': 'project-1',
        'bedrock': {
            'enabled': True,
            'model_ids': ['vendor-a.model-1'],
            'role_arn': ROLE_ARN,
            'instance_profile_arn': INSTANCE_PROFILE_ARN,
            'inference_profile_arns': INFERENCE_PROFILE_ARNS,
        },
    }
    project = bedrock_project(
        enabled=True,
        model_ids=['vendor-a.model-1', 'vendor-b.model-9'],
        role_arn='arn:aws:iam::123456789012:role/attacker-supplied',
    )

    ProjectsService.apply_bedrock_provisioner_fields(project, existing)

    assert project.bedrock.role_arn == ROLE_ARN
    assert project.bedrock.instance_profile_arn == INSTANCE_PROFILE_ARN
    assert project.bedrock.inference_profile_arns == INFERENCE_PROFILE_ARNS
    assert project.bedrock.model_ids == ['vendor-a.model-1', 'vendor-b.model-9']


def test_bedrock_block_round_trips_through_the_dao():
    project = bedrock_project(
        enabled=True,
        model_ids=['vendor-a.model-1', 'vendor-b.model-9'],
        role_arn=ROLE_ARN,
        instance_profile_arn=INSTANCE_PROFILE_ARN,
        inference_profile_arns=INFERENCE_PROFILE_ARNS,
    )

    db_project = ProjectsDAO.convert_to_db(project)
    assert db_project['bedrock'] == {
        'enabled': True,
        'model_ids': ['vendor-a.model-1', 'vendor-b.model-9'],
        'role_arn': ROLE_ARN,
        'instance_profile_arn': INSTANCE_PROFILE_ARN,
        'inference_profile_arns': INFERENCE_PROFILE_ARNS,
    }

    db_project['created_on'] = 1
    db_project['updated_on'] = 2
    restored = ProjectsDAO.convert_from_db(db_project)

    assert restored.bedrock == project.bedrock
    assert restored.is_bedrock_enabled() is True


def test_project_without_bedrock_block_round_trips_as_none():
    project = Project(project_id='project-1', name='sampleproject')

    db_project = ProjectsDAO.convert_to_db(project)
    assert 'bedrock' not in db_project

    db_project['created_on'] = 1
    db_project['updated_on'] = 2
    restored = ProjectsDAO.convert_from_db(db_project)

    assert restored.bedrock is None
    assert restored.is_bedrock_enabled() is False


# the provisioner records why a model or policy got no access. that is only useful if a
# read carries it back out again.


def test_recorded_model_errors_reach_the_api():
    stored = {
        'project_id': 'p-1',
        'name': 'research',
        'title': 'Research',
        'enabled': True,
        'created_on': 1,
        'updated_on': 1,
        'bedrock': {
            'enabled': True,
            'model_ids': ['good.model', 'bad.model'],
            'inference_profile_arns': {'good.model': 'arn:aws:bedrock:::x'},
            'model_errors': {'bad.model': 'not in the cluster model catalog'},
        },
    }

    project = ProjectsDAO.convert_from_db(stored)

    assert project.bedrock.model_errors == {
        'bad.model': 'not in the cluster model catalog'
    }
    assert project.bedrock.get_model_errors()['bad.model']


def test_recorded_policy_errors_reach_the_api():
    stored = {
        'project_id': 'p-1',
        'name': 'research',
        'title': 'Research',
        'enabled': True,
        'created_on': 1,
        'updated_on': 1,
        'bedrock': {
            'enabled': True,
            'policy_errors': {
                'arn:aws:iam::111122223333:policy/x': 'boundary voids s3:PutObject'
            },
        },
    }

    project = ProjectsDAO.convert_from_db(stored)

    assert project.bedrock.policy_errors == {
        'arn:aws:iam::111122223333:policy/x': 'boundary voids s3:PutObject'
    }


def test_a_project_with_no_recorded_errors_is_unchanged():
    stored = {
        'project_id': 'p-1',
        'name': 'research',
        'title': 'Research',
        'enabled': True,
        'created_on': 1,
        'updated_on': 1,
        'bedrock': {'enabled': True, 'model_ids': ['good.model']},
    }

    project = ProjectsDAO.convert_from_db(stored)

    assert project.bedrock.model_errors is None
    assert project.bedrock.policy_errors is None
    assert project.bedrock.get_model_errors() == {}


def test_recorded_errors_survive_an_update_round_trip():
    """an administrator edit is written through convert_to_db and must not erase them"""
    stored = {
        'project_id': 'p-1',
        'name': 'research',
        'title': 'Research',
        'enabled': True,
        'created_on': 1,
        'updated_on': 1,
        'bedrock': {
            'enabled': True,
            'model_ids': ['good.model'],
            'model_errors': {'bad.model': 'not in the cluster model catalog'},
            'policy_errors': {'arn:aws:iam::123456789012:policy/x': 'not attached'},
        },
    }

    written = ProjectsDAO.convert_to_db(ProjectsDAO.convert_from_db(stored))

    assert written['bedrock']['model_errors'] == stored['bedrock']['model_errors']
    assert written['bedrock']['policy_errors'] == stored['bedrock']['policy_errors']
    # absent errors stay absent rather than being written as empty
    plain = ProjectsDAO.convert_to_db(
        Project(name='r', bedrock=ProjectBedrockConfig(enabled=True, model_ids=[]))
    )
    assert 'model_errors' not in plain['bedrock']
    assert 'policy_errors' not in plain['bedrock']


def test_member_facing_projects_do_not_carry_policy_errors():
    project = Project(
        name='research',
        bedrock=ProjectBedrockConfig(
            enabled=True,
            model_ids=['good.model'],
            role_arn=ROLE_ARN,
            instance_profile_arn=INSTANCE_PROFILE_ARN,
            inference_profile_arns=INFERENCE_PROFILE_ARNS,
            model_errors={'bad.model': 'not in the cluster model catalog'},
            policy_errors={'arn:aws:iam::123456789012:policy/x': 'not attached'},
        ),
    )

    ProjectsAPI.strip_bedrock_provisioner_fields(project)

    assert project.bedrock.policy_errors is None
    assert project.bedrock.role_arn is None
    assert project.bedrock.instance_profile_arn is None
    # what a member needs to pick and invoke a model stays
    assert project.bedrock.inference_profile_arns == INFERENCE_PROFILE_ARNS
    assert project.bedrock.model_errors == {
        'bad.model': 'not in the cluster model catalog'
    }
