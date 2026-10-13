"""The ECS task this process runs in, from the container metadata endpoint."""

import json
import os
import time
import urllib.request
from typing import Optional

_task_arn: Optional[str] = None


def current_task_arn() -> Optional[str]:
    """
    the ARN of the task this process runs in, or None off ECS. A sender puts it in a
    message so the receiver can verify the sender through ecs:DescribeTasks, the way it
    verifies an EC2 sender through its instance id. On ECS, a failed lookup raises
    after three attempts and is not cached. Delete senders can fall back to the
    receiver reconstructing the task ARN from the SQS role session.
    """
    global _task_arn
    if _task_arn:
        return _task_arn
    uri = os.environ.get('ECS_CONTAINER_METADATA_URI_V4', '').strip()
    if not uri:
        return None
    for attempt in range(3):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'{uri}/task', timeout=5) as response:
                _task_arn = json.load(response).get('TaskARN') or None
            if not _task_arn:
                raise RuntimeError('ECS container metadata did not include TaskARN')
            return _task_arn
        except Exception as error:
            if attempt == 2:
                raise RuntimeError(
                    f'Unable to read ECS task ARN from container metadata: {error}'
                ) from error
            time.sleep(0.25 * (attempt + 1))
