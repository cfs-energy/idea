import os
import re
import boto3
import yaml
import shutil
import datetime
import itertools
from typing import List, Optional, Tuple
from botocore.config import Config
from botocore.exceptions import ClientError, ProfileNotFound

# a region the account cannot reach (me-south-1 from a VPC without a route) otherwise
# stalls the run for the default 60s connect timeout times the retry count.
EC2_CLIENT_CONFIG = Config(connect_timeout=5, retries={'max_attempts': 2})

# Define file paths as constants. The override lets a regen run write to a
# quarantine directory instead of the tree.
CONFIG_FILE_PATH = os.environ.get(
    'IDEA_AMI_CONFIG_PATH',
    '../../source/idea/idea-administrator/resources/config/region_ami_config.yml',
)

# GovCloud rows are maintained separately with the 'gov' profile and are left alone here.
SKIPPED_REGION_PREFIXES = ('us-gov-',)

# minor release is wildcarded so this keeps matching across point releases; anchoring on
# '_HVM-' excludes beta images (named '..._HVM_BETA-...').
AMI_NAME_MAPPINGS = {
    'amazonlinux2023': 'al2023-ami-2023.*-kernel-*-x86_64',
    'rhel8': 'RHEL-8.*_HVM-*-x86_64-*',
    'rhel9': 'RHEL-9.*_HVM-*-x86_64-*',
    # pinned to 10.1/10.2: the FSx Lustre client only exists for those minors, and 10.*
    # could match a refreshed 10.0 build with no client; EC2 filters can't exclude that.
    'rhel10': ['RHEL-10.1*_HVM-*-x86_64-*', 'RHEL-10.2*_HVM-*-x86_64-*'],
    # suffix must be optional (bare '*', no hyphen): RESF images end at .x86_64, while a
    # '-*' tail would only ever match Marketplace re-publishes.
    'rocky8': 'Rocky-8-EC2-Base-8.*-*.x86_64*',
    'rocky9': 'Rocky-9-EC2-Base-9.*-*.x86_64*',
    'rocky10': [
        'Rocky-10-EC2-Base-10.1-*.x86_64*',
        'Rocky-10-EC2-Base-10.2-*.x86_64*',
    ],
    # codename is wildcarded (jammy is hvm-ssd, noble+ is hvm-ssd-gp3) so a new LTS needs no
    # code change; these are lookup patterns only - ubuntu2604 isn't in ALLOWED_BASEOS.
    'ubuntu2204': 'ubuntu/images/hvm-ssd*/ubuntu-*-22.04-amd64-server-*',
    'ubuntu2404': 'ubuntu/images/hvm-ssd*/ubuntu-*-24.04-amd64-server-*',
    'ubuntu2604': 'ubuntu/images/hvm-ssd*/ubuntu-*-26.04-amd64-server-*',
}

# describe_images Owners per AMI type. Filtering on 'amazon' only returns
# Amazon-published images, which excludes every Red Hat, Rocky and Canonical AMI.
AMI_OWNERS = {
    'amazonlinux2023': ['amazon'],
    # Red Hat Inc. (commercial partitions)
    'rhel8': ['309956199498'],
    'rhel9': ['309956199498'],
    'rhel10': ['309956199498'],
    # RESF only - Marketplace excluded deliberately: a Marketplace-backed id in the shipped
    # map means OptInRequired on the first rocky job in every fresh account.
    'rocky8': ['792107900819'],
    'rocky9': ['792107900819'],
    'rocky10': ['792107900819'],
    # Canonical
    'ubuntu2204': ['099720109477'],
    'ubuntu2404': ['099720109477'],
    'ubuntu2604': ['099720109477'],
}

# never get a NEW row here: IDEA publishes no Ubuntu AMIs, and adding one would enable
# a base_os as a side effect of a refresh. an existing row is still refreshed.
UNPUBLISHED_AMI_TYPES = frozenset({'ubuntu2204', 'ubuntu2404', 'ubuntu2604'})

# Generating a timestamp
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

# Make a backup of the original file
shutil.copyfile(CONFIG_FILE_PATH, f'region_ami_config_backup_{timestamp}.yml')


def get_owners(ami_type: str) -> List[str]:
    return AMI_OWNERS.get(ami_type, ['amazon'])


def get_session() -> boto3.Session:
    # the named profile is the workstation setup; on an instance carrying only a role
    # there is no profile, so fall back to the default credential chain.
    try:
        return boto3.Session(profile_name='idea-dev')
    except ProfileNotFound:
        return boto3.Session()


# a vendor can republish an older minor, making it the newest by date (RHEL-9.6.0_HVM-20260811
# vs RHEL-9.8.0_HVM-20260728), so rank on the minor in the name before the date.
MINOR_VERSION_PATTERNS = (
    r'RHEL-(\d+)\.(\d+)',
    r'Rocky-\d+-EC2-Base-(\d+)\.(\d+)',
    r'al2023-ami-2023\.(\d+)\.(\d+)',
)


def image_sort_key(image: dict) -> Tuple[int, int, str]:
    # Ubuntu names carry a date, not a minor: no pattern matches and date order stands.
    for pattern in MINOR_VERSION_PATTERNS:
        match = re.match(pattern, image.get('Name', ''))
        if match:
            return (int(match.group(1)), int(match.group(2)), image['CreationDate'])
    return (0, 0, image['CreationDate'])


# Function to get AMI
def get_ami(region: str, ami_type: str) -> Tuple[str, Optional[str]]:
    session = get_session()
    ec2 = session.client('ec2', region_name=region, config=EC2_CLIENT_CONFIG)
    try:
        if ami_type not in AMI_NAME_MAPPINGS:
            print(f'Unknown AMI type: {ami_type}')
            return ('error', None)

        ami_name = AMI_NAME_MAPPINGS[ami_type]
        # a mapping entry may carry one pattern or a list; filter Values are OR'd
        ami_name_patterns = ami_name if isinstance(ami_name, list) else [ami_name]

        response = ec2.describe_images(
            Owners=get_owners(ami_type),
            Filters=[
                {'Name': 'name', 'Values': ami_name_patterns},
                {'Name': 'state', 'Values': ['available']},
                {'Name': 'architecture', 'Values': ['x86_64']},
            ],
        )

        # Check if we got any images back
        if not response['Images']:
            # distinguishes "nothing CURRENT" from "nothing at all": a region where the vendor
            # stopped publishing fresh builds can still have a working image hidden as deprecated.
            deprecated = ec2.describe_images(
                Owners=get_owners(ami_type),
                IncludeDeprecated=True,
                Filters=[
                    {'Name': 'name', 'Values': ami_name_patterns},
                    {'Name': 'state', 'Values': ['available']},
                    {'Name': 'architecture', 'Values': ['x86_64']},
                ],
            )
            if deprecated['Images']:
                # first-party policy: a row must never fall back to a marketplace id. use the
                # newest deprecated first-party image instead - deprecated-but-first-party beats marketplace.
                newest_deprecated = sorted(
                    deprecated['Images'],
                    key=image_sort_key,
                    reverse=True,
                )[0]['ImageId']
                print(
                    f'   Only DEPRECATED first-party images for {region} - {ami_type}; '
                    f'using newest ({newest_deprecated}) to keep the row first-party'
                )
                return ('ok_deprecated', newest_deprecated)
            print(
                f'   No images found for {region} - {ami_type} with pattern(s) {ami_name_patterns}'
            )
            return ('none', None)

        ami_id = sorted(response['Images'], key=image_sort_key, reverse=True)[0][
            'ImageId'
        ]
        return ('ok', ami_id)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code in ('AuthFailure', 'OptInRequired', 'UnauthorizedOperation'):
            # the QUERYING account cannot see this region (not opted in); that says
            # nothing about image availability for deployers who can - keep the row.
            print(
                f'   Region {region} not enabled for this account ({code}); '
                f'keeping the existing {ami_type} row untouched'
            )
            return ('region_disabled', None)
        print(f'   Failed to get AMI for {region} - {ami_type}. Error: {str(e)}')
        return ('error', None)
    except Exception as e:
        print(f'   Failed to get AMI for {region} - {ami_type}. Error: {str(e)}')
        return ('error', None)


# Load the data from YAML
with open(CONFIG_FILE_PATH, 'r') as stream:
    lines = stream.readlines()

# yaml.dump drops comments, so the leading header block is kept and written back.
header = ''.join(itertools.takewhile(lambda line: line.startswith('#'), lines))
data = yaml.safe_load(''.join(lines))

# Iterate through regions
for region in data:
    if region.startswith(SKIPPED_REGION_PREFIXES):
        print(f'Skipping region: {region} (maintained with the gov profile)')
        continue
    print(f'Processing region: {region}')
    # iterate the mappings rather than the existing keys, so a base_os that is not
    # in the file yet gets added when the region has an image for it.
    for ami_type in AMI_NAME_MAPPINGS:
        if ami_type in UNPUBLISHED_AMI_TYPES and ami_type not in data[region]:
            print(f'   Skipping {ami_type} in {region}: no AMIs published for it')
            continue
        status, ami_id = get_ami(region, ami_type)
        if status in ('ok', 'ok_deprecated'):
            print(f'   New AMI for {ami_type}: {ami_id}')
            data[region][ami_type] = ami_id
        elif status == 'none' and ami_type in data[region]:
            # first-party policy: never keep a row the vendor no longer backs - the old value
            # here can only be stale or a marketplace id, so drop it and leave it unsupported.
            print(
                f'   DROPPING {ami_type} row in {region}: no first-party images '
                f'(old value {data[region][ami_type]} removed)'
            )
            del data[region][ami_type]
        elif ami_type in data[region]:
            # region_disabled / only_deprecated / transient error: keep what ships
            print(f'   No update to AMI for {ami_type} in {region} ({status})')
        else:
            print(
                f'   No AMI available for {ami_type} in {region}, key omitted ({status})'
            )

# Save updated data back to YAML
with open(CONFIG_FILE_PATH, 'w') as outfile:
    outfile.write(header)
    yaml.dump(data, outfile, default_flow_style=False)

print('YAML file updated successfully.')
