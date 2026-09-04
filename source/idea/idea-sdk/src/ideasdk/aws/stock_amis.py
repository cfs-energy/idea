"""
Stock AMI resolution shared by the virtual desktop controller (base software stacks)
and the scheduler (compute image builds). One owner for the name patterns and the
publisher accounts, so a pattern fix lands in both modules at once.
"""

import re
from typing import List, Optional, Tuple

# AMI search patterns for different OS/architecture combinations.
# Minor releases are wildcarded so these keep resolving across point releases; the
# newest match by CreationDate wins. '_HVM-' excludes the '_HVM_BETA-' images.
AMI_PATTERNS = {
    # Windows base AMI names carry a build date. Do not pin the year: AWS retains only
    # recent releases, and find_latest_ami() already selects the newest by CreationDate.
    'windows2019/x86-64/base': 'Windows_Server-2019-English-Full-Base-*',
    'windows2022/x86-64/base': 'Windows_Server-2022-English-Full-Base-*',
    'windows2025/x86-64/base': 'Windows_Server-2025-English-Full-Base-*',
    'rhel8/arm64': 'RHEL-8.*_HVM-*-arm64-*',
    'rhel8/x86-64': 'RHEL-8.*_HVM-*-x86_64-*',
    'rhel9/arm64': 'RHEL-9.*_HVM-*-arm64-*',
    'rhel9/x86-64': 'RHEL-9.*_HVM-*-x86_64-*',
    'rhel10/arm64': 'RHEL-10.*_HVM-*-arm64-*',
    'rhel10/x86-64': 'RHEL-10.*_HVM-*-x86_64-*',
    # Lookup patterns only: these resolve an existing stack id, they do not create stacks.
    # VirtualDesktopBaseOS has no UBUNTU2604 member, so no ubuntu2604 stack can exist.
    'ubuntu2204/arm64': 'ubuntu/images/hvm-ssd*/ubuntu-*-22.04-arm64-server-*',
    'ubuntu2204/x86-64': 'ubuntu/images/hvm-ssd*/ubuntu-*-22.04-amd64-server-*',
    'ubuntu2404/arm64': 'ubuntu/images/hvm-ssd*/ubuntu-*-24.04-arm64-server-*',
    'ubuntu2404/x86-64': 'ubuntu/images/hvm-ssd*/ubuntu-*-24.04-amd64-server-*',
    'ubuntu2604/arm64': 'ubuntu/images/hvm-ssd*/ubuntu-*-26.04-arm64-server-*',
    'ubuntu2604/x86-64': 'ubuntu/images/hvm-ssd*/ubuntu-*-26.04-amd64-server-*',
    'amazonlinux2023/arm64': 'al2023-ami-2023.*-kernel-*-arm64',
    'amazonlinux2023/x86-64': 'al2023-ami-2023.*-kernel-*-x86_64',
    'rocky8/arm64': 'Rocky-8-EC2-Base-8.*-*.aarch64*',
    'rocky8/x86-64': 'Rocky-8-EC2-Base-8.*-*.x86_64*',
    'rocky9/arm64': 'Rocky-9-EC2-Base-9.*-*.aarch64*',
    'rocky9/x86-64': 'Rocky-9-EC2-Base-9.*-*.x86_64*',
    'rocky10/arm64': 'Rocky-10-EC2-Base-10.*-*.aarch64*',
    'rocky10/x86-64': 'Rocky-10-EC2-Base-10.*-*.x86_64*',
}

# describe_images Owners per AMI type. Filtering on 'amazon' only returns
# Amazon-published images, which excludes every Red Hat, Rocky and Canonical AMI.
AMI_OWNERS = {
    'amazonlinux2023': ['amazon'],
    'windows2019': ['amazon'],
    'windows2022': ['amazon'],
    'windows2025': ['amazon'],
    # Red Hat Inc. (commercial partitions)
    'rhel8': ['309956199498'],
    'rhel9': ['309956199498'],
    'rhel10': ['309956199498'],
    # RESF only. A Marketplace-backed id in the shipped map means OptInRequired on the
    # first rocky job in every fresh account.
    'rocky8': ['792107900819'],
    'rocky9': ['792107900819'],
    'rocky10': ['792107900819'],
    # Canonical
    'ubuntu2204': ['099720109477'],
    'ubuntu2404': ['099720109477'],
    'ubuntu2604': ['099720109477'],
}

# The aws-us-gov partition has its own Red Hat and Canonical publisher accounts. Rocky is
# there only through the Marketplace listing, so the first-party lookup finds nothing in GovCloud.
GOV_AMI_OWNERS = {
    'rhel8': ['219670896067'],
    'rhel9': ['219670896067'],
    'rhel10': ['219670896067'],
    'ubuntu2204': ['513442679011'],
    'ubuntu2404': ['513442679011'],
    'ubuntu2604': ['513442679011'],
}

# a vendor can republish an older minor, making it the newest by date (RHEL-9.6.0_HVM-20260811
# vs RHEL-9.8.0_HVM-20260728), so rank on the minor in the name before the date.
MINOR_VERSION_PATTERNS = (
    r'RHEL-(\d+)\.(\d+)',
    r'Rocky-\d+-EC2-Base-(\d+)\.(\d+)',
    r'al2023-ami-2023\.(\d+)\.(\d+)',
)

# EC2 reports 'x86_64'; the software stack ids and the pattern keys spell it 'x86-64'
ARCHITECTURE_TO_PATTERN_KEY = {
    'x86_64': 'x86-64',
    'arm64': 'arm64',
}

DEFAULT_OWNERS = ['amazon', 'aws-marketplace']


def image_sort_key(image):
    """(major, minor, CreationDate); names without a minor (Windows, Ubuntu) sort by date alone"""
    created = image.get('CreationDate', '')
    for pattern in MINOR_VERSION_PATTERNS:
        match = re.match(pattern, image.get('Name', ''))
        if match:
            return (int(match.group(1)), int(match.group(2)), created)
    return (0, 0, created)


def get_ami_pattern_for_stack(
    stack_id,
) -> Tuple[Optional[str], Optional[str], Optional[List[str]]]:
    """
    Extract the OS/architecture pattern from a software stack ID such as
    'ss-base-amazonlinux2023-x86-64-dcv'. Returns (pattern_key, ami_pattern, owners), or
    (None, None, None) when the id matches no pattern.
    """
    if not stack_id.startswith('ss-base-'):
        return None, None, None

    stack_suffix = stack_id[8:]

    for pattern_key, ami_pattern in AMI_PATTERNS.items():
        os_arch = pattern_key.replace('/base', '').replace('/', '-')
        if stack_suffix.startswith(os_arch):
            return pattern_key, ami_pattern, list(DEFAULT_OWNERS)

    return None, None, None


def stock_ami_pattern(base_os: str, architecture: str = 'x86_64') -> Optional[str]:
    """the name pattern for a base OS and EC2 architecture, or None when there is no stock image"""
    arch_key = ARCHITECTURE_TO_PATTERN_KEY.get(architecture, architecture)
    key = f'{base_os}/{arch_key}'
    return AMI_PATTERNS.get(key) or AMI_PATTERNS.get(f'{key}/base')


def stock_unsupported_reason(
    base_os: Optional[str], region: Optional[str]
) -> Optional[str]:
    """
    why no stock image can be resolved for base_os in this region, or None. Rocky is only
    published through the Marketplace in GovCloud, and a Marketplace id would put
    OptInRequired in front of every launch.
    """
    if (
        region
        and region.startswith('us-gov-')
        and base_os
        and base_os.startswith('rocky')
    ):
        return (
            'Rocky Linux is only available through the Marketplace in GovCloud; '
            'base stacks and image builds for it are not supported there'
        )
    return None


def get_owners(ami_type, region, fallback):
    """the publisher account(s) for an AMI type in a region; `fallback` when the type is unknown"""
    if region.startswith('us-gov-') and ami_type in GOV_AMI_OWNERS:
        return GOV_AMI_OWNERS[ami_type]
    return AMI_OWNERS.get(ami_type, fallback)


def trusted_owners(base_os: str, region: Optional[str]) -> List[str]:
    """
    the only owners a base image may have: this account and the vendor that publishes
    base_os in this region. anything else is refused before it can become a build base.
    """
    return ['self'] + list(get_owners(base_os, region or '', []))


def find_latest_ami(ec2_client, pattern, owners, logger, ami_type=None):
    """the id of the newest AMI matching the pattern, or None (see find_latest_image)"""
    image = find_latest_image(ec2_client, pattern, owners, logger, ami_type)
    return image['ImageId'] if image else None


def find_latest_image(ec2_client, pattern, owners, logger, ami_type=None):
    """
    The newest AMI matching the pattern, or None. `owners` is the fallback owner list, used
    when `ami_type` names no known publisher. Windows patterns carry no architecture token,
    so a caller updating a stack must compare Architecture before using the id.
    """
    try:
        actual_owners = get_owners(ami_type, ec2_client.meta.region_name, owners)

        logger.debug(
            f'Searching for AMIs with pattern: {pattern}, owners: {actual_owners}'
        )

        response = ec2_client.describe_images(
            Filters=[
                {'Name': 'name', 'Values': [pattern]},
                {'Name': 'state', 'Values': ['available']},
                {'Name': 'architecture', 'Values': ['x86_64', 'arm64']},
            ],
            Owners=actual_owners,
        )

        images = response.get('Images', [])
        if not images:
            logger.warning(f'No AMIs found matching pattern: {pattern}')
            return None

        images.sort(key=image_sort_key, reverse=True)
        latest_ami = images[0]

        logger.info(
            f'Found latest AMI: {latest_ami["ImageId"]} ({latest_ami["Name"]}) created on {latest_ami["CreationDate"]}'
        )
        return latest_ami

    except Exception as e:
        logger.error(f'Error searching for AMI with pattern {pattern}: {str(e)}')
        return None


def stack_architecture(stack_id: Optional[str]) -> Optional[str]:
    """the EC2 architecture a base software stack id encodes (ss-base-<os>-<arch>-...), or None"""
    if not stack_id:
        return None
    for token, architecture in ARCHITECTURE_TO_PATTERN_KEY.items():
        if f'-{architecture}-' in stack_id:
            return token
    return None


def find_latest_stock_ami(
    ec2_client, base_os: str, architecture: str, logger
) -> Optional[str]:
    """the newest stock (vendor published) AMI for a base OS and architecture, or None"""
    pattern = stock_ami_pattern(base_os, architecture)
    if pattern is None:
        return None
    return find_latest_ami(ec2_client, pattern, list(DEFAULT_OWNERS), logger, base_os)
