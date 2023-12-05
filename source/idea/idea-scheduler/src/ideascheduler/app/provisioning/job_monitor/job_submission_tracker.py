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

import ideascheduler

from ideascheduler.app.app_protocols import JobSubmissionTrackerProtocol
from ideadatamodel import SubmitJobResult
from ideasdk.utils import Utils

from cacheout import Cache
from typing import Optional, Union


class JobSubmissionTracker(JobSubmissionTrackerProtocol):
    """
    Job Submission Tracker is primarily used to track jobs that have been submitted but not queued.
    these operations are performed via different threads.
    the hook thread that received the initial hook from scheduler after submission calls put.
    the thread that submits the job to scheduler calls get to get the SubmitJobResult
    """

    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        # todo, move to config
        self._cache = Cache(maxsize=100)
        self._ttl_seconds = 10

    def ok(self, result: SubmitJobResult):
        job_uid = result.job_uid
        if Utils.is_empty(job_uid):
            return
        self._cache.set(job_uid, result, ttl=self._ttl_seconds)

    def fail(self, job_uid: str, exc: BaseException):
        if Utils.is_empty(job_uid):
            return
        self._cache.set(job_uid, exc, ttl=self._ttl_seconds)

    def get(self, job_uid: str) -> Optional[Union[SubmitJobResult, BaseException]]:
        return self._cache.get(job_uid)
