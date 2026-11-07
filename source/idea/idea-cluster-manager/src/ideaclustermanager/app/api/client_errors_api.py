import re
import threading
import time
from typing import Dict, List, Literal, Optional

import ideaclustermanager

from ideadatamodel import errorcodes, exceptions
from ideasdk.api import ApiInvocationContext, BaseAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_REPORTS_PER_HOUR = 60
MAX_TRACKED_USERS = 5000
MAX_MESSAGE_LENGTH = 160

# Only these words reach the logs, so a job name, a path or anything a person typed
# cannot, whatever a page puts in an exception. Unknown words become '?'.
MESSAGE_WORDS = frozenset(
    word.lower()
    for word in """
    Error TypeError RangeError ReferenceError SyntaxError URIError EvalError AggregateError
    ChunkLoadError DOMException NotFoundError AbortError NetworkError QuotaExceededError
    SecurityError InvalidStateError
    a an and are as at be been by call can cannot constant could did does element exceeded
    expected failed fetch for from function has have import imported in invalid is it iterable
    json load loading maximum minified module network no not null object of on or parse position
    properties property react reading read render rendered request script set setting size stack
    the to token unexpected undefined unknown variable was were while with assignment assign
    dynamically aborted timeout timed out chunk connection refused lost storage value values
    constructor child node remove insert before state update unmounted hook hooks
    """.split()
)
# error codes are reported by name; an uppercase word that isn't one could be anybody's job name
ERROR_CODES = frozenset(
    value
    for name, value in vars(errorcodes).items()
    if name.isupper() and isinstance(value, str)
) | {'NETWORK_ERROR', 'REQUEST_TIMEOUT', 'SERVER_ERROR'}
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
TOKEN_EDGE = '()[]{},;:.!'


def sanitize_message(message: Optional[str]) -> str:
    if not message:
        return ''
    kept: List[str] = []
    for raw in QUOTED.sub('?', message[:1000]).split():
        word = raw.strip(TOKEN_EDGE)
        if word.lower() in MESSAGE_WORDS or word in ERROR_CODES:
            kept.append(word)
        elif not kept or kept[-1] != '?':
            kept.append('?')
    return ' '.join(kept)[:MAX_MESSAGE_LENGTH]


class ClientErrorReport(BaseModel):
    model_config = ConfigDict(extra='forbid')

    error_type: Literal[
        'uncaught_error',
        'unhandled_rejection',
        'render_crash',
        'api_failure',
        'list_load_failed',
    ]
    # a route pattern, never a live path: fixed segments, :params and a trailing *
    route: str = Field(
        pattern=r'^/(?:(?:[a-z][a-z-]{0,39}|:[a-z_]{1,40}|\*)(?:/|$)){0,8}$',
        max_length=200,
    )
    app_version: str = Field(pattern=r'^[0-9A-Za-z.+-]{1,40}$')
    api_namespace: Optional[str] = Field(
        default=None, pattern=r'^[A-Z][A-Za-z]{0,40}\.[A-Z][A-Za-z0-9]{0,60}$'
    )
    message: Optional[str] = Field(default=None, max_length=1000)


class ClientErrorsAPI(BaseAPI):
    """
    browser errors reach the logs through this API, on the normal log path, without a third-party
    script or token. the session only limits the rate: who reported an error is not logged. the
    request is never logged as sent; only the fields below are, each one validated or reduced to
    known words.
    """

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.logger = context.logger('client-errors')
        self._lock = threading.Lock()
        self._recent: Dict[str, List[float]] = {}

    def _allow(self, username: str, now: float) -> bool:
        with self._lock:
            if username not in self._recent and len(self._recent) >= MAX_TRACKED_USERS:
                self._recent.clear()
            recent = [at for at in self._recent.get(username, []) if now - at < 3600]
            allowed = len(recent) < MAX_REPORTS_PER_HOUR
            if allowed:
                recent.append(now)
            self._recent[username] = recent
            return allowed

    def report(self, context: ApiInvocationContext):
        try:
            report = ClientErrorReport.model_validate(context.request_payload)
        except ValidationError:
            raise exceptions.invalid_params('Invalid error report') from None
        username = context.get_username()
        accepted = self._allow(username, time.monotonic())
        if accepted:
            self.logger.warning(
                f'[client-error] type={report.error_type} route={report.route} '
                f'version={report.app_version} api={report.api_namespace or "-"} '
                f'message="{sanitize_message(report.message)}"'
            )
        context.success({'accepted': accepted})

    def invoke(self, context: ApiInvocationContext):
        if (
            context.namespace != 'Portal.ReportClientError'
            or context.is_unix_domain_socket_invocation()
            or not context.is_authenticated_user()
            or not context.has_access_token()
            or not context.get_username()
        ):
            raise exceptions.unauthorized_access()
        self.report(context)
