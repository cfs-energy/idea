"""
Secrets Manager as the password tasks use it, held in memory: nothing reaches AWS.
"""

from datetime import datetime, timezone
from typing import Dict, List

from botocore.exceptions import ClientError


def client_error(code: str, operation: str, message: str = '') -> ClientError:
    return ClientError({'Error': {'Code': code, 'Message': message}}, operation)


class FakeSecretsManager:
    def __init__(self):
        self.secrets: Dict[str, Dict] = {}
        self.created: List[Dict] = []
        self.deleted: List[str] = []
        self.list_calls: List[Dict] = []
        # set to an exception to have the next calls of that operation raise it
        self.failing: Dict[str, Exception] = {}
        # what a role scoped by tags is told about a secret that no longer exists
        self.gone_code = 'ResourceNotFoundException'

    def _check(self, operation: str):
        if operation in self.failing:
            raise self.failing[operation]

    def add(self, name: str, value: str = 'secret-value', tags=None, age_days=0.0):
        arn = f'arn:aws:secretsmanager:us-east-1:123456789012:secret:{name}-AbCdEf'
        created = datetime.now(timezone.utc).timestamp() - age_days * 24 * 3600
        self.secrets[arn] = {
            'ARN': arn,
            'Name': name,
            'SecretString': value,
            'Tags': [{'Key': k, 'Value': v} for k, v in (tags or {}).items()],
            'CreatedDate': datetime.fromtimestamp(created, timezone.utc),
        }
        return arn

    def create_secret(self, **request):
        self._check('CreateSecret')
        self.created.append(request)
        tags = {tag['Key']: tag['Value'] for tag in request.get('Tags', [])}
        arn = self.add(request['Name'], request['SecretString'], tags)
        return {'ARN': arn, 'Name': request['Name']}

    def get_secret_value(self, SecretId):
        self._check('GetSecretValue')
        if SecretId not in self.secrets:
            raise client_error(self.gone_code, 'GetSecretValue')
        return {'SecretString': self.secrets[SecretId]['SecretString']}

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery):
        self._check('DeleteSecret')
        assert ForceDeleteWithoutRecovery is True
        if SecretId not in self.secrets:
            raise client_error(self.gone_code, 'DeleteSecret')
        del self.secrets[SecretId]
        self.deleted.append(SecretId)
        return {}

    def get_paginator(self, operation):
        assert operation == 'list_secrets'
        return self

    def paginate(self, **request):
        self._check('ListSecrets')
        self.list_calls.append(request)
        listed = [
            {key: value for key, value in secret.items() if key != 'SecretString'}
            for secret in self.secrets.values()
        ]
        # two pages, as a cluster with many would be answered
        yield {'SecretList': listed[:1]}
        yield {'SecretList': listed[1:]}
