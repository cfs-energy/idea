/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

import {
    CreateSessionRequest,
    CreateSessionResponse,
    GetSessionInfoRequest,
    GetSessionInfoResponse,
    UpdateSessionRequest,
    UpdateSessionResponse,
    SetSessionCleanupExemptionRequest,
    SetSessionCleanupExemptionResponse,
    RefreshBaseSoftwareStackAmisRequest,
    RefreshBaseSoftwareStackAmisResponse,
    DeleteSessionRequest,
    DeleteSessionResponse,
    ListSessionsRequest,
    ListSessionsResponse,
    GetSessionScreenshotRequest,
    GetSessionScreenshotResponse,
    StopSessionRequest,
    StopSessionResponse,
    ResumeSessionsRequest,
    ResumeSessionsResponse,
    ListSoftwareStackRequest,
    ListSoftwareStackResponse,
    CreateSoftwareStackRequest,
    CreateSoftwareStackResponse,
    UpdateSoftwareStackRequest,
    UpdateSoftwareStackResponse,
    DeleteSoftwareStackRequest,
    DeleteSoftwareStackResponse,
    GetModuleInfoRequest,
    GetModuleInfoResult,
    RebootSessionRequest,
    RebootSessionResponse,
    CreateSoftwareStackFromSessionRequest,
    CreateSoftwareStackFromSessionResponse,
    GetSessionConnectionInfoRequest,
    GetSessionConnectionInfoResponse,
    CreatePermissionProfileResponse,
    CreatePermissionProfileRequest,
    ListPermissionsRequest,
    ListPermissionsResponse,
    GetSoftwareStackInfoResponse,
    GetSoftwareStackInfoRequest,
    UpdatePermissionProfileResponse,
    UpdatePermissionProfileRequest,
    ListDesktopImagesRequest,
    ListDesktopImagesResponse,
    BuildDesktopImageRequest,
    BuildDesktopImageResponse,
    ListImageRowsRequest,
    ListImageRowsResponse,
    RefreshImagesRequest,
    RefreshImagesResponse,
    RollbackImageRequest,
    RollbackImageResponse,
    ExtendSoftwareStackRequest,
    ExtendSoftwareStackResponse,
    ListOrphanInstancesRequest,
    ListOrphanInstancesResponse,
    SetImagePinnedRequest,
    SetImagePinnedResponse,
    GetImageScheduleRequest,
    GetImageScheduleResponse,
    UpdateImageScheduleRequest,
    UpdateImageScheduleResponse
} from './data-model'
import IdeaBaseClient, {IdeaBaseClientProps} from "./base-client";

export interface VirtualDesktopAdminClientProps extends IdeaBaseClientProps {
}

class VirtualDesktopAdminClient extends IdeaBaseClient<VirtualDesktopAdminClientProps> {

    getModuleInfo(): Promise<GetModuleInfoRequest> {
        return this.apiInvoker.invoke_alt<GetModuleInfoRequest, GetModuleInfoResult>(
            'App.GetModuleInfo',
            {}
        )
    }

    createSession(req: CreateSessionRequest): Promise<CreateSessionResponse> {
        return this.apiInvoker.invoke_alt<CreateSessionRequest, CreateSessionResponse>(
            'VirtualDesktopAdmin.CreateSession',
            req
        )
    }

    updateSession(req: UpdateSessionRequest): Promise<UpdateSessionResponse> {
        return this.apiInvoker.invoke_alt<UpdateSessionRequest, UpdateSessionResponse>(
            'VirtualDesktopAdmin.UpdateSession',
            req
        )
    }

    setSessionCleanupExemption(req: SetSessionCleanupExemptionRequest): Promise<SetSessionCleanupExemptionResponse> {
        return this.apiInvoker.invoke_alt<SetSessionCleanupExemptionRequest, SetSessionCleanupExemptionResponse>(
            'VirtualDesktopAdmin.SetSessionCleanupExemption',
            req
        )
    }

    deleteSessions(req: DeleteSessionRequest): Promise<DeleteSessionResponse> {
        return this.apiInvoker.invoke_alt<DeleteSessionRequest, DeleteSessionResponse>(
            'VirtualDesktopAdmin.DeleteSessions',
            req
        )
    }

    getSessionInfo(req: GetSessionInfoRequest): Promise<GetSessionInfoResponse> {
        return this.apiInvoker.invoke_alt<GetSessionInfoRequest, GetSessionInfoResponse>(
            'VirtualDesktopAdmin.GetSessionInfo',
            req
        )
    }

    listSessions(req: ListSessionsRequest): Promise<ListSessionsResponse> {
        return this.apiInvoker.invoke_alt<ListSessionsRequest, ListSessionsResponse>(
            'VirtualDesktopAdmin.ListSessions',
            req
        )
    }

    stopSessions(req: StopSessionRequest): Promise<StopSessionResponse> {
        return this.apiInvoker.invoke_alt<StopSessionRequest, StopSessionResponse>(
            'VirtualDesktopAdmin.StopSessions',
            req
        )
    }

    rebootSessions(req: RebootSessionRequest): Promise<RebootSessionResponse> {
        return this.apiInvoker.invoke_alt<RebootSessionRequest, RebootSessionResponse>(
            'VirtualDesktopAdmin.RebootSessions',
            req
        )
    }

    resumeSessions(req: ResumeSessionsRequest): Promise<ResumeSessionsResponse> {
        return this.apiInvoker.invoke_alt<ResumeSessionsRequest, ResumeSessionsResponse>(
            'VirtualDesktopAdmin.ResumeSessions',
            req
        )
    }

    getSessionScreenshot(req: GetSessionScreenshotRequest): Promise<GetSessionScreenshotResponse> {
        return this.apiInvoker.invoke_alt<GetSessionScreenshotRequest, GetSessionScreenshotResponse>(
            'VirtualDesktopAdmin.GetSessionScreenshot',
            req
        )
    }

    getSessionConnectionInfo(req: GetSessionConnectionInfoRequest): Promise<GetSessionConnectionInfoResponse> {
        return this.apiInvoker.invoke_alt<GetSessionConnectionInfoRequest, GetSessionConnectionInfoResponse>(
            'VirtualDesktopAdmin.GetSessionConnectionInfo',
            req
        )
    }

    refreshBaseSoftwareStackAmis(req: RefreshBaseSoftwareStackAmisRequest): Promise<RefreshBaseSoftwareStackAmisResponse> {
        return this.apiInvoker.invoke_alt<RefreshBaseSoftwareStackAmisRequest, RefreshBaseSoftwareStackAmisResponse>(
            'VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis',
            req
        )
    }

    createSoftwareStack(req: CreateSoftwareStackRequest): Promise<CreateSoftwareStackResponse> {
        return this.apiInvoker.invoke_alt<CreateSoftwareStackRequest, CreateSoftwareStackResponse>(
            'VirtualDesktopAdmin.CreateSoftwareStack',
            req
        )
    }

    updateSoftwareStack(req: UpdateSoftwareStackRequest): Promise<UpdateSoftwareStackResponse> {
        return this.apiInvoker.invoke_alt<UpdateSoftwareStackRequest, UpdateSoftwareStackResponse>(
            'VirtualDesktopAdmin.UpdateSoftwareStack',
            req
        )
    }

    deleteSoftwareStack(req: DeleteSoftwareStackRequest): Promise<DeleteSoftwareStackResponse> {
        return this.apiInvoker.invoke_alt<DeleteSoftwareStackRequest, DeleteSoftwareStackResponse>(
            'VirtualDesktopAdmin.DeleteSoftwareStack',
            req
        )
    }

    getSoftwareStackInfo(req: GetSoftwareStackInfoRequest): Promise<GetSoftwareStackInfoResponse> {
        return this.apiInvoker.invoke_alt<GetSoftwareStackInfoRequest, GetSoftwareStackInfoResponse>(
            'VirtualDesktopAdmin.GetSoftwareStackInfo',
            req
        )
    }

    extendSoftwareStack(req: ExtendSoftwareStackRequest): Promise<ExtendSoftwareStackResponse> {
        return this.apiInvoker.invoke_alt<ExtendSoftwareStackRequest, ExtendSoftwareStackResponse>(
            'VirtualDesktopAdmin.ExtendSoftwareStack',
            req
        )
    }

    listOrphanInstances(req: ListOrphanInstancesRequest = {}): Promise<ListOrphanInstancesResponse> {
        return this.apiInvoker.invoke_alt<ListOrphanInstancesRequest, ListOrphanInstancesResponse>(
            'VirtualDesktopAdmin.ListOrphanInstances',
            req
        )
    }

    listSoftwareStacks(req: ListSoftwareStackRequest): Promise<ListSoftwareStackResponse> {
        return this.apiInvoker.invoke_alt<ListSoftwareStackRequest, ListSoftwareStackResponse>(
            'VirtualDesktopAdmin.ListSoftwareStacks',
            req
        )
    }

    createSoftwareStackFromSession(req: CreateSoftwareStackFromSessionRequest): Promise<CreateSoftwareStackFromSessionResponse> {
        return this.apiInvoker.invoke_alt<CreateSoftwareStackFromSessionRequest, CreateSoftwareStackFromSessionResponse>(
            'VirtualDesktopAdmin.CreateSoftwareStackFromSession',
            req
        )
    }

    createPermissionProfile(req: CreatePermissionProfileRequest): Promise<CreatePermissionProfileResponse> {
        return this.apiInvoker.invoke_alt<CreatePermissionProfileRequest, CreatePermissionProfileResponse>(
            'VirtualDesktopAdmin.CreatePermissionProfile',
            req
        )
    }

    updatePermissionProfile(req: UpdatePermissionProfileRequest): Promise<UpdatePermissionProfileResponse> {
        return this.apiInvoker.invoke_alt<UpdatePermissionProfileRequest, UpdatePermissionProfileResponse>(
            'VirtualDesktopAdmin.UpdatePermissionProfile',
            req
        )
    }

    listSessionPermissions(req: ListPermissionsRequest): Promise<ListPermissionsResponse> {
        return this.apiInvoker.invoke_alt<ListPermissionsRequest, ListPermissionsResponse>(
            'VirtualDesktopAdmin.ListSessionPermissions',
            req
        )
    }

    listSharedPermissions(req: ListPermissionsRequest): Promise<ListPermissionsResponse> {
        return this.apiInvoker.invoke_alt<ListPermissionsRequest, ListPermissionsResponse>(
            'VirtualDesktopAdmin.ListSharedPermissions',
            req
        )
    }

    listDesktopImages(req: ListDesktopImagesRequest): Promise<ListDesktopImagesResponse> {
        return this.apiInvoker.invoke_alt<ListDesktopImagesRequest, ListDesktopImagesResponse>(
            'VirtualDesktopAdmin.ListDesktopImages',
            req
        )
    }

    buildDesktopImage(req: BuildDesktopImageRequest): Promise<BuildDesktopImageResponse> {
        return this.apiInvoker.invoke_alt<BuildDesktopImageRequest, BuildDesktopImageResponse>(
            'VirtualDesktopAdmin.BuildDesktopImage',
            req
        )
    }

    listImageRows(req: ListImageRowsRequest): Promise<ListImageRowsResponse> {
        return this.apiInvoker.invoke_alt<ListImageRowsRequest, ListImageRowsResponse>(
            'VirtualDesktopAdmin.ListImageRows',
            req
        )
    }

    refreshImages(req: RefreshImagesRequest): Promise<RefreshImagesResponse> {
        return this.apiInvoker.invoke_alt<RefreshImagesRequest, RefreshImagesResponse>(
            'VirtualDesktopAdmin.RefreshImages',
            req
        )
    }

    rollbackImage(req: RollbackImageRequest): Promise<RollbackImageResponse> {
        return this.apiInvoker.invoke_alt<RollbackImageRequest, RollbackImageResponse>(
            'VirtualDesktopAdmin.RollbackImage',
            req
        )
    }

    setImagePinned(req: SetImagePinnedRequest): Promise<SetImagePinnedResponse> {
        return this.apiInvoker.invoke_alt<SetImagePinnedRequest, SetImagePinnedResponse>(
            'VirtualDesktopAdmin.SetImagePinned',
            req
        )
    }

    getImageSchedule(req: GetImageScheduleRequest): Promise<GetImageScheduleResponse> {
        return this.apiInvoker.invoke_alt<GetImageScheduleRequest, GetImageScheduleResponse>(
            'VirtualDesktopAdmin.GetImageSchedule',
            req
        )
    }

    updateImageSchedule(req: UpdateImageScheduleRequest): Promise<UpdateImageScheduleResponse> {
        return this.apiInvoker.invoke_alt<UpdateImageScheduleRequest, UpdateImageScheduleResponse>(
            'VirtualDesktopAdmin.UpdateImageSchedule',
            req
        )
    }
}

export default VirtualDesktopAdminClient
