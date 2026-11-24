"""HPC-26: the stock application's job-name pattern, as shipped and as stored"""

import re
import time

import pytest

from ideadatamodel import SocaUserInputModuleMetadata
from ideascheduler.app.applications.hpc_applications_dao import (
    STOCK_JOB_NAME_PATTERN,
    STOCK_JOB_NAME_PATTERN_OLD,
    HpcApplicationsDAO,
)
from ideasdk.utils import Utils


@pytest.mark.parametrize(
    'length,valid', [(2, False), (3, True), (50, True), (51, False)]
)
def test_the_pattern_enforces_3_to_50_characters(length, valid):
    assert bool(re.fullmatch(STOCK_JOB_NAME_PATTERN, 'a' * length)) is valid


def test_a_long_name_with_one_bad_character_is_refused_at_once():
    started = time.monotonic()
    assert re.fullmatch(STOCK_JOB_NAME_PATTERN, 'a' * 40 + '!') is None
    assert time.monotonic() - started < 0.05


def test_a_stored_form_with_the_old_pattern_is_read_with_the_new_one():
    form = SocaUserInputModuleMetadata(
        name='form',
        sections=[
            {
                'name': 's',
                'params': [
                    {
                        'name': 'job_name',
                        'validate': {
                            'required': True,
                            'regex': STOCK_JOB_NAME_PATTERN_OLD,
                        },
                    }
                ],
            }
        ],
    )
    db = {
        'application_id': 'a1',
        'created_on': 1,
        'updated_on': 1,
        'form_template': Utils.to_json(form),
    }
    dao = HpcApplicationsDAO.__new__(HpcApplicationsDAO)
    application = dao.convert_from_db(db)
    regex = Utils.to_dict(application.form_template)['sections'][0]['params'][0][
        'validate'
    ]['regex']
    assert regex == STOCK_JOB_NAME_PATTERN
