"""The ECS task this process runs in, from the container metadata endpoint."""

import json
import os
import urllib.request
from typing import Optional

_task_arn: Optional[str] = None


def current_task_arn() -> Optional[str]:
    """
    the ARN of the task this process runs in, or None off ECS. A sender puts it in a
    message so the receiver can verify the sender through ecs:DescribeTasks, the way it
    verifies an EC2 sender through its instance id. A failed lookup is not cached.
    """
    global _task_arn
    if _task_arn:
        return _task_arn
    uri = os.environ.get('ECS_CONTAINER_METADATA_URI_V4', '').strip()
    if not uri:
        return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'{uri}/task', timeout=5) as response:
            _task_arn = json.load(response).get('TaskARN') or None
    except Exception:
        return None
    return _task_arn
