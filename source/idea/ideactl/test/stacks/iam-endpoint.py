"""An endpoint that answers only the calls a rendered IAM policy document allows.

DynamoDB requests are proxied to a local DynamoDB when the policy allows the action on the
requested table and refused with the service's own AccessDeniedException shape when it does not.
Signatures are not checked: the decision comes from the policy document, not from the caller.

A request whose action and resource match a statement carrying a Condition is answered 500 rather
than allowed, because this evaluator does not evaluate conditions and must not guess.

Environment:
  IAM_POLICY_FILE   rendered policy document, JSON
  DDB_ENDPOINT      where allowed DynamoDB requests are proxied, for example http://ddb:8000
  AWS_ACCOUNT_ID    account segment of the ARNs this builds from request contents
  AWS_REGION_NAME   region segment of the same ARNs
  PORT              listen port, default 8099
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POLICY = json.load(open(os.environ['IAM_POLICY_FILE']))
DDB_ENDPOINT = os.environ.get('DDB_ENDPOINT', '').rstrip('/')
ACCOUNT = os.environ.get('AWS_ACCOUNT_ID', '')
REGION = os.environ.get('AWS_REGION_NAME', '')
PORT = int(os.environ.get('PORT', '8099'))

ROUTE53_RRSET = re.compile(r'^/2013-04-01/hostedzone/([^/]+)/rrset/?$')

CHANGE_RESPONSE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ChangeResourceRecordSetsResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
    '<ChangeInfo><Id>/change/C0000000000000</Id><Status>INSYNC</Status>'
    '<SubmittedAt>2026-01-01T00:00:00.000Z</SubmittedAt></ChangeInfo>'
    '</ChangeResourceRecordSetsResponse>'
)


class Undecidable(Exception):
    """Raised when a matching statement carries a condition this evaluator cannot evaluate."""


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def glob_matches(pattern, value):
    expression = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return re.fullmatch(expression, value, re.IGNORECASE) is not None


def allowed(action, resource):
    """Evaluates one action against the document: an explicit deny wins, otherwise any allow."""
    permitted = False
    for statement in POLICY['Statement']:
        if not any(glob_matches(a, action) for a in as_list(statement.get('Action'))):
            continue
        if not any(
            glob_matches(r, resource) for r in as_list(statement.get('Resource'))
        ):
            continue
        if 'Condition' in statement:
            raise Undecidable(f'{action} on {resource} matches a conditional statement')
        if statement.get('Effect') == 'Deny':
            return False
        permitted = True
    return permitted


def log(message):
    print(message, file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    # One request per connection: a caller must never mistake a dropped keep-alive for a refusal.
    protocol_version = 'HTTP/1.0'

    def log_message(self, *_args):
        return

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        target = self.headers.get('X-Amz-Target', '')
        try:
            if target.startswith('DynamoDB_'):
                self.dynamodb(target.split('.', 1)[1], body)
            elif target.startswith('AmazonSSM.'):
                self.ssm(target.split('.', 1)[1])
            elif ROUTE53_RRSET.match(self.path):
                self.route53(ROUTE53_RRSET.match(self.path).group(1))
            else:
                self.send_json(
                    500, {'message': f'unhandled request {target or self.path}'}
                )
                log(f'UNHANDLED {target or self.path}')
        except Undecidable as error:
            self.send_json(500, {'message': str(error)})
            log(f'UNDECIDABLE {error}')
        except Exception as error:  # never close the socket without an answer
            self.send_json(502, {'message': f'{type(error).__name__}: {error}'})
            log(f'ERROR {target or self.path}: {type(error).__name__}: {error}')

    def dynamodb(self, operation, body):
        action = f'dynamodb:{operation}'
        table = (json.loads(body or b'{}') or {}).get('TableName')
        if table is None:
            self.send_json(500, {'message': f'{action} without a TableName'})
            log(f'UNHANDLED {action} without a TableName')
            return
        resource = f'arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{table}'
        if not allowed(action, resource):
            log(f'DENY  {action} {resource}')
            self.send_json(
                400,
                {
                    '__type': 'com.amazon.coral.service#AccessDeniedException',
                    'message': f'User is not authorized to perform: {action} on resource: {resource}',
                },
            )
            return
        log(f'ALLOW {action} {resource}')
        self.proxy(body)

    def route53(self, zone):
        action = 'route53:ChangeResourceRecordSets'
        resource = f'arn:aws:route53:::hostedzone/{zone}'
        if not allowed(action, resource):
            log(f'DENY  {action} {resource}')
            self.send_xml(
                403,
                '<ErrorResponse><Error><Code>AccessDenied</Code></Error></ErrorResponse>',
            )
            return
        log(f'ALLOW {action} {resource}')
        self.send_xml(200, CHANGE_RESPONSE)

    def ssm(self, operation):
        # The role script swallows a failed send-command, so this answers without a decision and
        # keeps the evaluator's record free of a resource it cannot derive from a tag-targeted call.
        log(f'ANSWER ssm:{operation} without evaluating')
        self.send_json(
            200, {'Command': {'CommandId': '00000000-0000-0000-0000-000000000000'}}
        )

    def proxy(self, body):
        request = urllib.request.Request(DDB_ENDPOINT, data=body, method='POST')
        for name in ('Content-Type', 'X-Amz-Target', 'Authorization', 'X-Amz-Date'):
            if self.headers.get(name) is not None:
                request.add_header(name, self.headers[name])
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    self.respond(
                        response.status, response.read(), 'application/x-amz-json-1.0'
                    )
                return
            except urllib.error.HTTPError as error:
                self.respond(error.code, error.read(), 'application/x-amz-json-1.0')
                return
            except OSError as error:
                log(
                    f'RETRY proxy attempt {attempt + 1}: {type(error).__name__}: {error}'
                )
        raise Undecidable('the local DynamoDB did not answer three attempts')

    def send_json(self, status, payload):
        self.respond(status, json.dumps(payload).encode(), 'application/x-amz-json-1.0')

    def send_xml(self, status, text):
        self.respond(status, text.encode(), 'text/xml')

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    # `--decide <action> <resource>` prints one decision, so the evaluator itself can be shown to
    # allow and to refuse rather than being taken on trust.
    if len(sys.argv) == 4 and sys.argv[1] == '--decide':
        print('allow' if allowed(sys.argv[2], sys.argv[3]) else 'deny')
        raise SystemExit(0)
    log(f'policy has {len(POLICY["Statement"])} statements; listening on {PORT}')
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
