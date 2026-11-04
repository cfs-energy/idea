"""The broker role's logback edit, run against the vendor file the broker RPM installs."""

import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[4]
ROLE = ROOT / 'deployment/ecr/idea-control-plane/roles/broker.sh'

# /etc/dcv-session-manager-broker/logback.xml from nice-dcv-session-manager-broker 2024.0.504
VENDOR = """<?xml version="1.0" encoding="UTF-8"?>
<configuration>

    <appender name="STDOUT" class="ch.qos.logback.core.ConsoleAppender">
        <encoder>
            <pattern>%date{ISO8601} [%thread] %X{apiRequestId} %-5level %logger{56} - %msg%n</pattern>
        </encoder>
    </appender>

    <appender name="FILE" class="ch.qos.logback.core.rolling.RollingFileAppender">
        <rollingPolicy class="ch.qos.logback.core.rolling.TimeBasedRollingPolicy">
            <!-- Daily rollover -->
            <fileNamePattern>/var/log/dcv-session-manager-broker/DcvSmBroker${LOG_FILE_SUFFIX}.%d{yyyy-MM-dd}.log</fileNamePattern>

            <!-- Keep 5 days' worth of history -->
            <maxHistory>5</maxHistory>
            <cleanHistoryOnStart>true</cleanHistoryOnStart>
        </rollingPolicy>

        <encoder>
            <pattern>%date{ISO8601} [%thread] %X{apiRequestId} %-5level %logger{56} - %msg%n</pattern>
        </encoder>
    </appender>

    <!-- Configure so that it outputs to both console and log file -->
    <root level="INFO">
        <appender-ref ref="FILE" />
        <appender-ref ref="STDOUT" />
    </root>
    <logger name="org.eclipse.jetty" level="INFO"/>
    <logger name="org.jboss" level="WARN" />
    <logger name="org.apache" level="WARN" />
    <logger name="dcv.sm.ignite" level="INFO" />
    <logger name="software.amazon.awssdk" level="INFO" />
</configuration>"""

QUIET = (
    'io.swagger.api.HealthApi',
    'dcv.sm.filters.LogRequestFilter',
    'dcv.sm.agent.handlers.SessionsUpdateRequestHandler',
)


def edit(path, level='info'):
    source = ROLE.read_text()
    block = source[
        source.index('LOGBACK="${IDEA_BROKER_LOGBACK_FILE') : source.index(
            '# register-auth-server'
        )
    ]
    result = subprocess.run(
        ['bash', '-c', 'set -euo pipefail\nlog() { echo "$*"; }\n' + block],
        env={
            **os.environ,
            'IDEA_BROKER_LOGBACK_FILE': str(path),
            'DCV_BROKER_LOG_LEVEL': level,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return ET.fromstring(path.read_text())


@pytest.fixture
def logback(tmp_path):
    path = tmp_path / 'logback.xml'
    path.write_text(VENDOR)
    return path


def test_heartbeat_handlers_are_held_at_warn_and_a_restart_changes_nothing(logback):
    first = edit(logback)
    text = logback.read_text()
    second = edit(logback)
    assert logback.read_text() == text
    for config in (first, second):
        levels = {
            logger.get('name'): logger.get('level') for logger in config.iter('logger')
        }
        for name in QUIET:
            assert levels[name] == 'WARN', name
        names = [logger.get('name') for logger in config.iter('logger')]
        assert len(names) == len(set(names))
        assert config.find('root').get('level') == 'INFO'
    assert edit(logback, 'debug').find('root').get('level') == 'DEBUG'


def test_broker_lines_are_written_once_to_the_files_the_sidecar_ships(logback):
    # stdout and DcvSmBroker.log carried the same lines into the same log group and Datadog
    for _ in range(2):
        config = edit(logback)
        refs = [ref.get('ref') for ref in config.find('root').iter('appender-ref')]
        assert refs == ['FILE']
        pattern = config.find("appender[@name='FILE']/rollingPolicy/fileNamePattern")
        assert pattern.text.startswith('/var/log/dcv-session-manager-broker/')
