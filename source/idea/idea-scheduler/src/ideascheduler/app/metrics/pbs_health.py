"""Whether the PBS server answers, and how many jobs it holds by state."""

from ideasdk.metrics.health import HealthCollector, IsolatedProbe

import json

QSTAT = ['/opt/pbs/bin/qstat', '-B', '-f', '-F', 'json']


class PbsHealthCollector(HealthCollector):
    name = 'pbs'
    interval_seconds = 60
    time_limit_seconds = 30
    PROBE_SECONDS = 20

    def __init__(self, argv=None):
        self._probe = IsolatedProbe(argv or QSTAT, self.PROBE_SECONDS)

    def collect(self, metrics) -> None:
        result = self._probe.run()
        server = None
        if result.status == 'ok':
            try:
                servers = json.loads(result.output).get('Server') or {}
                server = next(iter(servers.values()), None)
            except ValueError:
                server = None
        metrics.level(
            'scheduler.pbs_up',
            1 if server and server.get('server_state') == 'Active' else 0,
            reason='ok' if server else result.status,
        )
        if result.seconds is not None:
            metrics.level('scheduler.pbs_check_seconds', result.seconds)
        if not server:
            return
        for pair in str(server.get('state_count') or '').split():
            state, _, count = pair.partition(':')
            if count.isdigit():
                metrics.level('scheduler.jobs', int(count), state=state.lower())
