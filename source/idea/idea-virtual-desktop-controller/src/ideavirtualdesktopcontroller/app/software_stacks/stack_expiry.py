"""
Stacks that nothing rebuilds grow old unseen: a hand-pinned image misses every kernel and fix, and
a desktop launched on one takes the slow path or fails on the new kernel. Each such stack gets an
owner and a date.

A stack on the monthly rebuild has no date, and neither does one whose image an admin exempted.
No row is rewritten at upgrade, and nobody sees a date until an administrator asks for one: a stack
with no date of its own counts from when it was created only once expiry.enforce_from is set, so
an upgrade does not turn every older stack red. A date set on the stack itself always counts.
Past its date the picker warns and points at the current stack. Only once enforce_from's day has
come does it stop being offered for new desktops; existing desktops never look at any of this.
"""

from ideadatamodel import (
    ListSessionsRequest,
    Notification,
    SocaPaginator,
    VirtualDesktopOrphanInstance,
    VirtualDesktopSoftwareStack,
    constants,
)
from ideasdk.metrics.health import HealthCollector, HealthMetrics
from ideasdk.utils import Utils

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set
import time

EXPIRY_PREFIX = 'virtual-desktop-controller.software_stacks.expiry'
DEFAULT_DAYS_KEY = f'{EXPIRY_PREFIX}.default_days'
WARN_DAYS_KEY = f'{EXPIRY_PREFIX}.warn_days'
EXEMPT_AMIS_KEY = f'{EXPIRY_PREFIX}.exempt_amis'
ENFORCE_FROM_KEY = f'{EXPIRY_PREFIX}.enforce_from'
DIGEST_ENABLED_KEY = f'{EXPIRY_PREFIX}.digest_enabled'
LAST_DIGEST_KEY = 'virtual-desktop-controller.software_stacks.expiry_last_digest_on'
DIGEST_TEMPLATE = 'virtual-desktop-controller.software-stack-expiry-digest'
DEFAULT_DAYS = 90
WARN_DAYS = 14
MAX_EXTEND_DAYS = 3650
DIGEST_EVERY = timedelta(days=7)
TICK_SECONDS = 6 * 3600
# a launch writes its session row just after the instance appears
ORPHAN_MIN_AGE = timedelta(hours=1)
EXPIRING, EXPIRED = 'expiring', 'expired'
LIVE_STATES = ['pending', 'running', 'stopping', 'stopped']


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _day(value: datetime) -> str:
    return value.strftime('%Y-%m-%d')


def on_monthly_rebuild(stack: VirtualDesktopSoftwareStack) -> bool:
    return bool(stack.base_ami_id) and not stack.image_pinned


class StackExpiry:
    def __init__(self, context):
        self.context = context

    def default_days(self) -> int:
        days = self.context.config().get_int(DEFAULT_DAYS_KEY, default=DEFAULT_DAYS)
        return days if days and days > 0 else DEFAULT_DAYS

    def warn_days(self) -> int:
        days = self.context.config().get_int(WARN_DAYS_KEY, default=WARN_DAYS)
        return days if days is not None and days >= 0 else WARN_DAYS

    def exempt_amis(self) -> Set[str]:
        amis = self.context.config().get_list(EXEMPT_AMIS_KEY, default=[]) or []
        return {str(ami).strip() for ami in amis}

    def _policy_date(self) -> Optional[datetime]:
        value = self.context.config().get_string(ENFORCE_FROM_KEY, default=None)
        if Utils.is_empty(value):
            return None
        try:
            return datetime.strptime(str(value).strip(), '%Y-%m-%d').replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None

    def enforce_from(self) -> Optional[datetime]:
        """empty hides nothing. so does a value that is not a date: hiding is opt-in."""
        policy = self._policy_date()
        if policy is None and not Utils.is_empty(
            self.context.config().get_string(ENFORCE_FROM_KEY, default=None)
        ):
            self.context.logger('stack-expiry').warning(
                f'{ENFORCE_FROM_KEY} is not a YYYY-MM-DD date, so nothing is hidden'
            )
        return policy

    def is_enforced(self, now: datetime) -> bool:
        enforce_from = self.enforce_from()
        return enforce_from is not None and now >= enforce_from

    def expires_on(
        self, stack: VirtualDesktopSoftwareStack, exempt: Optional[Set[str]] = None
    ) -> Optional[datetime]:
        exempt = self.exempt_amis() if exempt is None else exempt
        if on_monthly_rebuild(stack) or stack.ami_id in exempt:
            return None
        if stack.expires_on is None and self._policy_date() is None:
            return None
        return self._date_of(stack)

    def _date_of(self, stack: VirtualDesktopSoftwareStack) -> Optional[datetime]:
        if stack.expires_on is not None:
            return _as_utc(stack.expires_on)
        if stack.created_on is None:
            return None
        return _as_utc(stack.created_on) + timedelta(days=self.default_days())

    def state(
        self, stack: VirtualDesktopSoftwareStack, now: datetime, exempt=None
    ) -> Optional[str]:
        expires_on = self.expires_on(stack, exempt)
        if expires_on is None:
            return None
        if expires_on <= now:
            return EXPIRED
        if expires_on <= now + timedelta(days=self.warn_days()):
            return EXPIRING
        return None

    @staticmethod
    def current_stack(
        stack: VirtualDesktopSoftwareStack,
        offered: Iterable[VirtualDesktopSoftwareStack],
    ) -> Optional[VirtualDesktopSoftwareStack]:
        """the rebuilt stack for the same system, from those this person can already pick."""
        candidates = [
            other
            for other in offered
            if on_monthly_rebuild(other)
            and other.enabled
            and other.base_os == stack.base_os
            and other.architecture == stack.architecture
            and other.gpu == stack.gpu
        ]
        return candidates[0] if candidates else None

    def annotate(
        self,
        stacks: List[VirtualDesktopSoftwareStack],
        now: Optional[datetime] = None,
    ) -> List[VirtualDesktopSoftwareStack]:
        now = now or datetime.now(timezone.utc)
        exempt = self.exempt_amis()
        for stack in stacks:
            stack.effective_expires_on = self.expires_on(stack, exempt)
            stack.expiry_state = self.state(stack, now, exempt)
            stack.expiry_warning = None
            if stack.expiry_state is None:
                continue
            day = _day(stack.effective_expires_on)
            current = self.current_stack(stack, stacks)
            pointer = (
                f'Use {current.name} instead.'
                if current is not None
                else 'Ask an administrator which to use instead.'
            )
            stack.expiry_warning = (
                f'Past its expiry date, {day}. {pointer}'
                if stack.expiry_state == EXPIRED
                else f'Expires {day}. {pointer}'
            )
        return stacks

    def offered_for_new_desktops(
        self,
        stacks: List[VirtualDesktopSoftwareStack],
        now: Optional[datetime] = None,
    ) -> List[VirtualDesktopSoftwareStack]:
        now = now or datetime.now(timezone.utc)
        self.annotate(stacks, now)
        if not self.is_enforced(now):
            return stacks
        return [stack for stack in stacks if stack.expiry_state != EXPIRED]

    def refusal(
        self, stack: VirtualDesktopSoftwareStack, now: Optional[datetime] = None
    ) -> Optional[str]:
        """why a new desktop may not use this stack. settings that cannot be read refuse nothing."""
        try:
            now = now or datetime.now(timezone.utc)
            if not self.is_enforced(now) or self.state(stack, now) != EXPIRED:
                return None
            return (
                f'The software {stack.name} passed its expiry date on '
                f'{_day(self.expires_on(stack))} and is no longer offered for new desktops. '
                'Choose another, or ask an administrator to extend it.'
            )
        except Exception as e:
            self.context.logger('stack-expiry').warning(
                f'could not check the expiry of a stack: {type(e).__name__}'
            )
            return None

    def extended_date(
        self,
        stack: VirtualDesktopSoftwareStack,
        days: Optional[int],
        now: Optional[datetime] = None,
    ) -> datetime:
        """from today or from the current date, whichever is later: extending never shortens."""
        now = now or datetime.now(timezone.utc)
        current = None if on_monthly_rebuild(stack) else self._date_of(stack)
        start = max(now, current) if current is not None else now
        return start + timedelta(days=days or self.default_days())


class OrphanInstances:
    """instances tagged as desktops that no session row names. Reported, never touched."""

    def __init__(self, context, session_db):
        self.context = context
        self.session_db = session_db

    def known_instances(self) -> Set[str]:
        known: Set[str] = set()
        cursor = None
        while True:
            page = self.session_db.list_all_from_db(
                ListSessionsRequest(paginator=SocaPaginator(cursor=cursor))
            )
            for session in page.listing or []:
                if session.server is not None and session.server.instance_id:
                    known.add(session.server.instance_id)
            cursor = page.cursor
            if Utils.is_empty(cursor):
                return known

    def tagged_instances(self) -> List[Dict]:
        found: List[Dict] = []
        for page in (
            self.context.aws()
            .ec2()
            .get_paginator('describe_instances')
            .paginate(
                Filters=[
                    {
                        'Name': f'tag:{constants.IDEA_TAG_NODE_TYPE}',
                        'Values': [constants.NODE_TYPE_DCV_HOST],
                    },
                    {
                        'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
                        'Values': [self.context.cluster_name()],
                    },
                    {'Name': 'instance-state-name', 'Values': LIVE_STATES},
                ]
            )
        ):
            for reservation in page.get('Reservations', []):
                found.extend(reservation.get('Instances', []))
        return found

    def list(
        self, now: Optional[datetime] = None, min_age: timedelta = ORPHAN_MIN_AGE
    ) -> List[VirtualDesktopOrphanInstance]:
        now = now or datetime.now(timezone.utc)
        # the instances first: a desktop created in between has a row by the time rows are read
        tagged = self.tagged_instances()
        known = self.known_instances()
        orphans = []
        for instance in tagged:
            launched = instance.get('LaunchTime')
            if instance.get('InstanceId') in known:
                continue
            if launched is not None and now - _as_utc(launched) < min_age:
                continue
            tags = {t.get('Key'): t.get('Value') for t in instance.get('Tags', [])}
            orphans.append(
                VirtualDesktopOrphanInstance(
                    instance_id=instance.get('InstanceId'),
                    name=tags.get('Name'),
                    instance_type=instance.get('InstanceType'),
                    state=(instance.get('State') or {}).get('Name'),
                    launched_on=launched,
                )
            )
        return sorted(orphans, key=lambda o: o.instance_id or '')


class StackExpiryService(HealthCollector):
    """
    levels as a health collector, with its switch. the digest is the controller loop's tick, for
    a cluster with no metrics provider has owners too: one per owner per week at most.
    """

    name = 'stack_expiry'
    interval_seconds = TICK_SECONDS
    time_limit_seconds = 120

    def __init__(self, context, software_stack_db, session_db):
        self.context = context
        self.db = software_stack_db
        self.expiry = StackExpiry(context)
        self.orphans = OrphanInstances(context, session_db)
        self.metrics = HealthMetrics(context)
        self.logger = context.logger('stack-expiry')
        self._next_tick = 0.0

    def collect(self, metrics, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.publish_levels(self.all_stacks(), now)
        self.publish_orphans(now)

    def tick(self, now: Optional[datetime] = None) -> None:
        if time.monotonic() < self._next_tick:
            return
        self._next_tick = time.monotonic() + TICK_SECONDS
        self.send_digest(self.all_stacks(), now or datetime.now(timezone.utc))

    def all_stacks(self) -> List[VirtualDesktopSoftwareStack]:
        from ideadatamodel import ListSoftwareStackRequest

        stacks: List[VirtualDesktopSoftwareStack] = []
        cursor = None
        while True:
            page = self.db.list_all_from_db(
                ListSoftwareStackRequest(paginator=SocaPaginator(cursor=cursor))
            )
            stacks.extend(page.listing or [])
            cursor = page.cursor
            if Utils.is_empty(cursor):
                return [stack for stack in stacks if stack.enabled]

    def publish_levels(self, stacks, now: datetime) -> None:
        counts = {EXPIRING: 0, EXPIRED: 0}
        exempt = self.expiry.exempt_amis()
        for stack in stacks:
            state = self.expiry.state(stack, now, exempt)
            if state in counts:
                counts[state] += 1
        for state, count in counts.items():
            self.metrics.level('stacks.expiry', count, state=state)
        self.metrics.level(
            'stacks.expiry_enforced', 1 if self.expiry.is_enforced(now) else 0
        )

    def publish_orphans(self, now: datetime) -> None:
        try:
            self.metrics.level('desktop.orphan_instances', len(self.orphans.list(now)))
        except Exception as e:
            # no point rather than a wrong zero
            self.logger.warning(f'could not count orphan desktops: {type(e).__name__}')

    def send_digest(self, stacks, now: datetime) -> int:
        """
        one email per owner, listing all of theirs; stacks with no owner go to the cluster
        administrator as one email. claimed before sending, so a second leader sends nothing and a
        failed send waits a week rather than repeating every tick.
        """
        config = self.context.config()
        if not config.get_bool(DIGEST_ENABLED_KEY, default=True):
            return 0
        exempt = self.expiry.exempt_amis()
        due = [s for s in stacks if self.expiry.state(s, now, exempt) is not None]
        if not due:
            return 0
        last_ms = config.get_int(LAST_DIGEST_KEY, default=None)
        if (
            last_ms is not None
            and now - datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
            < DIGEST_EVERY
        ):
            return 0
        if not self._claim(last_ms, int(now.timestamp() * 1000)):
            return 0
        administrator = config.get_string(
            'cluster.administrator_username', default=None
        )
        by_owner: Dict[str, List[VirtualDesktopSoftwareStack]] = {}
        for stack in due:
            owner = (stack.owner or '').strip() or administrator
            if owner:
                by_owner.setdefault(owner, []).append(stack)
        sent = 0
        for owner, owned in sorted(by_owner.items()):
            try:
                self.context.notification_async_client.send_notification(
                    Notification(
                        username=owner,
                        template_name=DIGEST_TEMPLATE,
                        params={
                            'cluster_name': self.context.cluster_name(),
                            'username': owner,
                            'stacks': [
                                {
                                    'name': stack.name,
                                    'state': self.expiry.state(stack, now, exempt),
                                    'expires_on': _day(
                                        self.expiry.expires_on(stack, exempt)
                                    ),
                                }
                                for stack in sorted(owned, key=lambda s: s.name or '')
                            ],
                            'hiding': self.expiry.is_enforced(now),
                        },
                    )
                )
                sent += 1
            except Exception as e:
                self.logger.warning(
                    f'could not send a stack expiry digest: {type(e).__name__}'
                )
        return sent

    def _claim(self, expected: Optional[int], value: int) -> bool:
        config = self.context.config()
        real_key = config.get_real_key(LAST_DIGEST_KEY)
        if not config.db.set_config_entry_if(real_key, value, expected):
            return False
        config.put(real_key, value)
        return True
