import boto3
import yaml
import shutil
import datetime

# Generating a timestamp
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

# Make a backup of the original file
shutil.copyfile("../../source/idea/idea-administrator/resources/config/region_ami_config.yml", f"region_ami_config_backup_{timestamp}.yml")

# Function to get AMI
def get_ami(region, ami_type, profile):
    session = boto3.Session(profile_name=profile)
    ec2 = session.client('ec2', region_name=region)
    try:
        if ami_type == 'centos7':
            ami_name = 'CentOS Linux 7 x86_64 - *'
        elif ami_type == 'rhel7':
            ami_name = 'RHEL-7.9_HVM-*-x86_64-*'
        elif ami_type == 'rhel8':
            ami_name = 'RHEL-8.7.0_HVM-*-x86_64-*'
        elif ami_type == 'rhel9':
            ami_name = 'RHEL-9.2.0_HVM-*-x86_64-*'
        elif ami_type == 'rocky8':
            ami_name = 'Rocky-8-EC2-Base-8.7-*.x86_64-*'
        elif ami_type == 'rocky9':
            ami_name = 'Rocky-9-EC2-Base-9.2-*.x86_64-*'
        elif ami_type == 'amazonlinux2':
            ami_name = 'amzn2-ami-kernel-5.10-hvm-*-x86_64-gp2'
        else:
            print(f'Unknown AMI type: {ami_type}')
            return None

        response = ec2.describe_images(
            #Owners=['amazon'],
            Filters=[
                {'Name': 'name', 'Values': [ami_name]}
            ])
        ami_id = sorted(response['Images'], key=lambda x: x['CreationDate'], reverse=True)[0]['ImageId']
        return ami_id
    except Exception as e:
        print(f'   Failed to get AMI for {region} - {ami_type}. Error: {str(e)}')
        return None   

# Load the data from YAML
with open("../../source/idea/idea-administrator/resources/config/region_ami_config.yml", 'r') as stream:
    data = yaml.safe_load(stream)

# Iterate through regions
for region in data:
    print(f'Processing region: {region}')
    for ami_type in data[region]:
        ami_id = get_ami(region, ami_type, 'idea-dev')
        if ami_id:
            print(f'   New AMI for {ami_type}: {ami_id}')
            data[region][ami_type] = ami_id
        else:
            print(f'   No update to AMI for {ami_type} in {region}')

# Save updated data back to YAML
with open("../../source/idea/idea-administrator/resources/config/region_ami_config.yml", 'w') as outfile:
    yaml.dump(data, outfile, default_flow_style=False)

print('YAML file updated successfully.')
