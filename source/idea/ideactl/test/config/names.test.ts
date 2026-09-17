import assert from 'node:assert/strict';
import { test } from 'node:test';
import { shake256Hex } from '../../src/util/shake256.ts';
import {
  buildInstanceProfileArn,
  buildResourceName,
  buildTrimmedResourceName,
  getKmsKeyArn,
  getTargetGroupName,
} from '../../src/util/names.ts';

test('shake256Hex matches the CDK bootstrap qualifier', () => {
  assert.equal(shake256Hex('idea-dev27', 5), '6f3b37a775');
});

test('getTargetGroupName suffixes', () => {
  assert.equal(getTargetGroupName('idea-dev27', 'cluster-manager', 'x').slice(-8), '76c95e5f');
  assert.equal(getTargetGroupName('idea-dev27', 'scheduler', 'x').slice(-8), '79a59eed');
  assert.equal(getTargetGroupName('idea-dev27', 'vdc', 'x').slice(-8), 'e8356b3f');
  assert.equal(getTargetGroupName('idea-dev27', 'analytics', 'x').slice(-8), '2c13f863');
  assert.equal(getTargetGroupName('idea-dev27', 'cluster-manager', 'web-portal'), 'idea-dev27-web-portal-76c95e5f');
});

test('getTargetGroupName throws over 32 characters', () => {
  assert.throws(
    () => getTargetGroupName('idea-dev27', 'cluster-manager', 'a-very-long-identifier'),
    /is longer than 32 characters/,
  );
});

test('buildResourceName', () => {
  assert.equal(buildResourceName('idea-dev27', 'cluster-manager-role'), 'idea-dev27-cluster-manager-role');
  assert.equal(
    buildResourceName('idea-dev27', 'cluster-s3-bucket', 'us-east-2'),
    'idea-dev27-cluster-s3-bucket-us-east-2',
  );
});

// A 12-character cluster name exercises resource-name trimming.
test('buildTrimmedResourceName matches the Python formula', () => {
  assert.equal(
    buildTrimmedResourceName('idea-synth12', 'shared-storage-security-group'),
    'idea-synth12-shared-sto-9aee082e2d76243907ae01b813ba095d9d78d178',
  );
  assert.equal(
    buildTrimmedResourceName('idea-synth12', 'shared-storage-security-group', 'us-east-2'),
    'idea-synth12-us-east-2-shared-sto-e07731331b9d923d8db8425a318e66',
  );
  assert.equal(buildTrimmedResourceName('idea-synth12', 'shared-storage-security-group').length, 64);
});

test('arn helpers', () => {
  assert.equal(
    buildInstanceProfileArn('aws', '111111111111', 'idea-dev27-scheduler-instance-profile'),
    'arn:aws:iam::111111111111:instance-profile/idea-dev27-scheduler-instance-profile',
  );
  assert.equal(
    getKmsKeyArn('abcd1234-0000-0000-0000-00000000abcd', 'aws', 'us-east-2', '111111111111'),
    'arn:aws:kms:us-east-2:111111111111:key/abcd1234-0000-0000-0000-00000000abcd',
  );
  const already = 'arn:aws:kms:us-east-2:111111111111:key/abcd1234-0000-0000-0000-00000000abcd';
  assert.equal(getKmsKeyArn(already, 'aws-us-gov', 'us-gov-west-1', '222222222222'), already);
});
