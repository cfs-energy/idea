import boto3
import oyaml as yaml
import shutil
import datetime
import json

# Generating a timestamp
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

# Make a backup of the original file
shutil.copyfile("../../source/idea/idea-virtual-desktop-controller/resources/base-software-stack-config.yaml", f"base-software-stack-config_backup_{timestamp}.yaml")

NON_REGION_KEYS = ['default-description', 'default-min-ram-unit', 'default-min-ram-value', 'default-min-storage-unit', 'default-min-storage-value', 'default-name']

# Function to get AMI
def get_ami(region, ami_type, architecture_type, ss_id_suffix):
    profile = 'gov' if region == 'us-gov-west-1' else 'idea-dev'
    print(f'Getting AMI for Region: {region}, AMI Type: {ami_type}, Architecture: {architecture_type}, Profile: {profile}, Suffix: {ss_id_suffix}')  # Debug line

    try:
        if ami_type == 'windows':
            if ss_id_suffix == 'base':
                ami_name = 'DCV-Windows-*'
            elif ss_id_suffix == 'base-nvidia':
                ami_name = 'DCV-Windows-*-NVIDIA-*'
            elif ss_id_suffix == 'base-amd':
                ami_name = 'DCV-Windows-*-AMD-*'
        # elif ami_type == 'centos7':
        #     if architecture_type == 'arm64':
        #         ami_name = 'CentOS Linux 7 aarch64 - *'
        #     elif architecture_type == 'x86-64':
        #         ami_name = 'CentOS Linux 7 x86_64 - *'
        # elif ami_type == 'rhel7':
        #     if architecture_type == 'arm64':
        #         ami_name = ''
        #     elif architecture_type == 'x86-64':
        #         ami_name = 'RHEL-7.9_HVM-*-x86_64-*'
        elif ami_type == 'rhel8':
            if architecture_type == 'arm64':
                ami_name = 'RHEL-8.10.0_HVM-*-arm64-*'
            elif architecture_type == 'x86-64':
                ami_name = 'RHEL-8.10.0_HVM-*-x86_64-*'
        elif ami_type == 'rhel9':
            if architecture_type == 'arm64':
                ami_name = 'RHEL-9.4.0_HVM-*-arm64-*'
            elif architecture_type == 'x86-64':
                ami_name = 'RHEL-9.4.0_HVM-*-x86_64-*'
        elif ami_type == 'ubuntu2204':
            if architecture_type == 'arm64':
                ami_name = 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*'
            elif architecture_type == 'x86-64':
                ami_name = 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*'
        elif ami_type == 'ubuntu2404':
            if architecture_type == 'arm64':
                ami_name = 'ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*'
            elif architecture_type == 'x86-64':
                ami_name = 'ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*'
        # elif ami_type == 'rocky8':
        #     if architecture_type == 'arm64':
        #         ami_name = 'Rocky-8-EC2-Base-8.7-*.aarch64-*'
        #     elif architecture_type == 'x86-64':
        #         ami_name = 'Rocky-8-EC2-Base-8.7-*.x86_64-*'
        # elif ami_type == 'rocky9':
        #     if architecture_type == 'arm64':
        #         ami_name = 'Rocky-9-EC2-Base-9.2-*.aarch64-*'
        #     elif architecture_type == 'x86-64':
        #         ami_name = 'Rocky-9-EC2-Base-9.2-*.x86_64-*'
        elif ami_type == 'amazonlinux2':
            if architecture_type == 'arm64':
                ami_name = 'amzn2-ami-kernel-5.10-hvm-*-arm64-gp2'
            elif architecture_type == 'x86-64':
                ami_name = 'amzn2-ami-kernel-5.10-hvm-*-x86_64-gp2'
        else:
            print(f'Unknown AMI type: {ami_type}')
            return None

        print(f'AMI name filter: {ami_name}')
        
        session = boto3.Session(profile_name=profile)
        ec2 = session.client('ec2', region_name=region)
        response = ec2.describe_images(
            Owners=['amazon'],
            Filters=[
                {'Name': 'name', 'Values': [ami_name]},
            ])

        if ss_id_suffix == 'base' and ami_type == 'windows':
            valid_amis = [ami for ami in response['Images'] if 'ImageLocation' in ami and 'gaming' not in ami['ImageLocation'] and 
                        'AMD' not in ami['ImageLocation'] and 'NVIDIA' not in ami['ImageLocation']]
        else:
            valid_amis = [ami for ami in response['Images'] if 'ImageLocation' in ami and 'gaming' not in ami['ImageLocation']]

        ami_id = sorted(valid_amis, key=lambda x: x['CreationDate'], reverse=True)[0]['ImageId']
        return ami_id
    except Exception as e:
        print(f'   Failed to get AMI for {region} - {ami_type}/{architecture_type}. Error: {str(e)}')
        return None   

with open("../../source/idea/idea-virtual-desktop-controller/resources/base-software-stack-config.yaml", 'r') as stream:
    data = yaml.safe_load(stream)

for ami_type in data:
    for architecture_type in data[ami_type]:
        if architecture_type not in ["arm64", "x86-64"]:
            continue
        architecture_data = data[ami_type][architecture_type]
        for key in architecture_data:
            if key in NON_REGION_KEYS:
                continue
            for item in architecture_data[key]:
                old_ami_id = item.get('ami-id')
                new_ami_id = get_ami(key, ami_type, architecture_type, item['ss-id-suffix'])
                if new_ami_id and new_ami_id != old_ami_id:
                    print(f'  New AMI for {ami_type}/{architecture_type}/{item["ss-id-suffix"]}: {new_ami_id}')
                    item['ami-id'] = new_ami_id
                else:
                    print(f'  No update to AMI for {ami_type}/{architecture_type}/{item["ss-id-suffix"]} in {key}')

for ami_type in data.keys():
    for architecture_type in data[ami_type].keys():
        non_region_items = {k: data[ami_type][architecture_type].pop(k) for k in NON_REGION_KEYS if k in data[ami_type][architecture_type]}
        data[ami_type][architecture_type] = {**non_region_items, **data[ami_type][architecture_type]}

with open("../../source/idea/idea-virtual-desktop-controller/resources/base-software-stack-config.yaml", 'w') as outfile:
    yaml.dump(data, outfile, default_flow_style=False) 

with open("../../source/idea/idea-virtual-desktop-controller/resources/base-software-stack-config.yaml", 'r') as yfile:
    lines = yfile.readlines()

with open("../../source/idea/idea-virtual-desktop-controller/resources/base-software-stack-config.yaml", 'w') as yfile:
    for line in lines:
        if 'default-description:' in line:
            idx = line.index('default-description:')
            yfile.write(line[:idx] + 'default-description: \'' + line[idx:].split(":")[1].strip() + "'\n")
        elif 'default-name:' in line:
            idx = line.index('default-name:')
            yfile.write(line[:idx] + 'default-name: \'' + line[idx:].split(":")[1].strip() + "'\n")
        else:
            yfile.write(line)

print('YAML file updated successfully.')