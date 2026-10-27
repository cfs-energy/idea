"""Conditional DynamoDB operations keep tickets and limits correct across replicas."""

import hashlib
import secrets
import time

from botocore.exceptions import ClientError


class TerminalDenied(Exception):
    pass


class TerminalUnavailable(TerminalDenied):
    pass


class TerminalTargetStopped(TerminalDenied):
    pass


class TerminalMisconfigured(TerminalDenied):
    pass


class TicketRejected(TerminalDenied):
    pass


def aws_unavailable(error):
    code = error.response['Error']['Code']
    status = error.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
    return (
        status >= 500
        or status == 429
        or 'throttl' in code.lower()
        or code
        in {
            'ResourceNotFoundException',
            'ProvisionedThroughputExceededException',
            'RequestLimitExceeded',
            'TooManyRequestsException',
            'SlowDown',
            'BandwidthLimitExceeded',
            'TransactionInProgressException',
            'InternalServerError',
            'InternalServiceError',
            'ServerException',
            'InternalError',
            'Unavailable',
            'ServiceUnavailable',
        }
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class TerminalStore:
    def __init__(self, table, clock=time.time):
        self.table = table
        self.clock = clock

    def issue(self, username, target, client):
        ticket = secrets.token_urlsafe(32)
        self.table.put_item(
            Item={
                'pk': 'ticket:' + digest(ticket),
                'username': username,
                'target': target,
                'client': digest(client),
                'expires_at': int(self.clock()) + 30,
            }
        )
        return ticket

    def consume(self, ticket, username, target, client):
        item = self.table.delete_item(
            Key={'pk': 'ticket:' + digest(ticket)}, ReturnValues='ALL_OLD'
        ).get('Attributes')
        if (
            not item
            or item['expires_at'] <= self.clock()
            or (item['username'], item['target'], item['client'])
            != (username, target, digest(client))
        ):
            raise TicketRejected('Invalid or expired ticket')

    def acquire(self, username, session_id, limit=3):
        limit = max(1, min(10, limit))
        for slot in range(limit):
            key = f'slot:{username}:{slot}'
            try:
                self.table.put_item(
                    Item={
                        'pk': key,
                        'session_id': session_id,
                        'expires_at': int(self.clock()) + 60,
                    },
                    ConditionExpression='attribute_not_exists(pk) OR expires_at < :now',
                    ExpressionAttributeValues={':now': int(self.clock())},
                )
                return key
            except ClientError as error:
                if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
        raise TerminalUnavailable(f'{limit} terminals are already connected')

    def renew(self, key, session_id):
        try:
            self.table.update_item(
                Key={'pk': key},
                UpdateExpression='SET expires_at = :expires',
                ConditionExpression='session_id = :sid AND expires_at > :now',
                ExpressionAttributeValues={
                    ':sid': session_id,
                    ':now': int(self.clock()),
                    ':expires': int(self.clock()) + 60,
                },
            )
        except ClientError as error:
            if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise TerminalUnavailable('Terminal lease expired') from None
            raise

    def release(self, key, session_id):
        try:
            self.table.delete_item(
                Key={'pk': key},
                ConditionExpression='session_id = :sid',
                ExpressionAttributeValues={':sid': session_id},
            )
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
