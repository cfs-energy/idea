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

from abc import abstractmethod
from typing import Dict, Optional, Tuple


class TaskCannotSucceed(Exception):
    """retrying cannot help, and a retry holds up every later task of its FIFO group."""


class BaseTask:
    # FIFO lane: tasks for one entity share a message group, and so run in order,
    # unless their lane differs. A lane holds work that does not depend on, and is
    # not depended on by, the entity's other tasks, so its retries hold up only itself.
    lane: Optional[str] = None

    @abstractmethod
    def get_name(self) -> str: ...

    def entity_ref(self, payload: Dict) -> Optional[Tuple[str, str]]:
        return None

    @abstractmethod
    def invoke(self, payload: Dict): ...

    def on_terminal_failure(self, payload: Dict):
        """release what the payload holds once the task will not run again."""
