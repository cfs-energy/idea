#!/bin/bash

# Set AMI_VALUE and STOP_ON_IDLE variables
AMI_VALUE="ami-00cda30cf72311684" ## AMI ID for Amazon Linux 2 in us-east-2. AMI ID's can be found in source/idea/idea-administrator/resources/config/region_ami_config.yml
STOP_ON_IDLE="STOP_ON_IDLE"

# Set IDEA_CLUSTER_NAME, IDEA_AWS_REGION, and IDEA_AWS_PROFILE variables
IDEA_CLUSTER_NAME="cluster-name" ### Cluster Name
IDEA_AWS_REGION="region" ### AWS Region
IDEA_AWS_PROFILE="default" ### Profile Name

# Capture YAML formatted command output for 'instance_ami' config
OUTPUT=$(./idea-admin.sh config show --cluster-name $IDEA_CLUSTER_NAME --aws-region $IDEA_AWS_REGION -q .*.instance_ami --format yaml)

# Parse YAML using Python and get all keys associated with 'instance_ami'
KEYS=$(python3 -c "
import yaml

def find_ami_keys(input_dict, path=''):
    for key, value in input_dict.items():
        new_path = f'{path}.{key}' if path else key
        if isinstance(value, dict):
            for result in find_ami_keys(value, new_path):
                yield result
        elif key.endswith('instance_ami'):
            yield new_path

output = yaml.safe_load('''$OUTPUT''')

for key in find_ami_keys(output):
    print(key)
")

# Start the command
CMD="./idea-admin.sh config set"

# Add each key to the command
while IFS= read -r KEY; do
    # Skip if key is empty
    if [[ -n "$KEY" ]]; then
        CMD+=" \"Key=$KEY,Type=string,Value=$AMI_VALUE\""
    fi
done <<< "$KEYS"

# Replace 'STOP_ALL_DAY' with 'STOP_ON_IDLE' and append to the command
OUTPUT=$(./idea-admin.sh config show --cluster-name $IDEA_CLUSTER_NAME --aws-region $IDEA_AWS_REGION -q vdc.dcv_session.schedule --format yaml)

KEYS=$(python3 -c "
import yaml

def find_schedule_keys(input_dict, path=''):
    for key, value in input_dict.items():
        new_path = f'{path}.{key}' if path else key
        if isinstance(value, dict):
            for result in find_schedule_keys(value, new_path):
                yield result
        elif value == 'STOP_ALL_DAY':
            yield new_path.rsplit('.', 1)[0] 

output = yaml.safe_load('''$OUTPUT''')

for key in find_schedule_keys(output):
    print(key)
")

while IFS= read -r KEY; do
    # Skip if key is empty
    if [[ -n "$KEY" ]]; then
        CMD+=" \"Key=$KEY.type,Type=string,Value=$STOP_ON_IDLE\""
    fi
done <<< "$KEYS"

# Add additional parameters to the command
CMD+=" --cluster-name $IDEA_CLUSTER_NAME --aws-region $IDEA_AWS_REGION --aws-profile $IDEA_AWS_PROFILE"

# Print the command
echo "Constructed command:"
echo "$CMD"

echo "Script completed."