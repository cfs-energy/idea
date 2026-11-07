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

"""
While a folder is being removed its owner's jobs cannot start. A file per owner, in a folder only
root can reach on this task, holds the time it lasts until; the runjob hook reads it without
asking the scheduler. Checking job state by polling can only narrow the window, and this closes
it: any job already submitted is seen by the look at the scheduler made after the hold is taken,
and any job submitted later cannot start until the hold ends.

A hold ends when the removal does, or at its time if the removal stops renewing it. The removal
checks that time before every file, so it stops by itself rather than run on past a hold that
has lapsed. A replaced task takes the folder with it, along with the removal.
"""

from contextlib import contextmanager
from typing import Iterator, Optional
import os
import re
import tempfile
import time

HOLD_TTL_SECONDS = 300
MAX_HOLD_SECONDS = 1800
DEFAULT_HOLDS_DIR = '/run/idea-scratch-holds'
_SAFE_OWNER = re.compile(r'[a-z0-9_][a-z0-9_.-]{0,63}', re.ASCII)


class HoldLapsed(Exception):
    pass


class HoldUnavailable(Exception):
    """no hold, so no removal"""


class Hold:
    def __init__(self, owner: str):
        self.owner = owner
        self.started_at = time.time()
        self.expires_at = 0.0


class OwnerHolds:
    def __init__(self, directory: str = DEFAULT_HOLDS_DIR):
        self.directory = directory
        self.current: Optional[Hold] = None

    def _path(self, owner: str) -> str:
        name = owner.lower()
        if not _SAFE_OWNER.fullmatch(name):
            raise ValueError(f'refused owner name: {owner!r}')
        return os.path.join(self.directory, name)

    def write(self, hold: Hold):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        expires_at = time.time() + HOLD_TTL_SECONDS
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix='.')
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(str(expires_at))
            os.replace(temporary, self._path(hold.owner))
        except Exception:
            os.unlink(temporary)
            raise
        hold.expires_at = expires_at

    @contextmanager
    def hold(self, owner: str) -> Iterator[Hold]:
        current = Hold(owner)
        try:
            self.write(current)
        except (OSError, ValueError) as e:
            raise HoldUnavailable(str(e)) from e
        self.current = current
        try:
            yield current
        finally:
            self.current = None
            try:
                os.unlink(self._path(owner))
            except FileNotFoundError:
                pass

    def renew(self):
        if self.current is not None:
            self.assert_held()
            self.write(self.current)

    def assert_held(self):
        """before every file: past its time a job may have started"""
        if self.current is not None and time.time() >= self.current.expires_at:
            raise HoldLapsed(
                f'The hold for {self.current.owner} lapsed during removal.'
            )
