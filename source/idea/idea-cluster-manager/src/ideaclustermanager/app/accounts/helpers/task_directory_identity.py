# the modules whose ECS tasks send AD automation messages; each stack publishes the task
# role id and service name the verification reads
TASK_SENDER_MODULES = ('bastion-host', 'virtual-desktop-controller', 'scheduler')


def verify_cluster_task(context, sender_id, task_arn) -> str:
    """
    A message from an ECS task carries the task's ARN. The sender is a cluster component
    when the SQS SenderId names one of the cluster's task roles with that task's id as its
    session, and ECS shows the task running in the cluster as a member of the module's
    service. Mirrors the instance check an EC2 sender goes through.
    :returns: the module name the task belongs to
    :raises ValueError: when the sender cannot be verified
    """
    config = context.config()
    cluster = config.get_string('ecs.cluster_name', required=True)
    tokens = sender_id.split(':')
    if len(tokens) != 2:
        raise ValueError('Task sender is not a role session')
    module = next(
        (
            name
            for name in TASK_SENDER_MODULES
            if tokens[0] == config.get_string(f'{name}.task_role_id')
        ),
        None,
    )
    if module is None:
        raise ValueError('Task sender role is not a cluster task role')
    service = config.get_string(f'{module}.service_name', required=True)
    if task_arn.rsplit('/', 1)[-1] != tokens[1]:
        raise ValueError('Task sender session does not match the task')
    result = (
        context.aws()
        .get_client('ecs')
        .describe_tasks(cluster=cluster, tasks=[task_arn])
    )
    tasks = result.get('tasks', [])
    if result.get('failures') or len(tasks) != 1:
        raise ValueError('Task was not found in the cluster')
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
        raise ValueError(f'Task is not a running member of the {module} service')
    return module


def verify_bastion_task(context, sender_id, task_arn):
    """the bastion's join: kept for callers that still name it"""
    if verify_cluster_task(context, sender_id, task_arn) != 'bastion-host':
        raise ValueError('Bastion task sender role does not match')
    return sender_id.split(':')[1]
