#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

from ideasdk.utils import Utils, EnvironmentUtils
from ideadatamodel import exceptions, constants
from ideasdk.config.cluster_config import ClusterConfig
from ideasdk.context import SocaCliContext, SocaContextOptions

from typing import Optional, Dict, List, Any
import botocore.exceptions
import time

DEFAULT_USER_POOL_CLIENT_NAME = 'single-sign-on-client'
DEFAULT_IDENTITY_PROVIDER_IDENTIFIER = 'single-sign-on-identity-provider'


class SingleSignOnHelper:
    """
    Configures Single Sign-On for the Cluster

    Multiple Identity Providers for the cluster is NOT supported. Identity Provider must support SAMLv2 or OIDC.
    Disabling Single-Sign On is NOT supported.

    Helps with:
    1. Initializing the Identity Provider in the Cluster's Cognito User Pool
    2. Initializing the Cognito User Pool Client to be used for Communicating with IDP
    3. Linking existing non-system users with IDP
    """

    def __init__(self, cluster_name: str, aws_region: str, aws_profile: str = None):
        self.cluster_name = cluster_name
        self.aws_region = aws_region
        self.aws_profile = aws_profile

        self.context = SocaCliContext(
            options=SocaContextOptions(
                cluster_name=cluster_name,
                aws_region=aws_region,
                aws_profile=aws_profile,
                enable_aws_client_provider=True,
                enable_iam_permission_util=True,
                locale=EnvironmentUtils.get_environment_variable(
                    'LC_CTYPE', default='en_US'
                ),
            )
        )
        self.config: ClusterConfig = self.context.config()

    def _update_config_entry(self, key: str, value: Any):
        module_id = self.config.get_module_id(constants.MODULE_IDENTITY_PROVIDER)
        self.config.db.set_config_entry(f'{module_id}.{key}', value)
        self.config.put(f'{module_id}.{key}', value)

    def get_callback_urls(self) -> List[str]:
        """
        Build the callback URLs to be configured in the User Pool's OAuth 2.0 Client used for communicating with IDP.
        Since custom domain name can be added at a later point in time, callback urls for both the default load balancer domain name
        and the custom domain name (if available) are returned.

        The `create_or_update_user_pool_client` method can be called multiple times and will update the callback urls returned by this method.
        """
        load_balancer_dns_name = self.context.config().get_string(
            'cluster.load_balancers.external_alb.load_balancer_dns_name', required=True
        )

        custom_dns_name = self.context.config().get_string(
            'cluster.load_balancers.external_alb.certificates.custom_dns_name'
        )
        if Utils.is_empty(custom_dns_name):
            custom_dns_name = self.context.config().get_string(
                'cluster.load_balancers.external_alb.custom_dns_name'
            )

        cluster_manager_web_context_path = self.context.config().get_string(
            'cluster-manager.server.web_resources_context_path', required=True
        )
        if cluster_manager_web_context_path == '/':
            sso_auth_callback_path = '/sso/oauth2/callback'
        else:
            sso_auth_callback_path = (
                f'{cluster_manager_web_context_path}/oauth2/callback'
            )

        if not sso_auth_callback_path.startswith('/'):
            sso_auth_callback_path = f'/{sso_auth_callback_path}'

        callback_urls = [f'https://{load_balancer_dns_name}{sso_auth_callback_path}']
        if Utils.is_not_empty(custom_dns_name):
            callback_urls.append(f'https://{custom_dns_name}{sso_auth_callback_path}')
        return callback_urls

    def get_idp_redirect_uri(self, provider_type: str) -> str:
        """
        The redirect URL that must be configured with the IDP.
        """
        domain_url = self.config.get_string(
            'identity-provider.cognito.domain_url', required=True
        )
        if provider_type == constants.SSO_IDP_PROVIDER_OIDC:
            return f'{domain_url}/oauth2/idpresponse'
        else:
            return f'{domain_url}/saml2/idpresponse'

    def get_entity_id(self) -> str:
        """
        The entity id required for Azure based SAML providers.
        Refer: https://aws.amazon.com/blogs/security/how-to-set-up-amazon-cognito-for-federated-authentication-using-azure-ad/
        """
        user_pool_id = self.context.config().get_string(
            'identity-provider.cognito.user_pool_id', required=True
        )
        return f'urn:amazon:cognito:sp:{user_pool_id}'

    def create_or_update_user_pool_client(
        self, provider_name: str, refresh_token_validity_hours: int = None
    ) -> Dict:
        """
        setup the user pool client used for communicating with the IDP.

        the ClientId and ClientSecret are saved to cluster configuration and secrets manager.
        cluster configuration with clientId and secret arn is updated only once during creation.

        method can be invoked multiple times to update the user pool client if it already exists to support updating
        the callback urls
        """

        user_pool_id = self.context.config().get_string(
            'identity-provider.cognito.user_pool_id', required=True
        )
        sso_client_id = self.context.config().get_string(
            'identity-provider.cognito.sso_client_id'
        )
        if refresh_token_validity_hours is None or refresh_token_validity_hours <= 0:
            refresh_token_validity_hours = 12

        callback_urls = self.get_callback_urls()
        user_pool_client_request = {
            'UserPoolId': user_pool_id,
            'ClientName': DEFAULT_USER_POOL_CLIENT_NAME,
            'AccessTokenValidity': 1,
            'IdTokenValidity': 1,
            'RefreshTokenValidity': refresh_token_validity_hours,
            'TokenValidityUnits': {
                'AccessToken': 'hours',
                'IdToken': 'hours',
                'RefreshToken': 'hours',
            },
            'ReadAttributes': [
                'address',
                'birthdate',
                'custom:aws_region',
                'custom:cluster_name',
                'custom:password_last_set',
                'custom:password_max_age',
                'email',
                'email_verified',
                'family_name',
                'gender',
                'given_name',
                'locale',
                'middle_name',
                'name',
                'nickname',
                'phone_number',
                'phone_number_verified',
                'picture',
                'preferred_username',
                'profile',
                'updated_at',
                'website',
                'zoneinfo',
            ],
            'AllowedOAuthFlows': ['code'],
            'AllowedOAuthScopes': ['email', 'openid', 'aws.cognito.signin.user.admin'],
            'CallbackURLs': callback_urls,
            'SupportedIdentityProviders': [provider_name],
            'AllowedOAuthFlowsUserPoolClient': True,
        }

        if Utils.is_not_empty(sso_client_id):
            user_pool_client_request['ClientId'] = sso_client_id
            update_user_pool_client_result = (
                self.context.aws()
                .cognito_idp()
                .update_user_pool_client(**user_pool_client_request)
            )
            return update_user_pool_client_result['UserPoolClient']

        user_pool_client_request['GenerateSecret'] = True
        create_user_pool_client_result = (
            self.context.aws()
            .cognito_idp()
            .create_user_pool_client(**user_pool_client_request)
        )
        user_pool_client = create_user_pool_client_result['UserPoolClient']

        # get custom kms key id for secrets manager if configured
        # and add kms key id to request if available. else boto client throws validation exception for None
        kms_key_id = self.config.get_string('cluster.secretsmanager.kms_key_id')

        tags = [
            {'Key': constants.IDEA_TAG_CLUSTER_NAME, 'Value': self.cluster_name},
            {
                'Key': constants.IDEA_TAG_MODULE_NAME,
                'Value': constants.MODULE_CLUSTER_MANAGER,
            },
        ]

        secret_name = f'{self.cluster_name}-sso-client-secret'
        try:
            describe_secret_result = (
                self.context.aws()
                .secretsmanager()
                .describe_secret(SecretId=secret_name)
            )
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                describe_secret_result = None
            else:
                raise e

        if describe_secret_result is None:
            create_secret_client_secret_request = {
                'Name': f'{self.cluster_name}-sso-client-secret',
                'Description': f'Single Sign-On OAuth2 Client Secret for Cluster: {self.cluster_name}',
                'Tags': tags,
                'SecretString': user_pool_client['ClientSecret'],
            }
            if Utils.is_not_empty(kms_key_id):
                create_secret_client_secret_request['KmsKeyId'] = kms_key_id
            create_secret_client_secret_result = (
                self.context.aws()
                .secretsmanager()
                .create_secret(**create_secret_client_secret_request)
            )
            secret_arn = create_secret_client_secret_result['ARN']
        else:
            update_secret_client_secret_request = {
                'SecretId': describe_secret_result['ARN'],
                'SecretString': user_pool_client['ClientSecret'],
            }
            if Utils.is_not_empty(kms_key_id):
                update_secret_client_secret_request['KmsKeyId'] = kms_key_id
            update_secret_client_secret_result = (
                self.context.aws()
                .secretsmanager()
                .update_secret(**update_secret_client_secret_request)
            )
            secret_arn = update_secret_client_secret_result['ARN']

        self._update_config_entry('cognito.sso_client_id', user_pool_client['ClientId'])
        self._update_config_entry('cognito.sso_client_secret', secret_arn)
        return user_pool_client

    @staticmethod
    def get_saml_provider_details(**kwargs) -> Dict:
        """
        build the SAML provider details based on user input
        the `saml_metadata_file` must be a path to the file on local file system, which is read and contents are sent to boto API.

        refer to below link for more details:
        https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cognito-idp.html#CognitoIdentityProvider.Client.create_identity_provider
        """
        provider_details = {}
        saml_metadata_url = Utils.get_value_as_string('saml_metadata_url', kwargs)
        saml_metadata_file = Utils.get_value_as_string('saml_metadata_file', kwargs)
        if Utils.are_empty(saml_metadata_url, saml_metadata_file):
            raise exceptions.invalid_params(
                'Either one of [saml_metadata_url, saml_metadata_file] is required, when provider_type = SAML'
            )

        if Utils.is_not_empty(saml_metadata_file):
            if not Utils.is_file(saml_metadata_file):
                raise exceptions.invalid_params(f'file not found: {saml_metadata_file}')

            with open(saml_metadata_file, 'r') as f:
                saml_metadata_file_contents = f.read()

            provider_details['MetadataFile'] = saml_metadata_file_contents
        else:
            provider_details['MetadataURL'] = saml_metadata_url
        return provider_details

    @staticmethod
    def get_oidc_provider_details(**kwargs) -> Dict:
        """
        build the OIDC provider details based on user input.

        refer to below link for more details:
        https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cognito-idp.html#CognitoIdentityProvider.Client.create_identity_provider
        """

        provider_details = {}
        oidc_client_id = Utils.get_value_as_string('oidc_client_id', kwargs)
        oidc_client_secret = Utils.get_value_as_string('oidc_client_secret', kwargs)
        oidc_issuer = Utils.get_value_as_string('oidc_issuer', kwargs)
        oidc_attributes_request_method = Utils.get_value_as_string(
            'oidc_attributes_request_method', kwargs, 'GET'
        )
        oidc_authorize_scopes = Utils.get_value_as_string(
            'oidc_authorize_scopes', kwargs, 'openid'
        )
        oidc_authorize_url = Utils.get_value_as_string('oidc_authorize_url', kwargs)
        oidc_token_url = Utils.get_value_as_string('oidc_token_url', kwargs)
        oidc_attributes_url = Utils.get_value_as_string('oidc_attributes_url', kwargs)
        oidc_jwks_uri = Utils.get_value_as_string('oidc_jwks_uri', kwargs)

        # Replace ',' with space in scopes
        oidc_authorize_scopes = oidc_authorize_scopes.replace(',', ' ')

        if Utils.is_empty(oidc_client_id):
            raise exceptions.invalid_params('oidc_client_id is required')
        if Utils.is_empty(oidc_client_secret):
            raise exceptions.invalid_params('oidc_client_secret is required')
        if Utils.is_empty(oidc_issuer):
            raise exceptions.invalid_params('oidc_issuer is required')

        provider_details['client_id'] = oidc_client_id
        provider_details['client_secret'] = oidc_client_secret
        provider_details['attributes_request_method'] = oidc_attributes_request_method
        provider_details['authorize_scopes'] = oidc_authorize_scopes
        provider_details['oidc_issuer'] = oidc_issuer
        if Utils.is_not_empty(oidc_authorize_url):
            provider_details['authorize_url'] = oidc_authorize_url
        if Utils.is_not_empty(oidc_token_url):
            provider_details['token_url'] = oidc_token_url
        if Utils.is_not_empty(oidc_attributes_url):
            provider_details['attributes_url'] = oidc_attributes_url
        if Utils.is_not_empty(oidc_jwks_uri):
            provider_details['jwks_uri'] = oidc_jwks_uri

        return provider_details

    def get_identity_provider(self) -> Optional[Dict]:
        """
        method to check if SSO identity provider is already created to ensure only a single IDP is created for the cluster.
        The `identity-provider.cognito.sso_idp_identifier` config entry value is used as the identifier. if the entry does not exist,
        value of `DEFAULT_IDENTITY_PROVIDER_IDENTIFIER` is used.
        """
        user_pool_id = self.context.config().get_string(
            'identity-provider.cognito.user_pool_id', required=True
        )
        sso_idp_identifier = self.context.config().get_string(
            'identity-provider.cognito.sso_idp_identifier',
            DEFAULT_IDENTITY_PROVIDER_IDENTIFIER,
        )
        try:
            result = (
                self.context.aws()
                .cognito_idp()
                .get_identity_provider_by_identifier(
                    UserPoolId=user_pool_id, IdpIdentifier=sso_idp_identifier
                )
            )
            return result['IdentityProvider']
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return None
            else:
                raise e

    def create_or_update_identity_provider(
        self,
        provider_name: str,
        provider_type: str,
        provider_email_attribute: str,
        **kwargs,
    ):
        """
        setup the Identity Provider for the Cognito User Pool.
        at the moment, only OIDC and SAML provider types are supported.

        provider details are built using the `get_saml_provider_details` for SAML and `get_oidc_provider_details` for OIDC.

        Important:
        `identity-provider.cognito.sso_idp_provider_name` config entry is updated with the name of the IDP provider.
        changing this value will result in adding multiple IDPs for the cluster and will result in broken SSO functionality.
        as part of AccountsService during user creation, if SSO is enabled, the new user will be linked with the IDP Provider name
         specified in `identity-provider.cognito.sso_idp_provider_name`
        """

        if Utils.is_empty(provider_name):
            raise exceptions.invalid_params('provider_name is required')
        if Utils.is_empty(provider_type):
            raise exceptions.invalid_params('provider_type is required')
        if Utils.is_empty(provider_email_attribute):
            raise exceptions.invalid_params('provider_email_attribute is required')

        if provider_type == constants.SSO_IDP_PROVIDER_SAML:
            provider_details = self.get_saml_provider_details(**kwargs)
        elif provider_type == constants.SSO_IDP_PROVIDER_OIDC:
            provider_details = self.get_oidc_provider_details(**kwargs)
        else:
            raise exceptions.invalid_params(
                'provider type must be one of: SAML or OIDC'
            )

        user_pool_id = self.context.config().get_string(
            'identity-provider.cognito.user_pool_id', required=True
        )

        existing_identity_provider = self.get_identity_provider()
        sso_idp_identifier = self.context.config().get_string(
            'identity-provider.cognito.sso_idp_identifier',
            DEFAULT_IDENTITY_PROVIDER_IDENTIFIER,
        )
        if existing_identity_provider is not None:
            self.context.aws().cognito_idp().update_identity_provider(
                UserPoolId=user_pool_id,
                ProviderName=provider_name,
                ProviderDetails=provider_details,
                AttributeMapping={'email': provider_email_attribute},
                IdpIdentifiers=[sso_idp_identifier],
            )
        else:
            self.context.aws().cognito_idp().create_identity_provider(
                UserPoolId=user_pool_id,
                ProviderName=provider_name,
                ProviderType=provider_type,
                ProviderDetails=provider_details,
                AttributeMapping={'email': provider_email_attribute},
                IdpIdentifiers=[sso_idp_identifier],
            )

        self._update_config_entry('cognito.sso_idp_provider_name', provider_name)
        self._update_config_entry('cognito.sso_idp_provider_type', provider_type)
        self._update_config_entry('cognito.sso_idp_identifier', sso_idp_identifier)
        self._update_config_entry(
            'cognito.sso_idp_provider_email_attribute', provider_email_attribute
        )

    def link_existing_users(self):
        """
        if there are existing users in the user pool, added prior to enabling SSO, link all the existing users with IDP.
        this ensures SSO works for existing users that were created before enabling IDP.

        system administration users such as clusteradmin are not linked with IDP with an assumption that no such users will exist
        in corporate directory service.

        when SSO is enabled, accessing the cluster endpoint will result in signing-in automatically.
        to ensure clusteradmin user can sign-in, a special query parameter can be provided:
        `sso=False`
        sending this parameter as part of query string will not trigger the sso flow from the web portal.
        """
        user_pool_id = self.context.config().get_string(
            'identity-provider.cognito.user_pool_id', required=True
        )
        provider_name = self.context.config().get_string(
            'identity-provider.cognito.sso_idp_provider_name', required=True
        )
        provider_type = self.context.config().get_string(
            'identity-provider.cognito.sso_idp_provider_type', required=True
        )
        cluster_admin_username = self.context.config().get_string(
            'cluster.administrator_username', required=True
        )

        if provider_type == constants.SSO_IDP_PROVIDER_OIDC:
            provider_email_attribute = 'email'
        else:
            provider_email_attribute = self.context.config().get_string(
                'identity-provider.cognito.sso_idp_provider_email_attribute',
                required=True,
            )

        while True:
            list_users_result = (
                self.context.aws().cognito_idp().list_users(UserPoolId=user_pool_id)
            )
            users = list_users_result['Users']
            for user in users:
                try:
                    if user['UserStatus'] == 'EXTERNAL_PROVIDER':
                        continue

                    username = user['Username']

                    # exclude system administration users
                    if username in cluster_admin_username or username.startswith(
                        'clusteradmin'
                    ):
                        print(
                            f'system administration user found: {username}. skip linking with IDP.'
                        )
                        continue

                    email = None
                    already_linked = False
                    user_attributes = Utils.get_value_as_list('Attributes', user, [])
                    for user_attribute in user_attributes:
                        name = user_attribute['Name']
                        if name == 'email':
                            email = Utils.get_value_as_string('Value', user_attribute)
                        elif name == 'identities':
                            identities = Utils.get_value_as_list(
                                'Value', user_attribute, []
                            )
                            for identity in identities:
                                if identity['providerName'] == provider_name:
                                    already_linked = True

                    if Utils.is_empty(email):
                        continue
                    if already_linked:
                        print(f'user: {username}, email: {email} already linked. skip.')
                        continue

                    print(f'linking user: {username}, email: {email} ...')

                    def admin_link_provider_for_user(**kwargs):
                        print(f'link request: {Utils.to_json(kwargs)}')
                        self.context.aws().cognito_idp().admin_link_provider_for_user(
                            **kwargs
                        )

                    admin_link_provider_for_user(
                        UserPoolId=user_pool_id,
                        DestinationUser={
                            'ProviderName': 'Cognito',
                            'ProviderAttributeName': 'cognito:username',
                            'ProviderAttributeValue': user['Username'],
                        },
                        SourceUser={
                            'ProviderName': provider_name,
                            'ProviderAttributeName': provider_email_attribute,
                            'ProviderAttributeValue': email,
                        },
                    )
                    # sleep for a while to avoid flooding aws with these requests.
                    time.sleep(0.2)
                except Exception as e:
                    print(
                        f'failed to link user: {user} with IDP: {provider_name} - {e}'
                    )

            pagination_token = Utils.get_value_as_string(
                'PaginationToken', list_users_result
            )
            if Utils.is_empty(pagination_token):
                break

    def configure_sso(self, provider_name: str, provider_type: str, **kwargs):
        """
        execute series of steps to configure Single Sign-On for the cluster.

        `identity-provider.cognito.sso_enabled` boolean config entry is set to True at the end of the flow.
        this config entry is the single place in the cluster to indicate SSO is enabled.
        """
        # identity provider
        with self.context.spinner('creating identity provider ...'):
            self.create_or_update_identity_provider(
                provider_name=provider_name, provider_type=provider_type, **kwargs
            )
        self.context.success('✓ identity provider created')

        # user pool client
        with self.context.spinner('creating user pool client ...'):
            self.create_or_update_user_pool_client(
                provider_name=provider_name,
                refresh_token_validity_hours=Utils.get_value_as_int(
                    'refresh_token_validity_hours', kwargs, default=None
                ),
            )
        self.context.success('✓ user pool client created')

        # link existing users
        with self.context.spinner('linking existing users with IDP ...'):
            self.link_existing_users()
        self.context.success('✓ existing users linked with IDP')

        # update cluster settings - this must be last step in the pipeline.
        with self.context.spinner('enabling Single Sign-On ...'):
            self._update_config_entry('cognito.sso_enabled', True)
        self.context.success('✓ Single Sign-On enabled for cluster')
