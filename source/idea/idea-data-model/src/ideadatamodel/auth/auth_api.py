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
__all__ = (
    'CreateUserRequest',
    'CreateUserResult',
    'GetUserRequest',
    'GetUserResult',
    'ModifyUserRequest',
    'ModifyUserResult',
    'DeleteUserRequest',
    'DeleteUserResult',
    'EnableUserRequest',
    'EnableUserResult',
    'DisableUserRequest',
    'DisableUserResult',
    'ListUsersRequest',
    'ListUsersResult',
    'InitiateAuthRequest',
    'InitiateAuthResult',
    'RespondToAuthChallengeRequest',
    'RespondToAuthChallengeResult',
    'ForgotPasswordRequest',
    'ForgotPasswordResult',
    'ChangePasswordRequest',
    'ChangePasswordResult',
    'ResetPasswordRequest',
    'ResetPasswordResult',
    'ConfirmForgotPasswordRequest',
    'ConfirmForgotPasswordResult',
    'SignOutRequest',
    'SignOutResult',
    'GlobalSignOutRequest',
    'GlobalSignOutResult',
    'CreateGroupRequest',
    'CreateGroupResult',
    'ModifyGroupRequest',
    'ModifyGroupResult',
    'DeleteGroupRequest',
    'DeleteGroupResult',
    'EnableGroupRequest',
    'EnableGroupResult',
    'DisableGroupRequest',
    'DisableGroupResult',
    'GetGroupRequest',
    'GetGroupResult',
    'ListGroupsRequest',
    'ListGroupsResult',
    'AddUserToGroupRequest',
    'AddUserToGroupResult',
    'RemoveUserFromGroupRequest',
    'RemoveUserFromGroupResult',
    'ListUsersInGroupRequest',
    'ListUsersInGroupResult',
    'AddSudoUserRequest',
    'AddSudoUserResult',
    'RemoveSudoUserRequest',
    'RemoveSudoUserResult',
    'AuthenticateUserRequest',
    'AuthenticateUserResult',
    'GetUserPrivateKeyRequest',
    'GetUserPrivateKeyResult',
    'OPEN_API_SPEC_ENTRIES_AUTH'
)

from pydantic import Field

from ideadatamodel.api import SocaPayload, SocaListingPayload, IdeaOpenAPISpecEntry
from ideadatamodel.auth.auth_model import User, Group, AuthResult

from typing import Optional, List, Dict


# CreateUser

class CreateUserRequest(SocaPayload):
    user: Optional[User] = Field(default=None)
    email_verified: Optional[bool] = Field(default=None)


class CreateUserResult(SocaPayload):
    user: Optional[User] = Field(default=None)


# GetUser

class GetUserRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class GetUserResult(SocaPayload):
    user: Optional[User] = Field(default=None)


# ModifyUser

class ModifyUserRequest(SocaPayload):
    user: Optional[User] = Field(default=None)
    email_verified: Optional[bool] = Field(default=None)


class ModifyUserResult(SocaPayload):
    user: Optional[User] = Field(default=None)


# DeleteUser

class DeleteUserRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class DeleteUserResult(SocaPayload):
    pass


# EnableUser

class EnableUserRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class EnableUserResult(SocaPayload):
    user: Optional[User] = Field(default=None)


# DisableUser

class DisableUserRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class DisableUserResult(SocaPayload):
    user: Optional[User] = Field(default=None)


# ListUsers

class ListUsersRequest(SocaListingPayload):
    pass


class ListUsersResult(SocaListingPayload):
    listing: Optional[List[User]] = Field(default=None)


# InitiateAuth

class InitiateAuthRequest(SocaPayload):
    client_id: Optional[str] = Field(default=None)
    auth_flow: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    refresh_token: Optional[str] = Field(default=None)
    authorization_code: Optional[str] = Field(default=None)


class InitiateAuthResult(SocaPayload):
    challenge_name: Optional[str] = Field(default=None)
    session: Optional[str] = Field(default=None)
    challenge_params: Optional[Dict] = Field(default=None)
    auth: Optional[AuthResult] = Field(default=None)


# RespondToAuthChallenge

class RespondToAuthChallengeRequest(SocaPayload):
    client_id: Optional[str] = Field(default=None)
    session: Optional[str] = Field(default=None)
    challenge_name: Optional[str] = Field(default=None)
    challenge_params: Optional[Dict] = Field(default=None)
    username: Optional[str] = Field(default=None)
    new_password: Optional[str] = Field(default=None)


class RespondToAuthChallengeResult(SocaPayload):
    challenge_name: Optional[str] = Field(default=None)
    session: Optional[str] = Field(default=None)
    challenge_params: Optional[Dict] = Field(default=None)
    auth: Optional[AuthResult] = Field(default=None)


# ForgotPassword

class ForgotPasswordRequest(SocaPayload):
    client_id: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)


class ForgotPasswordResult(SocaPayload):
    pass


# ChangePassword

class ChangePasswordRequest(SocaPayload):
    username: Optional[str] = Field(default=None)
    old_password: Optional[str] = Field(default=None)
    new_password: Optional[str] = Field(default=None)


class ChangePasswordResult(SocaPayload):
    pass


# ResetPassword

class ResetPasswordRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class ResetPasswordResult(SocaPayload):
    pass


# ConfirmForgotPassword

class ConfirmForgotPasswordRequest(SocaPayload):
    client_id: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)
    confirmation_code: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)


class ConfirmForgotPasswordResult(SocaPayload):
    pass


# SignOut

class SignOutRequest(SocaPayload):
    refresh_token: Optional[str] = Field(default=None)
    sso_auth: Optional[bool] = Field(default=None)


class SignOutResult(SocaPayload):
    pass


# GlobalSignOut

class GlobalSignOutRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class GlobalSignOutResult(SocaPayload):
    pass


# CreateGroup

class CreateGroupRequest(SocaPayload):
    group: Optional[Group] = Field(default=None)


class CreateGroupResult(SocaPayload):
    group: Optional[Group] = Field(default=None)


# ModifyGroup

class ModifyGroupRequest(SocaPayload):
    group: Optional[Group] = Field(default=None)


class ModifyGroupResult(SocaPayload):
    group: Optional[Group] = Field(default=None)


# DeleteGroup

class DeleteGroupRequest(SocaPayload):
    group_name: Optional[str] = Field(default=None)


class DeleteGroupResult(SocaPayload):
    pass


# EnableGroup

class EnableGroupRequest(SocaPayload):
    group_name: Optional[str] = Field(default=None)


class EnableGroupResult(SocaPayload):
    pass


# DisableGroup

class DisableGroupRequest(SocaPayload):
    group_name: Optional[str] = Field(default=None)


class DisableGroupResult(SocaPayload):
    pass


# GetGroup

class GetGroupRequest(SocaPayload):
    group_name: Optional[str] = Field(default=None)


class GetGroupResult(SocaPayload):
    group: Optional[Group] = Field(default=None)


# ListGroups

class ListGroupsRequest(SocaListingPayload):
    username: Optional[str] = Field(default=None)


class ListGroupsResult(SocaListingPayload):
    listing: Optional[List[Group]] = Field(default=None)


# AddUserToGroup

class AddUserToGroupRequest(SocaPayload):
    usernames: Optional[List[str]] = Field(default=None)
    group_name: Optional[str] = Field(default=None)


class AddUserToGroupResult(SocaPayload):
    pass


# RemoveUserFromGroup

class RemoveUserFromGroupRequest(SocaPayload):
    usernames: Optional[List[str]] = Field(default=None)
    group_name: Optional[str] = Field(default=None)


class RemoveUserFromGroupResult(SocaPayload):
    group: Optional[Group] = Field(default=None)


# ListUsersInGroup

class ListUsersInGroupRequest(SocaListingPayload):
    group_names: Optional[List[str]] = Field(default=None)


class ListUsersInGroupResult(SocaListingPayload):
    listing: Optional[List[User]] = Field(default=None)


# AddSudoUser

class AddSudoUserRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class AddSudoUserResult(SocaPayload):
    user: Optional[User] = Field(default=None)


# RemoveSudoUser

class RemoveSudoUserRequest(SocaPayload):
    username: Optional[str] = Field(default=None)


class RemoveSudoUserResult(SocaPayload):
    user: Optional[User] = Field(default=None)


# AuthenticateUser

class AuthenticateUserRequest(SocaPayload):
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)


class AuthenticateUserResult(SocaPayload):
    status: Optional[bool] = Field(default=None)


# GetUserPrivateKey

class GetUserPrivateKeyRequest(SocaPayload):
    key_format: Optional[str] = Field(default=None)  # pem, ppk
    platform: Optional[str] = Field(default=None)


class GetUserPrivateKeyResult(SocaPayload):
    name: Optional[str] = Field(default=None)
    key_material: Optional[str] = Field(default=None)


OPEN_API_SPEC_ENTRIES_AUTH = [
    IdeaOpenAPISpecEntry(
        namespace='Accounts.CreateUser',
        request=CreateUserRequest,
        result=CreateUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.GetUser',
        request=GetUserRequest,
        result=GetUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.ModifyUser',
        request=ModifyUserRequest,
        result=ModifyUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.EnableUser',
        request=EnableUserRequest,
        result=EnableUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.DisableUser',
        request=DisableUserRequest,
        result=DisableUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.DeleteUser',
        request=DeleteUserRequest,
        result=DeleteUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.ListUsers',
        request=ListUsersRequest,
        result=ListUsersResult,
        is_listing=True,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.CreateGroup',
        request=CreateGroupRequest,
        result=CreateGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.GetGroup',
        request=GetGroupRequest,
        result=GetGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.ModifyGroup',
        request=ModifyGroupRequest,
        result=ModifyGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.EnableGroup',
        request=EnableGroupRequest,
        result=EnableGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.DisableGroup',
        request=DisableGroupRequest,
        result=DisableGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.DeleteGroup',
        request=DeleteGroupRequest,
        result=DeleteGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.AddUserToGroup',
        request=AddUserToGroupRequest,
        result=AddUserToGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.RemoveUserFromGroup',
        request=RemoveUserFromGroupRequest,
        result=RemoveUserFromGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.ListGroups',
        request=ListGroupsRequest,
        result=ListGroupsResult,
        is_listing=True,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.ListUsersInGroup',
        request=ListUsersInGroupRequest,
        result=ListUsersInGroupResult,
        is_listing=True,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.AddSudoUser',
        request=AddSudoUserRequest,
        result=AddSudoUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.RemoveSudoUser',
        request=RemoveSudoUserRequest,
        result=RemoveSudoUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.GlobalSignOut',
        request=GlobalSignOutRequest,
        result=GlobalSignOutResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Accounts.ResetPassword',
        request=ResetPasswordRequest,
        result=ResetPasswordResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.InitiateAuth',
        request=InitiateAuthRequest,
        result=InitiateAuthResult,
        is_listing=False,
        is_public=True
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.RespondToAuthChallenge',
        request=RespondToAuthChallengeRequest,
        result=RespondToAuthChallengeResult,
        is_listing=False,
        is_public=True
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.ForgotPassword',
        request=ForgotPasswordRequest,
        result=ForgotPasswordResult,
        is_listing=False,
        is_public=True
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.ConfirmForgotPassword',
        request=ConfirmForgotPasswordRequest,
        result=ConfirmForgotPasswordResult,
        is_listing=False,
        is_public=True
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.ChangePassword',
        request=ChangePasswordRequest,
        result=ChangePasswordResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.GetUser',
        request=GetUserRequest,
        result=GetUserResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.GetGroup',
        request=GetGroupRequest,
        result=GetGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.AddUserToGroup',
        request=AddUserToGroupRequest,
        result=AddUserToGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.RemoveUserFromGroup',
        request=RemoveUserFromGroupRequest,
        result=RemoveUserFromGroupResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.GetUserPrivateKey',
        request=GetUserPrivateKeyRequest,
        result=GetUserPrivateKeyResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.ListUsersInGroup',
        request=ListUsersInGroupRequest,
        result=ListUsersInGroupResult,
        is_listing=True,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.SignOut',
        request=SignOutRequest,
        result=SignOutResult,
        is_listing=False,
        is_public=False
    ),
    IdeaOpenAPISpecEntry(
        namespace='Auth.GlobalSignOut',
        request=GlobalSignOutRequest,
        result=GlobalSignOutResult,
        is_listing=False,
        is_public=False
    )
]
