# APIs

{% hint style="info" %}
To see all APIs available, refer to APIs section for each [Broken link](broken-reference "mention").&#x20;
{% endhint %}

All actions performed by IDEA web interface can also be triggered  via HTTP APIs. APIs cover all modules such as creating IDEA users, submit a job or control virtual desktops.

IDEA provides a Swagger documentation available on the IDEA web interface under "Module Name" > "Settings" (example below for "**eVDI**" > "**Settings**")

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-01 at 2.01.52 PM.png" alt=""><figcaption><p>API Spec and Swagger link available for each module</p></figcaption></figure>

### User Authorization <a href="#apiinterfaceguide-userauthorization" id="apiinterfaceguide-userauthorization"></a>

TO be rewritten

API Authorization is available in 4 categories:

* **Public** - As long as client has network access to the endpoint.
* **Authenticated User** - The calling user must send a valid JWT token issued by the cluster’s Cognito User Pool
* **Manager** - The user must be part of the **managers** Cognito User Group.
* **Administrator** - The user must be part of the **administrators** Cognito User Group, in addition to the Sudoers LDAP Group.

### API Samples <a href="#apiinterfaceguide-apisamples" id="apiinterfaceguide-apisamples"></a>

#### Auth.InitiateAuth (Using Username/Password) <a href="#apiinterfaceguide-auth.initiateauth-usingusername-password" id="apiinterfaceguide-auth.initiateauth-usingusername-password"></a>

InitiateAuth is a public API, that is used to authenticate the cluster user. The API may return the authentication result or challenges such as FORCE\_RESET\_PASSWORD, MFA challenge based configuration.

```http
POST <CLUSTER_ALB_ENDPOINT>/cluster-manager/api/v1 HTTP/1.1
Content-Type: application/json
```

**Username/Password Auth: Request Payload**

```json
{
    "header": {
        "namespace": "Auth.InitiateAuth"
    },
    "payload": {
        "auth_flow": "USER_PASSWORD_AUTH",
        "username": "<username>",
        "password": "<password>"
    }
}
```

**Username/Password Auth: Response Payload**

```json
{
    "header": {
        "namespace": "Auth.InitiateAuth",
        "request_id": "defc4408-922a-401c-a004-6be6f00718ee"
    },
    "success": true,
    "payload": {
        "auth": {
            "access_token": "eyJra.eyJzd...",
            "id_token": "eyJraWQiOi....",
            "refresh_token": "eyJ...",
            "expires_in": 3600,
            "token_type": "Bearer"
        }
    }
}
```

**RefreshToken Auth: Request Payload**

```json
{
    "header": {
        "namespace": "Auth.InitiateAuth",
        "request_id": "defc4408-922a-401c-a004-6be6f00718ee"
    },
    "payload": {
        "auth_flow": "REFRESH_TOKEN_AUTH",
        "username": "<username>",
        "refresh_token": "<refresh_token>"
    }
}
```

**Username/Password Auth: Response Payload**

```json
{
    "header": {
        "namespace": "Auth.InitiateAuth",
        "request_id": "defc4408-922a-401c-a004-6be6f00718ee"
    },
    "success": true,
    "payload": {
        "auth": {
            "access_token": "eyJra.eyJzd...",
            "id_token": "eyJraWQiOi....",
            "expires_in": 3600,
            "token_type": "Bearer"
        }
    }
}
```

### Authenticated API Invocations <a href="#apiinterfaceguide-authenticatedapiinvocations" id="apiinterfaceguide-authenticatedapiinvocations"></a>

To invoke authenticated APIs, set the Authorization HTTP Header with: Bearer \<access\_token>and invoke applicable APIs.

### **Examples**

#### **cURL**

<pre class="language-markup"><code class="lang-markup"> curl -s -k -L -X POST "CLUSTER_ALB_ENDPOINT/cluster-manager/api/v1" \
     --header "Authorization: Bearer $BEARER" \
     --header "Content-Type: application/json" \
     --data-raw '{
            "header": {
                "namespace": "Accounts.CreateUser"
            },
            "payload": {
                "user": {
                    "username": "newuser",
                    "password": "password",
                    "email": "email",
                    "additional_groups": ["managers-cluster-group"]
                },
                "email_verified": true
            }
<strong>}'
</strong></code></pre>

#### Python (full example - get access token and query API)

{% hint style="info" %}
Accounts.CreateUser namespace requires elevated access. Make sure to test this API with a user that belong to **manager** or **cluster-admin** groups (e.g: `clusteradmin`)
{% endhint %}

```python
import requests
import json
import sys
IDEA_ENDPOINT = "HTTPS://<DNS>"
IDEA_USER = "USER_WITH_ADMIN_PRIVILEGES"
IDEA_PASSWORD = "PASSWORD"

# Initiate Auth and retrieve Access Token

## Prepare Payload
get_auth_data = {
    "header": {
        "namespace": "Auth.InitiateAuth"
    },
    "payload": {
        "auth_flow": "USER_PASSWORD_AUTH",
        "username": IDEA_USER,
        "password": IDEA_PASSWORD
    }
}

## Prepare Header
get_auth_headers = {'Content-Type': 'application/json'}

## Submit request and retrieve access token 
get_auth_request = requests.post(f"{IDEA_ENDPOINT}/cluster-manager/api/v1",
                                 headers=get_auth_headers,
                                 data=json.dumps(get_auth_data),
                                 verify=False # in case you are using self-signed cert
                                 ).json()
if get_auth_request['success']:
    access_token = get_auth_request['payload']['auth']['access_token']
else:
    sys.exit(1)

# Query API requiring elevated permissions (Create new user account)

## Prepare Payload
create_user_data = {
            "header": {
                "namespace": "Accounts.CreateUser"
            },
            "payload": {
                "user": {
                    "username": "testuser1",
                    "password": "p@sswordTest123",
                    "email": "invalid@email.none",
                    "sudo": False
                },
                "email_verified": True
}}

## Prepare Headers
create_user_headers = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {access_token}'
}

## Submit request
create_user_request = requests.post(f'{IDEA_ENDPOINT}/cluster-manager/api/v1',
                                    data=json.dumps(create_user_data),
                                    headers=create_user_headers,
                                    verify=False # in case you are using self-signed cert
                                 ).json()

print(create_user_request)

```

Response:

```json
{
	'header': {
		'namespace': 'Accounts.CreateUser',
		'request_id': '550145c7-93c0-4f3a-96f5-c8b17095c1c0'
	},
	'success': True,
	'payload': {
		'user': {
			'username': 'testuser1',
			'email': 'invalid@email.none',
			'uid': 5004,
			'gid': 5012,
			'group_name': 'testuser1-user-group',
			'login_shell': '/bin/bash',
			'home_dir': '/data/home/testuser1',
			'sudo': False,
			'status': 'CONFIRMED',
			'enabled': True,
			'created_on': '2022-11-01T15:45:27.244000+00:00',
			'updated_on': '2022-11-01T15:45:27.244000+00:00'
		}
	}
}
```

\
