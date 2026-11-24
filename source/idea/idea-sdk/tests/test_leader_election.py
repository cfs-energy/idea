import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from ideasdk.clustering import leader_election_constants
from ideasdk.clustering.leader_election import LeaderElection
from ideasdk.distributed_lock.distributed_lock import DistributedLock


class Leases:
    """one shared lease store, and one holder handle per elector"""

    def __init__(self):
        self.holder = None
        self.changed = threading.Lock()

    def lock_for(self, name):
        store = self

        class Lock:
            def acquire(self, key):
                with store.changed:
                    if store.holder not in (None, name):
                        from python_dynamodb_lock.python_dynamodb_lock import (
                            DynamoDBLockError,
                        )

                        raise DynamoDBLockError(code=DynamoDBLockError.ACQUIRE_TIMEOUT)
                    store.holder = name

            def is_held(self, key):
                return store.holder == name

            def release(self, key):
                pass

        return Lock()


def elector(leases, name):
    election = LeaderElection.__new__(LeaderElection)
    published = []
    lock = leases.lock_for(name)
    election.context = SimpleNamespace(
        distributed_lock=lambda: lock,
        broadcast=lambda: SimpleNamespace(
            publish=lambda service, message: published.append(message)
        ),
    )
    election.logger = MagicMock()
    election.leader_election_interval = 0.05
    election._exit = threading.Event()
    election.LEADER_ELECTION_KEY = 'module-leader-node'
    election._is_leader = threading.Event()
    election.shell = MagicMock()
    thread = threading.Thread(target=election.loop, daemon=True)
    thread.start()
    return election, published, thread


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_a_leader_whose_lease_lapsed_stops_leading():
    leases = Leases()
    first, first_messages, first_thread = elector(leases, 'first')
    wait_for(first.is_leader)

    # the first task paused past its lease; the second took it
    leases.holder = 'second'
    assert not first.is_leader()
    wait_for(lambda: leader_election_constants.LEADER_DISMISSED in first_messages)

    second, _, second_thread = elector(leases, 'second')
    wait_for(second.is_leader)
    assert not first.is_leader()
    for election, thread in ((first, first_thread), (second, second_thread)):
        election._exit.set()
        thread.join(5)


def test_the_lease_test_needs_no_call_to_the_store():
    lock = DistributedLock.__new__(DistributedLock)
    lock._lock = threading.Lock()
    lock._lock_client = SimpleNamespace(
        _safe_period=SimpleNamespace(total_seconds=lambda: 20)
    )
    held = SimpleNamespace(
        status='LOCKED',
        LOCKED='LOCKED',
        last_updated_time=time.monotonic(),
        thread_lock=threading.Lock(),
    )
    lock._active_locks = {'k': held}
    assert lock.is_held('k')
    held.last_updated_time -= 21
    assert not lock.is_held('k')
    assert not lock.is_held('other')


def test_stopping_releases_every_held_lock():
    lock = DistributedLock.__new__(DistributedLock)
    lock._lock = threading.Lock()
    lock.logger = MagicMock()
    client = MagicMock()
    lock._lock_client = client
    lock._active_locks = {'a': object(), 'b': object()}
    lock.stop()
    assert lock._active_locks == {}
    client.close.assert_called_once_with(release_locks=True)
