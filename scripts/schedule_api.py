import requests
import json
import sys

# Command line argument for update mode
update_mode = '--update' in sys.argv

# API details
IDEA_ENDPOINT = 'https://your_idea_url'
IDEA_USER = 'an_idea_admin'
IDEA_PASSWORD = 'an_idea_password'

# Print update mode status
print(f'Update Mode: {"ON" if update_mode else "OFF"}')

# Initiate Auth and retrieve Access Token
get_auth_data = {
    'header': {'namespace': 'Auth.InitiateAuth'},
    'payload': {
        'auth_flow': 'USER_PASSWORD_AUTH',
        'username': IDEA_USER,
        'password': IDEA_PASSWORD,
    },
}
get_auth_headers = {'Content-Type': 'application/json'}

try:
    get_auth_response = requests.post(
        f'{IDEA_ENDPOINT}/cluster-manager/api/v1',
        headers=get_auth_headers,
        data=json.dumps(get_auth_data),
        verify=False,  # in case you are using self-signed cert
    ).json()

    print('Authentication Request Sent. Checking for success...')
    if get_auth_response['success']:
        access_token = get_auth_response['payload']['auth']['access_token']
        print('Authentication Successful.')
    else:
        print('Failed to authenticate.')
        sys.exit(1)
except Exception as e:
    print(f'Error during authentication: {e}')
    sys.exit(1)

# Prepare API call to retrieve session data
list_session_data = {
    'header': {'namespace': 'VirtualDesktopAdmin.ListSessions'},
    'payload': {'filters': [], 'paginator': {'start': 0, 'page_size': 1000}},
}
headers = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {access_token}',
}

try:
    list_sessions_response = requests.post(
        f'{IDEA_ENDPOINT}/vdc/api/v1',
        data=json.dumps(list_session_data),
        headers=headers,
        verify=False,
    ).json()

    print('Session List Request Sent. Processing data...')
    for listing in list_sessions_response['payload']['listing']:
        idea_session_id = listing['idea_session_id']
        owner = listing['owner']
        print(f'Processing session for: {owner} with ID: {idea_session_id}')

        if update_mode:
            update_session_data = {
                'header': {'namespace': 'VirtualDesktopAdmin.UpdateSession'},
                'payload': {
                    'session': {
                        'idea_session_id': idea_session_id,
                        'owner': owner,
                        'schedule': {
                            'monday': {'schedule_type': 'STOP_ON_IDLE'},
                            'tuesday': {'schedule_type': 'STOP_ON_IDLE'},
                            'wednesday': {'schedule_type': 'STOP_ON_IDLE'},
                            'thursday': {'schedule_type': 'STOP_ON_IDLE'},
                            'friday': {'schedule_type': 'STOP_ON_IDLE'},
                            'saturday': {'schedule_type': 'STOP_ON_IDLE'},
                            'sunday': {'schedule_type': 'STOP_ON_IDLE'},
                        },
                    }
                },
            }

            # Make the POST request to update session details
            update_response = requests.post(
                f'{IDEA_ENDPOINT}/vdc/api/v1',
                data=json.dumps(update_session_data),
                headers=headers,
                verify=False,
            )
            print(f'Updated session for {owner}. Response: {update_response.json()}')
        else:
            print(
                'Update mode is off - No actual update performed. Re-run with --update to make actual changes.'
            )
except Exception as e:
    print(f'Error processing session data: {e}')
