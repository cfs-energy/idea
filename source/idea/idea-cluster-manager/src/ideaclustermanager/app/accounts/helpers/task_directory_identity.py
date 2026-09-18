"""Validate the SQS sender before granting a bastion task a directory identity."""


def verify_bastion_task(context, sender_id, task_arn):
    config = context.config()
    role_id = config.get_string('bastion-host.task_role_id', required=True)
    cluster = config.get_string('ecs.cluster_name', required=True)
    service = config.get_string('bastion-host.service_name', required=True)
    tokens = sender_id.split(':')
    if len(tokens) != 2 or tokens[0] != role_id:
        raise ValueError('Bastion task sender role does not match')
    if task_arn.rsplit('/', 1)[-1] != tokens[1]:
        raise ValueError('Bastion task sender session does not match')
    result = (
        context.aws()
        .get_client('ecs')
        .describe_tasks(cluster=cluster, tasks=[task_arn])
    )
    tasks = result.get('tasks', [])
    if result.get('failures') or len(tasks) != 1:
        raise ValueError('Bastion task was not found in the cluster')
    task = tasks[0]
    # A task whose container carries a health check stays ACTIVATING until the check passes, and
    # the bastion's check is sshd, which starts only after this join. A started task the service
    # still wants running is a member; one being stopped, or not yet started, is not.
    started = task.get('lastStatus') in ('ACTIVATING', 'RUNNING')
    wanted = task.get('desiredStatus', 'RUNNING') == 'RUNNING'
    if (
        task.get('taskArn') != task_arn
        or not started
        or not wanted
        or task.get('group') != f'service:{service}'
    ):
        raise ValueError('Bastion task is not a running member of the service')
    return tokens[1]
