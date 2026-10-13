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
import sys

from ideasdk.config.soca_config import SocaConfig
from ideadatamodel import exceptions, errorcodes, constants, CustomFileLoggerParams
from ideasdk.utils import Utils, EnvironmentUtils
from ideasdk.protocols import SocaLoggingProtocol

import logging
import logging.handlers
import os
from typing import Optional


def get_default_logging_config(profile: str = 'default'):
    return {
        'cluster': {
            'logging': {
                'formatters': {
                    'default': {
                        'format': '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s'
                    }
                },
                'handlers': {'console': {'class': 'logging.handlers.StreamHandler'}},
                'profiles': {
                    'default': {
                        'formatter': 'default',
                        'loggers': {
                            'app': {'level': 'INFO', 'handlers': ['console']},
                            'root': {'level': 'ERROR', 'handlers': ['console']},
                        },
                    },
                    'debug': {
                        'formatter': 'default',
                        'loggers': {
                            'app': {'level': 'DEBUG', 'handlers': ['console']},
                            'root': {'level': 'ERROR', 'handlers': ['console']},
                        },
                    },
                },
            }
        },
        'default': {
            'logging': {'profile': profile, 'default_log_file_name': 'application.log'}
        },
    }


class SocaLogging(SocaLoggingProtocol):
    def __init__(
        self,
        config: SocaConfig = None,
        module_id: str = 'default',
        default_logging_profile: str = None,
    ):
        if default_logging_profile is None:
            default_logging_profile = 'default'

        # Resolve once from the supplied settings, even if the module has no profile.
        level_config = config or SocaConfig({})
        module_name = (getattr(level_config, 'module_info', None) or {}).get(
            'name', module_id
        )
        level = None
        for level_key in (
            f'{module_id}.logging.default_log_level',
            f'{module_name}.logging.default_log_level',
            'cluster.logging.default_log_level',
            'logging.default_log_level',  # Legacy cluster key for one release.
        ):
            # Read stored keys: ClusterConfig otherwise remaps the name back to the id.
            level = SocaConfig.get(level_config, level_key)
            if level is not None:
                break
        if level is None:
            level = 'INFO'
        self._invalid_levels = []
        self._default_log_level = self._resolve_level(level_key, level)
        portal_key = 'cluster-manager.web_portal.default_log_level'
        portal_level = level_config.get(portal_key)
        self._portal_log_level = None
        if portal_level is not None:
            # Preserve the portal's existing OFF/ERROR/WARN/INFO/DEBUG/TRACE numbers.
            portal_levels = {
                0: logging.CRITICAL + 1,
                1: logging.ERROR,
                2: logging.WARNING,
                3: logging.INFO,
                4: logging.DEBUG,
                5: logging.DEBUG,
            }
            self._portal_log_level = (
                portal_levels.get(portal_level) if type(portal_level) is int else None
            )
            if self._portal_log_level is None:
                self._portal_log_level = self._resolve_level(portal_key, portal_level)

        if config is None:
            self.module_id = 'default'
            self._config = SocaConfig(
                get_default_logging_config(profile=default_logging_profile)
            )
        else:
            logging_config = config.get_config(f'{module_id}.logging')
            if logging_config is not None:
                self._config = config
                self.module_id = module_id
            else:
                self.module_id = 'default'
                self._config = SocaConfig(
                    get_default_logging_config(profile=default_logging_profile)
                )
        self._file_handlers = {}
        self._initialize_root_logger()
        if self._invalid_levels:
            logger = self.get_logger('logging')
            level = logger.level
            logger.setLevel(logging.WARNING)
            for key in self._invalid_levels:
                logger.warning('Invalid log level for %s; falling back to INFO', key)
            logger.setLevel(level)
        logging.captureWarnings(True)

    def _resolve_level(self, key, value):
        levels = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'WARN': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL,
        }
        level = levels.get(value.strip().upper()) if isinstance(value, str) else None
        if level is None:
            self._invalid_levels.append(key)
            return logging.INFO
        return level

    def _initialize_root_logger(self):
        root = logging.getLogger()
        self._reset_logger(root)
        self._build_logger(logger_template=constants.LOGGER_TEMPLATE_ROOT)

    def get_log_dir(self) -> str:
        log_dir = self._config.get_string(f'{self.module_id}.logging.logs_directory')
        if log_dir is not None and os.path.isabs(log_dir):
            return log_dir
        if log_dir is None:
            log_dir = 'logs'
        app_deploy_dir = EnvironmentUtils.idea_app_deploy_dir()
        return os.path.join(app_deploy_dir, log_dir)

    def _get_default_log_file_name(self):
        return self._config.get_string(
            f'{self.module_id}.logging.default_log_file_name', required=True
        )

    def _build_formatter(self, name):
        log_format = self._config.get_string(
            f'cluster.logging.formatters.{name}.format'
        )
        return logging.Formatter(fmt=log_format)

    def _build_handler(self, handler_name, logger_name: Optional[str]):
        handler_config = self._config.get(f'cluster.logging.handlers.{handler_name}')

        handler_cls = handler_config['class']

        if handler_cls == 'logging.handlers.StreamHandler':
            return logging.StreamHandler(stream=sys.stdout)

        elif handler_cls == 'logging.handlers.TimedRotatingFileHandler':
            create_separate_file = False
            separate_files = self._get_separate_files()
            if separate_files and len(separate_files) > 0:
                for separate_file in separate_files:
                    if logger_name.startswith(separate_file):
                        create_separate_file = True
                        break

            if create_separate_file:
                filename = logger_name
            else:
                filename = self._get_default_log_file_name()

            log_dir = self.get_log_dir()
            logfile = os.path.join(log_dir, filename)

            if logfile in self._file_handlers:
                return self._file_handlers[logfile]
            else:
                file_handler = logging.handlers.TimedRotatingFileHandler(
                    filename=logfile,
                    encoding=constants.DEFAULT_ENCODING,
                    when=handler_config['when'],
                    interval=int(handler_config['interval']),
                    backupCount=int(handler_config['backupCount']),
                )
                self._file_handlers[logfile] = file_handler
                return file_handler
        else:
            raise exceptions.SocaException(
                error_code=errorcodes.CONFIG_ERROR,
                message=f'logging handler: {handler_cls} not supported',
            )

    def _get_profile(self):
        return self._config.get_string(f'{self.module_id}.logging.profile')

    def _get_separate_files(self):
        return self._config.get_list(f'{self.module_id}.logging.separate_files')

    def _get_enable_debug_logging(self):
        return self._config.get_list(f'{self.module_id}.logging.enable_debug_logging')

    def _get_profile_settings(self, profile=None):
        if profile is None:
            profile = self._get_profile()
        return self._config.get(f'cluster.logging.profiles.{profile}')

    def _get_app_log_level(self, logger_name):
        if logger_name == 'web-portal' and self._portal_log_level is not None:
            return self._portal_log_level
        for prefix in self._get_enable_debug_logging() or []:
            if logger_name.startswith(prefix):
                return logging.DEBUG
        return self._default_log_level

    @staticmethod
    def _reset_logger(logger: logging.Logger):
        for handler in logger.handlers:
            logger.removeHandler(handler)

        for log_filter in logger.filters:
            logger.removeFilter(log_filter)

    @staticmethod
    def _build_default_logger(logger_template: str, logger: logging.Logger):
        if logger_template == 'root':
            level = logging.CRITICAL
        else:
            level = logging.INFO

        logger.setLevel(level)
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                fmt='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s'
            )
        )
        logger.addHandler(handler)
        return logger

    def _build_logger(self, logger_template: str = None, logger_name: str = None):
        if logger_template == constants.LOGGER_TEMPLATE_APP and Utils.is_empty(
            logger_name
        ):
            logger_name = 'app'

        logger = logging.getLogger(logger_name)
        if len(logger.handlers) > 0:
            return logger

        settings = self._get_profile_settings()

        if settings is None:
            self._build_default_logger(logger_template=logger_template, logger=logger)
            if logger_template == constants.LOGGER_TEMPLATE_APP:
                logger.setLevel(self._get_app_log_level(logger_name))
                for handler in logger.handlers:
                    handler.setLevel(logging.NOTSET)
            return logger

        level = settings[f'loggers.{logger_template}.level']
        if logger_template == constants.LOGGER_TEMPLATE_APP:
            level = self._get_app_log_level(logger_name)

        logger.setLevel(level)

        for handler_name in settings[f'loggers.{logger_template}.handlers']:
            handler = self._build_handler(
                handler_name=handler_name, logger_name=logger_name
            )
            # File handlers are shared: a portal override must not filter other loggers.
            handler.setLevel(logging.NOTSET)
            handler.setFormatter(self._build_formatter(name=settings['formatter']))
            logger.addHandler(handler)

        logger.propagate = False

        return logger

    def get_logger(self, logger_name: str = None) -> logging.Logger:
        return self._build_logger(
            logger_template=constants.LOGGER_TEMPLATE_APP, logger_name=logger_name
        )

    def get_custom_file_logger(
        self,
        params: CustomFileLoggerParams,
        log_level=logging.CRITICAL,
        fmt='%(message)s',
    ) -> logging.Logger:
        logger = logging.getLogger(params.logger_name)

        for handler in logger.handlers:
            logger.removeHandler(handler)
        for log_filter in logger.filters:
            logger.removeFilter(log_filter)

        logger.setLevel(log_level)

        if os.path.isabs(params.log_dir_name):
            log_dir = params.log_dir_name
        else:
            log_dir = os.path.join(self.get_log_dir(), params.log_dir_name)
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir)
        logfile = os.path.join(log_dir, params.log_file_name)

        if logfile in self._file_handlers:
            file_handler = self._file_handlers[logfile]
        else:
            file_handler = logging.handlers.TimedRotatingFileHandler(
                filename=logfile,
                encoding=constants.DEFAULT_ENCODING,
                when=params.when,
                interval=params.interval,
                backupCount=params.backupCount,
            )
            self._file_handlers[logfile] = file_handler

        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(fmt=fmt))
        logger.addHandler(file_handler)
        logger.propagate = False

        return logger
