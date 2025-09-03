import boto3
import oyaml as yaml
import shutil
import datetime
import concurrent.futures
from typing import Optional, Tuple
from botocore.exceptions import ClientError

# Define file paths as constants
CONFIG_FILE_PATH = '../../source/idea/idea-virtual-desktop-controller/resources/base-software-stack-config.yaml'

# Define AMI patterns
AMI_PATTERNS = {
    # OS_TYPE/ARCH or OS_TYPE/ARCH/SUFFIX
    # Windows patterns - all versions will use the same pattern
    'windows2019/x86-64/base': 'Windows_Server-2019-English-Full-Base-2025.*',
    'windows2022/x86-64/base': 'Windows_Server-2022-English-Full-Base-2025.*',
    'windows2025/x86-64/base': 'Windows_Server-2025-English-Full-Base-2025.*',
    'rhel8/arm64': 'RHEL-8.10.0_HVM-*-arm64-*',
    'rhel8/x86-64': 'RHEL-8.10.0_HVM-*-x86_64-*',
    'rhel9/arm64': 'RHEL-9.6.0_HVM-*-arm64-*',
    'rhel9/x86-64': 'RHEL-9.6.0_HVM-*-x86_64-*',
    'ubuntu2204/arm64': 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*',
    'ubuntu2204/x86-64': 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*',
    'ubuntu2404/arm64': 'ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*',
    'ubuntu2404/x86-64': 'ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*',
    'amazonlinux2/arm64': 'amzn2-ami-kernel-5.10-hvm-*-arm64-gp2',
    'amazonlinux2/x86-64': 'amzn2-ami-kernel-5.10-hvm-*-x86_64-gp2',
    'amazonlinux2023/arm64': 'al2023-ami-2023.8*-kernel-6.1-arm64',
    'amazonlinux2023/x86-64': 'al2023-ami-2023.8.*-kernel-6.1-x86_64',
    # Commented out patterns kept for reference
    # "rhel7/x86-64": "RHEL-7.9_HVM-*-x86_64-*",
    'rocky8/arm64': 'Rocky-8-EC2-Base-8.9-*.aarch64-*',
    'rocky8/x86-64': 'Rocky-8-EC2-Base-8.9-*.x86_64-*',
    'rocky9/arm64': 'Rocky-9-EC2-Base-9.6-*.aarch64-*',
    'rocky9/x86-64': 'Rocky-9-EC2-Base-9.6-*.x86_64-*',
}

# Generating a timestamp
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

# Make a backup of the original file
shutil.copyfile(CONFIG_FILE_PATH, f'base-software-stack-config_backup_{timestamp}.yaml')

NON_REGION_KEYS = [
    'default-description',
    'default-min-ram-unit',
    'default-min-ram-value',
    'default-min-storage-unit',
    'default-min-storage-value',
    'default-name',
]

# Maximum number of concurrent threads
MAX_WORKERS = 10


# Function to get AMI
def get_ami(
    region: str, ami_type: str, architecture_type: str, ss_id_suffix: str = None
) -> Optional[str]:
    profile = 'gov' if region == 'us-gov-west-1' else 'idea-dev'

    # Construct pattern key
    if ami_type.startswith('windows'):
        pattern_key = f'{ami_type}/{architecture_type}/base'
        print(
            f'Getting AMI for Region: {region}, AMI Type: {ami_type}, Architecture: {architecture_type}, Profile: {profile}'
        )
    else:
        pattern_key = f'{ami_type}/{architecture_type}'
        print(
            f'Getting AMI for Region: {region}, AMI Type: {ami_type}, Architecture: {architecture_type}, Profile: {profile}'
        )

    try:
        # Check if we have a pattern for this configuration
        if pattern_key not in AMI_PATTERNS:
            print(f'No AMI pattern defined for: {pattern_key}')
            return None

        ami_name = AMI_PATTERNS[pattern_key]
        print(f'AMI name filter: {ami_name}')

        # Use aws-marketplace only for rocky8 and rocky9
        if ami_type in ['rocky8', 'rocky9']:
            owners = ['aws-marketplace']
        else:
            owners = ['amazon']

        session = boto3.Session(profile_name=profile)
        ec2 = session.client('ec2', region_name=region)
        response = ec2.describe_images(
            Owners=owners,
            Filters=[
                {'Name': 'name', 'Values': [ami_name]},
            ],
        )

        # Check if we got any images back
        if not response['Images']:
            print(
                f'   No images found for {region} - {pattern_key} with pattern {ami_name}'
            )
            return None

        # Sort by creation date to get the most recent one
        valid_amis = [
            ami
            for ami in response['Images']
            if 'ImageLocation' in ami and 'gaming' not in ami['ImageLocation']
        ]

        # Check if we have any valid AMIs after filtering
        if not valid_amis:
            print(
                f'   No valid AMIs found after filtering for {region} - {pattern_key}'
            )
            return None

        ami_id = sorted(valid_amis, key=lambda x: x['CreationDate'], reverse=True)[0][
            'ImageId'
        ]
        return ami_id
    except ClientError as e:
        print(f'   Failed to get AMI for {region} - {pattern_key}. Error: {str(e)}')
        return None
    except Exception as e:
        print(f'   Failed to get AMI for {region} - {pattern_key}. Error: {str(e)}')
        return None


# Worker function for parallel AMI lookup
def get_ami_worker(task: Tuple) -> Tuple:
    region, ami_type, architecture_type, ss_id_suffix, item_index = task
    new_ami_id = get_ami(region, ami_type, architecture_type, ss_id_suffix)
    return (region, ami_type, architecture_type, ss_id_suffix, item_index, new_ami_id)


# Function to fix YAML formatting for description and name fields
def format_yaml_file(file_path: str) -> None:
    with open(file_path, 'r') as yfile:
        lines = yfile.readlines()

    with open(file_path, 'w') as yfile:
        for line in lines:
            if 'default-description:' in line:
                idx = line.index('default-description:')
                yfile.write(
                    line[:idx]
                    + "default-description: '"
                    + line[idx:].split(':')[1].strip()
                    + "'\n"
                )
            elif 'default-name:' in line:
                idx = line.index('default-name:')
                yfile.write(
                    line[:idx]
                    + "default-name: '"
                    + line[idx:].split(':')[1].strip()
                    + "'\n"
                )
            else:
                yfile.write(line)


# Load the data from YAML
with open(CONFIG_FILE_PATH, 'r') as stream:
    data = yaml.safe_load(stream)

# Gather all AMI lookup tasks for parallel processing
ami_lookup_tasks = []

# First prepare non-Windows AMI lookup tasks
for ami_type in [t for t in data if not t.startswith('windows')]:
    for architecture_type in data[ami_type]:
        if architecture_type not in ['arm64', 'x86-64']:
            continue
        architecture_data = data[ami_type][architecture_type]
        for region in [r for r in architecture_data.keys() if r not in NON_REGION_KEYS]:
            for i, item in enumerate(architecture_data[region]):
                ami_lookup_tasks.append(
                    (region, ami_type, architecture_type, item['ss-id-suffix'], i)
                )

# Then prepare Windows AMI lookup tasks - one per Windows version per region
for windows_type in ['windows2019', 'windows2022', 'windows2025']:
    if windows_type not in data:
        continue

    for region in [
        r for r in data[windows_type]['x86-64'].keys() if r not in NON_REGION_KEYS
    ]:
        # Find the index of the base AMI in the region list
        base_ami_index = next(
            (
                i
                for i, item in enumerate(data[windows_type]['x86-64'][region])
                if item.get('ss-id-suffix') == 'base'
            ),
            0,
        )
        ami_lookup_tasks.append(
            (region, windows_type, 'x86-64', 'base', base_ami_index)
        )

# Windows AMI results cache
windows_ami_cache = {}  # format: {(windows_type, region): ami_id}

print(
    f'Starting parallel AMI lookups for {len(ami_lookup_tasks)} tasks with {MAX_WORKERS} workers...'
)

# Process AMI lookups in parallel
with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    results = list(executor.map(get_ami_worker, ami_lookup_tasks))

# Process results
for result in results:
    region, ami_type, architecture_type, ss_id_suffix, item_index, new_ami_id = result

    if not new_ami_id:
        print(
            f'No AMI found for {ami_type}/{architecture_type}/{ss_id_suffix} in {region}'
        )
        continue

    if ami_type.startswith('windows'):
        # Cache Windows AMI result
        windows_ami_cache[(ami_type, region)] = new_ami_id
    else:
        # Update non-Windows AMI directly
        old_ami_id = data[ami_type][architecture_type][region][item_index].get('ami-id')
        if new_ami_id != old_ami_id:
            print(
                f'  New AMI for {ami_type}/{architecture_type}/{ss_id_suffix} in {region}: {new_ami_id}'
            )
            data[ami_type][architecture_type][region][item_index]['ami-id'] = new_ami_id
        else:
            print(
                f'  No update to AMI for {ami_type}/{architecture_type}/{ss_id_suffix} in {region}'
            )

# Update Windows AMIs using the cache
for windows_type in ['windows2019', 'windows2022', 'windows2025']:
    if windows_type not in data:
        continue

    for region in [
        r for r in data[windows_type]['x86-64'].keys() if r not in NON_REGION_KEYS
    ]:
        new_ami_id = windows_ami_cache.get((windows_type, region))
        if not new_ami_id:
            print(f'  No AMI found for {windows_type} in {region}')
            continue

        # Update the base AMI
        item = data[windows_type]['x86-64'][region][
            0
        ]  # There's only one item per region now
        old_ami_id = item.get('ami-id')
        if new_ami_id != old_ami_id:
            print(f'  New AMI for {windows_type}/x86-64/base in {region}: {new_ami_id}')
            item['ami-id'] = new_ami_id
        else:
            print(f'  No update to AMI for {windows_type}/x86-64/base in {region}')

# Reorder keys to keep non-region keys at the beginning
for ami_type in data.keys():
    for architecture_type in data[ami_type].keys():
        non_region_items = {
            k: data[ami_type][architecture_type].pop(k)
            for k in NON_REGION_KEYS
            if k in data[ami_type][architecture_type]
        }
        data[ami_type][architecture_type] = {
            **non_region_items,
            **data[ami_type][architecture_type],
        }

# Save updated data back to YAML
with open(CONFIG_FILE_PATH, 'w') as outfile:
    yaml.dump(data, outfile, default_flow_style=False)

# Fix YAML formatting for description and name fields
format_yaml_file(CONFIG_FILE_PATH)

print('YAML file updated successfully.')
