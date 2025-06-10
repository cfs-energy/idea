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
    'ListFilesRequest',
    'ListFilesResult',
    'ReadFileRequest',
    'ReadFileResult',
    'TailFileRequest',
    'TailFileResult',
    'SaveFileRequest',
    'SaveFileResult',
    'DownloadFilesRequest',
    'DownloadFilesResult',
    'CreateFileRequest',
    'CreateFileResult',
    'DeleteFilesRequest',
    'DeleteFilesResult',
    'OPEN_API_SPEC_ENTRIES_FILE_BROWSER',
)

from pydantic import Field

from ideadatamodel import SocaPayload, SocaListingPayload, IdeaOpenAPISpecEntry
from ideadatamodel.filesystem.filesystem_model import FileData
from typing import Optional, List


# FileBrowser.ListFiles


class ListFilesRequest(SocaListingPayload):
    cwd: Optional[str] = Field(default=None)


class ListFilesResult(SocaListingPayload):
    cwd: Optional[str] = Field(default=None)
    listing: Optional[List[FileData]] = Field(default=None)


# FileBrowser.ReadFile


class ReadFileRequest(SocaPayload):
    file: Optional[str] = Field(default=None)


class ReadFileResult(SocaPayload):
    file: Optional[str] = Field(default=None)
    content_type: Optional[str] = Field(default=None)
    content: Optional[str] = Field(default=None)


# FileBrowser.TailFile


class TailFileRequest(SocaPayload):
    file: Optional[str] = Field(default=None)
    line_count: Optional[int] = Field(default=None)
    next_token: Optional[str] = Field(default=None)


class TailFileResult(SocaPayload):
    file: Optional[str] = Field(default=None)
    next_token: Optional[str] = Field(default=None)
    lines: Optional[List[str]] = Field(default=None)
    line_count: Optional[int] = Field(default=None)


# FileBrowser.SaveFile


class SaveFileRequest(SocaPayload):
    file: Optional[str] = Field(default=None)
    content: Optional[str] = Field(default=None)


class SaveFileResult(SocaPayload):
    pass


# FileBrowser.DownloadFiles


class DownloadFilesRequest(SocaPayload):
    files: Optional[List[str]] = Field(default=None)


class DownloadFilesResult(SocaPayload):
    download_url: Optional[str] = Field(default=None)


# FileBrowser.DeleteFiles
class DeleteFilesRequest(SocaPayload):
    files: Optional[List[str]] = Field(default=None)


class DeleteFilesResult(SocaPayload):
    pass


# FileBrowser.CreateFile
class CreateFileRequest(SocaPayload):
    cwd: Optional[str] = Field(default=None)
    filename: Optional[str] = Field(default=None)
    is_folder: Optional[bool] = Field(default=None)


class CreateFileResult(SocaPayload):
    pass


OPEN_API_SPEC_ENTRIES_FILE_BROWSER = [
    IdeaOpenAPISpecEntry(
        namespace='FileBrowser.ListFiles',
        request=ListFilesRequest,
        result=ListFilesResult,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='FileBrowser.ReadFile',
        request=ReadFileRequest,
        result=ReadFileResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='FileBrowser.TailFile',
        request=TailFileRequest,
        result=TailFileResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='FileBrowser.SaveFile',
        request=SaveFileRequest,
        result=SaveFileResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='FileBrowser.DownloadFiles',
        request=DownloadFilesRequest,
        result=DownloadFilesResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='FileBrowser.CreateFile',
        request=CreateFileRequest,
        result=CreateFileResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='FileBrowser.DeleteFiles',
        request=DeleteFilesRequest,
        result=DeleteFilesResult,
        is_listing=False,
        is_public=False,
    ),
]
