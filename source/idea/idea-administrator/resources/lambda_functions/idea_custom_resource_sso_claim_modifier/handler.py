import json

def handler(event, context):
    # Attempt to retrieve user attributes from event request safely
    user_attributes = event.get('request', {}).get('userAttributes', {})
    
    # Print the original user attributes for debugging (optional)
    # print('Original User Attributes:', json.dumps(user_attributes, indent=2))
    
    # Safely get specific attributes with default values if they're missing
    aws_region = user_attributes.get('custom:aws_region', None)
    cluster_name = user_attributes.get('custom:cluster_name', None)
    
    # In Cognito Trigger V1, Claims to ID Token can only be directly set to the response
    event['response'] = {
        "claimsOverrideDetails": {
            "claimsToAddOrOverride": {
                "custom:aws_region": aws_region,
                "custom:cluster_name": cluster_name
            }
        }
    }
    
    # Optionally print the modified event (for debugging)
    # print(json.dumps(event, indent=2))
    
    # Return the modified event
    return event
