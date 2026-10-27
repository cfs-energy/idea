from ideadatamodel import exceptions
from ideasdk.api import BaseAPI

from botocore.exceptions import ClientError
from ideaclustermanager.app.terminal.store import (
    TerminalDenied,
    TerminalMisconfigured,
    TerminalTargetStopped,
    TerminalUnavailable,
    aws_unavailable,
)


class TerminalAPI(BaseAPI):
    def __init__(self, context):
        self.context = context

    def invoke(self, context):
        if not (
            context.has_access_token()
            and context.is_authenticated_user()
            and context.is_authorized_user()
            and not context.is_unix_domain_socket_invocation()
        ):
            raise exceptions.unauthorized_access()
        service = self.context.terminal
        payload = context.request_payload or {}
        try:
            if context.namespace == 'Terminal.ListTargets' and not payload:
                context.success({'targets': service.targets(context.get_username())})
            elif (
                context.namespace == 'Terminal.CreateTicket'
                and set(payload) == {'target', 'client'}
                and isinstance(payload['target'], str)
            ):
                context.success(
                    service.create_ticket(
                        context.get_username(), payload['target'], payload['client']
                    )
                )
            else:
                raise TerminalDenied('Invalid request')
        except TerminalDenied as error:
            self.context.logger('web-terminal').info(
                'terminal request rejected user=%r target=%r reason=%s',
                context.get_username(),
                str(payload.get('target', ''))[:256],
                str(error),
            )
            if isinstance(error, TerminalTargetStopped):
                raise exceptions.SocaException(
                    error_code='TERMINAL_TARGET_STOPPED', message=str(error)
                ) from None
            if isinstance(error, TerminalUnavailable):
                raise exceptions.SocaException(
                    error_code='TERMINAL_UNAVAILABLE', message=str(error)
                ) from None
            if isinstance(error, TerminalMisconfigured):
                raise exceptions.SocaException(
                    error_code='TERMINAL_TARGET_MISCONFIGURED', message=str(error)
                ) from None
            raise exceptions.unauthorized_access() from None
        except ClientError as error:
            code = error.response['Error']['Code']
            self.context.logger('web-terminal').error(
                'terminal AWS request failed code=%s', code
            )
            raise exceptions.SocaException(
                error_code='TERMINAL_UNAVAILABLE'
                if aws_unavailable(error)
                else 'TERMINAL_TARGET_MISCONFIGURED',
                message='Terminal infrastructure is unavailable',
            ) from None
