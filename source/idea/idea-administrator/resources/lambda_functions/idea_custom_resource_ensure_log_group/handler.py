from idea_lambda_commons import HttpClient, CfnResponse, CfnResponseStatus
import boto3
import logging

logging.getLogger().setLevel(logging.INFO)


def ensure_log_group(logs_client, log_group_name: str, retention_in_days) -> str:
    """
    Create the group if it is not already there, then apply retention.

    Returns 'created' or 'adopted' so the outcome is visible in the stack event.
    """
    outcome = 'created'
    try:
        logs_client.create_log_group(logGroupName=log_group_name)
    except logs_client.exceptions.ResourceAlreadyExistsException:
        outcome = 'adopted'
        logging.info(f'log group already present, adopting: {log_group_name}')

    if retention_in_days:
        logs_client.put_retention_policy(
            logGroupName=log_group_name, retentionInDays=int(retention_in_days)
        )
    return outcome


def handler(event, context):
    """
    Create-or-adopt the bedrock invocation log group.

    The group outlives the stack on purpose: idea does not own the account-level
    invocation logging configuration, so another caller may still be delivering to
    it. A plain CloudFormation log group with a fixed name cannot express that -
    once retained, the next deploy fails because the group already exists. This
    creates it when absent, adopts it when present, and never deletes it.
    """
    http_client = HttpClient()
    log_group_name = event['ResourceProperties']['LogGroupName']
    try:
        logging.info(f'ReceivedEvent: {event}')
        data = {}
        if event['RequestType'] == 'Delete':
            logging.info(f'leaving log group in place on delete: {log_group_name}')
        else:
            outcome = ensure_log_group(
                boto3.client('logs'),
                log_group_name,
                event['ResourceProperties'].get('RetentionInDays'),
            )
            data = {'LogGroupName': log_group_name, 'Outcome': outcome}
        http_client.send_cfn_response(
            CfnResponse(
                context=context,
                event=event,
                status=CfnResponseStatus.SUCCESS,
                data=data,
                physical_resource_id=log_group_name,
            )
        )
    except Exception as e:
        logging.exception(f'Failed to ensure log group {log_group_name}: {e}')
        http_client.send_cfn_response(
            CfnResponse(
                context=context,
                event=event,
                status=CfnResponseStatus.FAILED,
                data={'error': str(e)},
                physical_resource_id=log_group_name,
            )
        )
