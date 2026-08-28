import re

REDACTED = 'redacted'
# account and resource identifiers are removed from messages shown to job owners. they
# remain in the scheduler logs and the cluster manager web interface for administrators.
AWS_IDENTIFIERS = re.compile(
    r'arn:[a-z0-9-]*:\S+'
    r'|\b(?:ami|subnet|sg|vpc|eni|vol|snap|fs|i|lt|pg)-[0-9a-f]{8,}\b'
    r'|\bsfr-[0-9a-f-]{8,}\b'
    r'|\bL-[0-9A-Z]{8}\b'
    r'|\b\d{12}\b',
    re.IGNORECASE,
)


def redact_aws_identifiers(message) -> str:
    if message is None:
        return ''
    return AWS_IDENTIFIERS.sub(REDACTED, str(message))
