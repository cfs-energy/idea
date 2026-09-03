"""
Image build bookkeeping shared by the scheduler (compute images) and the virtual
desktop controller (desktop images).
"""

from ideadatamodel.base import SocaBaseModel

from typing import Optional, List
from datetime import datetime
from pydantic import Field

__all__ = (
    'ImageBuildRecord',
    'ImageInventoryRow',
)


class ImageBuildRecord(SocaBaseModel):
    """the last build for one base OS and architecture, as kept in the module's image-builds table"""

    base_os: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    # building | complete | failed
    status: Optional[str] = Field(default=None)
    ami_name: Optional[str] = Field(default=None)
    base_ami: Optional[str] = Field(default=None)
    image_id: Optional[str] = Field(default=None)
    instance_id: Optional[str] = Field(default=None)
    requested_by: Optional[str] = Field(default=None)
    # the module host that runs the build thread; a restart there orphans the build
    host: Optional[str] = Field(default=None)
    # whether the caller asked for the default / base stack to be repointed on success
    update_target: Optional[bool] = Field(default=None)
    error: Optional[str] = Field(default=None)
    started_on: Optional[datetime] = Field(default=None)
    finished_on: Optional[datetime] = Field(default=None)


class ImageInventoryRow(SocaBaseModel):
    """one line of the Custom AMIs page: what an OS runs on today and what the last build did"""

    base_os: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    # desktop rows: the ss-base-* software stack the image belongs to
    stack_id: Optional[str] = Field(default=None)
    image_id: Optional[str] = Field(default=None)
    image_name: Optional[str] = Field(default=None)
    # desktop rows: the stock image the next build starts from (the stack's base_ami_id)
    base_ami_id: Optional[str] = Field(default=None)
    # built | built_outdated (a newer stock base exists than the built image came from)
    # | stock | missing | none | building
    state: Optional[str] = Field(default=None)
    build_date: Optional[datetime] = Field(default=None)
    referenced_by: Optional[List[str]] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    last_build: Optional[ImageBuildRecord] = Field(default=None)
