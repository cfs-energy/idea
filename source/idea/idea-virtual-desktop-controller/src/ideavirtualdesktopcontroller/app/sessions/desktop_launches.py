"""
When each of this cluster's desktops was launched, from CloudTrail.

Desktops deleted before 26.10.6 left a history row without an instance id, so their cost
can only be guessed from creation to stop. CloudTrail keeps 90 days of RunInstances
events, which carry the instance id and the desktop's owner tag; the controller reads
them once to put the instance id back on those rows.
"""

from ideadatamodel import constants
from ideasdk.utils import Utils

from botocore.exceptions import ClientError

import arrow
import json
import time
from typing import Dict, List

# how far back CloudTrail event history goes
CLOUDTRAIL_DAYS = 90
# LookupEvents allows 2 requests a second per account and region
PAGE_INTERVAL_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 32
THROTTLED = {'ThrottlingException', 'Throttling', 'TooManyRequestsException'}


class DesktopLaunches:
    """
    pages RunInstances events in time-bounded slices, so the leader loop is never held
    for long; the page token survives between slices and a later call carries on.
    """

    def __init__(self, context, sleep=time.sleep, clock=time.monotonic):
        self.context = context
        self._sleep = sleep
        self._clock = clock
        self._request = None
        # the oldest event read so far, epoch ms; a pass whose page token is refused
        # carries on from here instead of from the start
        self._oldest = None
        self._recovered_at = None
        self.launches: List[Dict] = []
        self._seen = set()
        self.complete = False

    def read(self, budget_seconds: float = 30) -> bool:
        """true once every event in the 90 days has been read."""
        if self.complete:
            return True
        if self._request is None:
            # fixed once: a page token is only good for the request that issued it
            now = arrow.utcnow()
            self._request = {
                'LookupAttributes': [
                    {'AttributeKey': 'EventName', 'AttributeValue': 'RunInstances'}
                ],
                'StartTime': now.shift(days=-CLOUDTRAIL_DAYS).datetime,
                'EndTime': now.datetime,
                'MaxResults': 50,
            }
        cloudtrail = self.context.aws().cloudtrail()
        deadline = self._clock() + budget_seconds
        backoff = 1
        # a hard budget: no call is started, and no wait runs, past the deadline
        while self._clock() < deadline:
            try:
                page = cloudtrail.lookup_events(**self._request)
            except ClientError as e:
                code = e.response.get('Error', {}).get('Code')
                if code == 'InvalidNextTokenException' and self._resume():
                    continue
                if code not in THROTTLED:
                    raise
                self._sleep(max(0, min(backoff, deadline - self._clock())))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            backoff = 1
            for event in page.get('Events', []):
                if event.get('EventTime') is not None:
                    at = arrow.get(event['EventTime']).int_timestamp * 1000
                    self._oldest = at if self._oldest is None else min(self._oldest, at)
                for launch in self._desktop_launches(event):
                    if launch['instance_id'] not in self._seen:
                        self._seen.add(launch['instance_id'])
                        self.launches.append(launch)
            token = page.get('NextToken')
            if not token:
                self.complete = True
                return True
            self._request['NextToken'] = token
            self._sleep(max(0, min(PAGE_INTERVAL_SECONDS, deadline - self._clock())))
        return False

    def _resume(self) -> bool:
        """
        CloudTrail refused the page token: continue the same pass without it, from the
        oldest event already read (events come newest first; the boundary second is
        read twice and deduplicated). once per point reached, so a token refused again
        with no progress since fails the slice rather than looping.
        """
        if self._oldest is None or self._oldest == self._recovered_at:
            return False
        self._recovered_at = self._oldest
        self._request.pop('NextToken', None)
        self._request['EndTime'] = arrow.get((self._oldest + 1000) / 1000).datetime
        self.context.logger('desktop-launches').warning(
            'CloudTrail refused the page token; resuming the RunInstances read from '
            f'{self._request["EndTime"].isoformat()}'
        )
        return True

    def _desktop_launches(self, event: Dict) -> List[Dict]:
        try:
            detail = json.loads(event.get('CloudTrailEvent') or '{}')
        except ValueError:
            return []
        if detail.get('errorCode'):
            return []
        tags = {}
        request = _as_dict(detail.get('requestParameters'))
        for spec in _items(request.get('tagSpecificationSet')):
            if spec.get('resourceType') == 'instance':
                for tag in _as_list(spec.get('tags')):
                    if isinstance(tag, dict):
                        tags[tag.get('key')] = tag.get('value')
        response = _as_dict(detail.get('responseElements'))
        launched = arrow.get(detail.get('eventTime') or event['EventTime'])
        result = []
        for item in _items(response.get('instancesSet')):
            item_tags = dict(tags)
            for tag in _items(item.get('tagSet')):
                item_tags[tag.get('key')] = tag.get('value')
            if (
                item_tags.get(constants.IDEA_TAG_NODE_TYPE)
                != constants.NODE_TYPE_DCV_HOST
                or item_tags.get(constants.IDEA_TAG_CLUSTER_NAME)
                != self.context.cluster_name()
                or Utils.is_empty(item.get('instanceId'))
            ):
                continue
            result.append(
                {
                    'owner': item_tags.get(constants.IDEA_TAG_JOB_OWNER),
                    'instance_id': item['instanceId'],
                    'instance_type': item.get('instanceType'),
                    'launched_on': launched.int_timestamp * 1000,
                }
            )
        return result


# CloudTrail replaces some fields with the string HIDDEN_DUE_TO_SECURITY_REASONS (tag
# specifications and tag sets, for example), so every nested field may be a str
def _as_dict(value) -> Dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> List:
    return value if isinstance(value, list) else []


def _items(value) -> List[Dict]:
    return [
        item
        for item in _as_list(_as_dict(value).get('items'))
        if isinstance(item, dict)
    ]
