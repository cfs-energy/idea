import boto3
import yaml
import shutil
import datetime
from typing import Optional
from botocore.exceptions import ClientError

# Define file paths as constants
CONFIG_FILE_PATH = (
    '../../source/idea/idea-administrator/resources/config/region_ami_config.yml'
)

# Define AMI name mappings
AMI_NAME_MAPPINGS = {
    # "rhel7": "RHEL-7.9_HVM-*-x86_64-*",
    'rhel8': 'RHEL-8.10.0_HVM-*-x86_64-*',
    'rhel9': 'RHEL-9.5.0_HVM-*-x86_64-*',
    'rocky8': 'Rocky-8-EC2-Base-8.9-*.x86_64-*',
    'rocky9': 'Rocky-9-EC2-Base-9.5-*.x86_64-*',
    # "amazonlinux2": "amzn2-ami-kernel-5.10-hvm-*-x86_64-gp2",
    'amazonlinux2023': 'al2023-ami-2023.7.*-kernel-6.1-x86_64',
}

# Generating a timestamp
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

# Make a backup of the original file
shutil.copyfile(CONFIG_FILE_PATH, f'region_ami_config_backup_{timestamp}.yml')


# Function to get AMI
def get_ami(region: str, ami_type: str) -> Optional[str]:
    profile = 'gov' if region == 'us-gov-west-1' else 'idea-dev'
    session = boto3.Session(profile_name=profile)
    ec2 = session.client('ec2', region_name=region)
    try:
        if ami_type not in AMI_NAME_MAPPINGS:
            print(f'Unknown AMI type: {ami_type}')
            return None

        ami_name = AMI_NAME_MAPPINGS[ami_type]

        # Use aws-marketplace only for rocky8 and rocky9
        if ami_type in ['rocky8', 'rocky9']:
            owners = ['aws-marketplace']
        else:
            owners = ['amazon']

        response = ec2.describe_images(
            Owners=owners,
            Filters=[{'Name': 'name', 'Values': [ami_name]}],
        )

        # Check if we got any images back
        if not response['Images']:
            print(
                f'   No images found for {region} - {ami_type} with pattern {ami_name}'
            )
            return None

        ami_id = sorted(
            response['Images'], key=lambda x: x['CreationDate'], reverse=True
        )[0]['ImageId']
        return ami_id
    except ClientError as e:
        print(f'   Failed to get AMI for {region} - {ami_type}. Error: {str(e)}')
        return None
    except Exception as e:
        print(f'   Failed to get AMI for {region} - {ami_type}. Error: {str(e)}')
        return None


# Load the data from YAML
with open(CONFIG_FILE_PATH, 'r') as stream:
    data = yaml.safe_load(stream)

# Iterate through regions
for region in data:
    print(f'Processing region: {region}')
    for ami_type in data[region]:
        ami_id = get_ami(region, ami_type)
        if ami_id:
            print(f'   New AMI for {ami_type}: {ami_id}')
            data[region][ami_type] = ami_id
        else:
            print(f'   No update to AMI for {ami_type} in {region}')

# Save updated data back to YAML
with open(CONFIG_FILE_PATH, 'w') as outfile:
    yaml.dump(data, outfile, default_flow_style=False)

print('YAML file updated successfully.')
