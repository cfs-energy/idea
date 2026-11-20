"""
Every namespace the SDK's service clients send is one a service serves: the cluster
manager's dispatch tables for Accounts.* and Projects.*, and the desktop controller's queue
consumer for the event it receives. A client that names a namespace nobody serves gets an
error the caller may read as "not found" (a group share admitted nobody this way).
"""

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
CLIENTS = SOURCE / 'idea-sdk' / 'src' / 'ideasdk' / 'client'
CLUSTER_MANAGER_API = (
    SOURCE / 'idea-cluster-manager' / 'src' / 'ideaclustermanager' / 'app' / 'api'
)
CONTROLLER_QUEUE = (
    SOURCE
    / 'idea-virtual-desktop-controller'
    / 'src'
    / 'ideavirtualdesktopcontroller'
    / 'app'
    / 'events'
    / 'service'
    / 'controller_queue_monitor_service.py'
)


def sent_namespaces():
    sent = {}
    for path in CLIENTS.glob('*.py'):
        for namespace in re.findall(
            r"namespace='([A-Za-z]+\.[A-Za-z]+)'", path.read_text()
        ):
            sent.setdefault(namespace, path.name)
    return sent


def served_by_cluster_manager():
    """the handler tables: a namespace is dispatched only when its API's table names it"""
    served = set()
    for name in ('accounts_api.py', 'projects_api.py'):
        served |= set(
            re.findall(
                r"'((?:Accounts|Projects)\.[A-Za-z]+)': \{",
                (CLUSTER_MANAGER_API / name).read_text(),
            )
        )
    return served


def test_the_scan_sees_the_clients_and_the_tables():
    sent = sent_namespaces()
    assert {'Accounts.GetUser', 'Projects.GetProject'} <= set(sent)
    assert {
        'Accounts.GetUser',
        'Accounts.GetGroup',
        'Projects.GetProject',
    } <= served_by_cluster_manager()


def test_every_namespace_a_client_sends_is_served():
    served = served_by_cluster_manager()
    invoker = (CLUSTER_MANAGER_API / 'api_invoker.py').read_text()
    queue = CONTROLLER_QUEUE.read_text()
    unserved = []
    for namespace, client in sorted(sent_namespaces().items()):
        if namespace.endswith('Event'):
            # an event the desktop controller consumes from its queue
            if f"'{namespace}'" not in queue:
                unserved.append((namespace, client, 'controller queue'))
            continue
        if namespace not in served or f"'{namespace}'" not in invoker:
            unserved.append((namespace, client, 'cluster manager'))
    assert unserved == []
