import assert from 'node:assert/strict';

import { describe, it } from 'node:test';

import {
  ARCHITECTURE_ARM64,
  ARCHITECTURE_X86_64,
  ClusterConfigError,
  GeneralException,
  loadRegionAmiConfig,
  regionAmiConfigPath,
  resolveRegionAmi,
  type RegionsConfig,
} from '../../src/config/region-ami.ts';
import { requireFixtures } from '../support/fixtures.ts';

const FLAT: RegionsConfig = {
  'us-east-2': { amazonlinux2023: 'ami-flat-al2023', rhel9: 'ami-flat-rhel9' },
};

const NESTED: RegionsConfig = {
  'us-east-2': {
    x86_64: { amazonlinux2023: 'ami-x86-al2023' },
    arm64: { amazonlinux2023: 'ami-arm-al2023' },
  },
};

const HALF_MIGRATED: RegionsConfig = {
  'us-east-2': { amazonlinux2023: 'ami-flat-al2023', arm64: { amazonlinux2023: 'ami-arm-al2023' } },
};

describe('resolveRegionAmi', () => {
  it('flat shape: entries under the region are x86_64', () => {
    assert.equal(resolveRegionAmi(FLAT, 'us-east-2', 'amazonlinux2023'), 'ami-flat-al2023');
    assert.equal(
      resolveRegionAmi(FLAT, 'us-east-2', 'amazonlinux2023', ARCHITECTURE_X86_64),
      'ami-flat-al2023',
    );
  });

  it('nested shape: selects by architecture', () => {
    assert.equal(resolveRegionAmi(NESTED, 'us-east-2', 'amazonlinux2023', ARCHITECTURE_X86_64), 'ami-x86-al2023');
    assert.equal(resolveRegionAmi(NESTED, 'us-east-2', 'amazonlinux2023', ARCHITECTURE_ARM64), 'ami-arm-al2023');
    assert.equal(resolveRegionAmi(NESTED, 'us-east-2', 'amazonlinux2023'), 'ami-x86-al2023');
  });

  it('error 1: region missing', () => {
    assert.throws(
      () => resolveRegionAmi(FLAT, 'eu-west-3', 'amazonlinux2023'),
      (error: Error) => {
        assert.ok(error instanceof GeneralException);
        assert.match(error.message, /aws_region: eu-west-3 not found in region_ami_config\.yml/);
        return true;
      },
    );
  });

  it('error 2: a region that mixes architecture sections with base OS keys', () => {
    assert.throws(
      () => resolveRegionAmi(HALF_MIGRATED, 'us-east-2', 'amazonlinux2023'),
      (error: Error) => {
        assert.ok(error instanceof GeneralException);
        assert.match(error.message, /mixes architecture sections \(arm64\)/);
        assert.match(error.message, /top level \(amazonlinux2023\)/);
        return true;
      },
    );
  });

  it('error 3a: nested shape with the architecture section missing', () => {
    const onlyArm: RegionsConfig = { 'us-east-2': { arm64: { amazonlinux2023: 'ami-arm-al2023' } } };
    assert.throws(
      () => resolveRegionAmi(onlyArm, 'us-east-2', 'amazonlinux2023', ARCHITECTURE_X86_64),
      (error: Error) => {
        assert.ok(error instanceof GeneralException);
        assert.match(error.message, /no AMIs configured for architecture: x86_64 in region: us-east-2/);
        return true;
      },
    );
  });

  it('error 3b: flat shape with a non-x86_64 architecture requested', () => {
    assert.throws(
      () => resolveRegionAmi(FLAT, 'us-east-2', 'amazonlinux2023', ARCHITECTURE_ARM64),
      (error: Error) => {
        assert.ok(error instanceof GeneralException);
        assert.match(error.message, /lists x86_64 AMIs only for region: us-east-2/);
        assert.match(error.message, /architecture: arm64 was requested/);
        return true;
      },
    );
  });

  it('error 4: base_os missing in the chosen section', () => {
    assert.throws(
      () => resolveRegionAmi(FLAT, 'us-east-2', 'rocky10'),
      (error: Error) => {
        assert.ok(error instanceof ClusterConfigError);
        assert.match(
          error.message,
          /instance_ami not found for base_os: rocky10, architecture: x86_64, region: us-east-2/,
        );
        return true;
      },
    );
  });
});

requireFixtures([regionAmiConfigPath()], "npm run build");

describe('the shipped region_ami_config.yml', () => {
  const config = loadRegionAmiConfig();

  it('is flat for every region', () => {
    for (const [region, entry] of Object.entries(config)) {
      for (const key of Object.keys(entry)) {
        assert.ok(!['x86_64', 'arm64'].includes(key), `${region} is nested`);
      }
    }
  });

  it('resolves an AMI id per region and base OS, and rejects arm64 everywhere', () => {
    const regions = Object.keys(config);
    assert.ok(regions.length > 0);
    for (const region of regions) {
      const baseOs = Object.keys(config[region] as Record<string, unknown>);
      for (const os of baseOs) {
        const shipped = (config[region] as Record<string, unknown>)[os];
        assert.equal(typeof shipped, 'string');
        const resolved = resolveRegionAmi(config, region, os);
        assert.match(resolved, /^ami-[0-9a-f]+$/);
        assert.equal(resolved, shipped);
      }
      assert.throws(() => resolveRegionAmi(config, region, baseOs[0] as string, ARCHITECTURE_ARM64));
    }
  });
});
