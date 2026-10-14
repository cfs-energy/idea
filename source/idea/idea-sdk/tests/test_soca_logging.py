import logging
from uuid import uuid4

import pytest

from ideasdk.config.soca_config import SocaConfig
from ideasdk.config.cluster_config import ClusterConfig
from ideasdk.logging.soca_logging import SocaLogging, get_default_logging_config


@pytest.fixture
def logging_factory():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    existing = set(logging.Logger.manager.loggerDict)

    def make(values=None, profile='default', module='cluster-manager'):
        config = SocaConfig(get_default_logging_config(profile))
        config.put(f'{module}.logging.profile', profile)
        for key, value in (values or {}).items():
            config.put(key, value)
        return SocaLogging(config=config, module_id=module), config

    yield make
    for name in set(logging.Logger.manager.loggerDict) - existing:
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logging.Logger.manager.loggerDict.pop(name, None)
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def app_logger(setup):
    return setup.get_logger(f'test-{uuid4()}')


@pytest.mark.parametrize('profile', ['default', 'debug'])
def test_default_info_overrides_profile_debug(logging_factory, profile):
    setup, _ = logging_factory(profile=profile)
    logger = app_logger(setup)
    assert logger.level == logging.INFO
    assert not logger.isEnabledFor(logging.DEBUG)
    assert logger.isEnabledFor(logging.INFO)


@pytest.mark.parametrize('level', ['DEBUG', 'WARNING', 'ERROR', 'CRITICAL'])
def test_cluster_default(logging_factory, level):
    setup, _ = logging_factory({'cluster.logging.default_log_level': level})
    assert app_logger(setup).level == getattr(logging, level)


@pytest.mark.parametrize(
    'module',
    ['cluster-manager', 'scheduler', 'vdc'],
)
def test_module_override(logging_factory, module):
    setup, _ = logging_factory(
        {
            'cluster.logging.default_log_level': 'ERROR',
            f'{module}.logging.default_log_level': 'DEBUG',
        },
        module=module,
    )
    assert app_logger(setup).level == logging.DEBUG


@pytest.mark.parametrize(
    'values,expected',
    [
        ({'vdc.logging.default_log_level': 'DEBUG'}, logging.DEBUG),
        (
            {'virtual-desktop-controller.logging.default_log_level': 'DEBUG'},
            logging.DEBUG,
        ),
        (
            {
                'vdc.logging.default_log_level': 'WARNING',
                'virtual-desktop-controller.logging.default_log_level': 'DEBUG',
            },
            logging.WARNING,
        ),
    ],
)
def test_module_id_and_raw_name_overrides(logging_factory, values, expected):
    # Exercise the real name remapping without constructing a database client.
    config = object.__new__(ClusterConfig)
    SocaConfig.__init__(config, get_default_logging_config())
    config.module_info = {'name': 'virtual-desktop-controller', 'module_id': 'vdc'}
    config.module_set = 'default'
    config.put('cluster.logging.default_log_level', 'ERROR')
    for key, value in values.items():
        config.put(key, value)
    setup = SocaLogging(config=config, module_id='vdc')
    assert app_logger(setup).level == expected


@pytest.mark.parametrize(
    'values,expected',
    [
        ({'logging.default_log_level': 'DEBUG'}, logging.DEBUG),
        (
            {
                'logging.default_log_level': 'DEBUG',
                'cluster.logging.default_log_level': 'ERROR',
            },
            logging.ERROR,
        ),
    ],
)
def test_legacy_cluster_key_is_a_fallback(logging_factory, values, expected):
    setup, _ = logging_factory(values)
    assert app_logger(setup).level == expected


def test_cluster_level_without_module_logging_block(logging_factory):
    config = SocaConfig({'cluster': {'logging': {'default_log_level': 'ERROR'}}})
    setup = SocaLogging(config=config, module_id='vdc')
    assert app_logger(setup).level == logging.ERROR


def test_settings_are_read_once(logging_factory):
    setup, config = logging_factory({'cluster.logging.default_log_level': 'DEBUG'})
    config.put('cluster.logging.default_log_level', 'ERROR')
    assert app_logger(setup).level == logging.DEBUG


@pytest.mark.parametrize(
    'portal,expected', [(0, 51), (1, 40), (2, 30), (3, 20), (4, 10), (5, 10)]
)
def test_portal_numeric_precedence(logging_factory, portal, expected):
    setup, _ = logging_factory(
        {
            'cluster.logging.default_log_level': 'ERROR',
            'cluster-manager.logging.default_log_level': 'DEBUG',
            'cluster-manager.web_portal.default_log_level': portal,
            'cluster-manager.logging.enable_debug_logging': ['web-portal'],
        }
    )
    assert setup.get_logger('web-portal').level == expected
    assert app_logger(setup).level == logging.DEBUG


@pytest.mark.parametrize('value', ['verbose', 17, ['DEBUG']])
def test_invalid_level_warns_once_and_uses_info(logging_factory, capsys, value):
    setup, _ = logging_factory({'cluster.logging.default_log_level': value})
    assert app_logger(setup).level == logging.INFO
    app_logger(setup)
    output = capsys.readouterr().out
    assert output.count('Invalid log level for cluster.logging.default_log_level') == 1


def test_portal_precedence_without_a_profile(logging_factory):
    setup, config = logging_factory(
        {
            'cluster.logging.default_log_level': 'ERROR',
            'cluster-manager.web_portal.default_log_level': 4,
        }
    )
    config.put('cluster-manager.logging.profile', 'missing')
    assert setup.get_logger('web-portal').level == logging.DEBUG
    assert app_logger(setup).level == logging.ERROR


def test_file_handler_does_not_filter_other_loggers(logging_factory, tmp_path):
    setup, config = logging_factory(
        {
            'cluster.logging.default_log_level': 'DEBUG',
            'cluster-manager.web_portal.default_log_level': 3,
            'cluster-manager.logging.logs_directory': str(tmp_path),
            'cluster-manager.logging.default_log_file_name': 'application.log',
            'cluster.logging.handlers.file': {
                'class': 'logging.handlers.TimedRotatingFileHandler',
                'when': 'midnight',
                'interval': 1,
                'backupCount': 2,
            },
            'cluster.logging.profiles.default.loggers.app.handlers': ['file'],
        }
    )
    logger = app_logger(setup)
    portal = setup.get_logger('web-portal')
    logger.debug('application debug survives')
    portal.debug('portal debug filtered')
    portal.info('portal info survives')
    output = (tmp_path / 'application.log').read_text()
    assert 'application debug survives' in output
    assert 'portal debug filtered' not in output
    assert 'portal info survives' in output


def test_invalid_portal_warns_even_when_cluster_level_is_error(logging_factory, capsys):
    setup, _ = logging_factory(
        {
            'cluster.logging.default_log_level': 'ERROR',
            'cluster-manager.web_portal.default_log_level': 99,
        }
    )
    assert setup.get_logger('web-portal').level == logging.INFO
    assert (
        capsys.readouterr().out.count(
            'Invalid log level for cluster-manager.web_portal.default_log_level'
        )
        == 1
    )


def test_invalid_module_override_falls_back_to_info_not_cluster_level(
    logging_factory, capsys
):
    setup, _ = logging_factory(
        {
            'cluster.logging.default_log_level': 'DEBUG',
            'cluster-manager.logging.default_log_level': 'invalid',
        }
    )
    assert app_logger(setup).level == logging.INFO
    assert (
        capsys.readouterr().out.count(
            'Invalid log level for cluster-manager.logging.default_log_level'
        )
        == 1
    )
