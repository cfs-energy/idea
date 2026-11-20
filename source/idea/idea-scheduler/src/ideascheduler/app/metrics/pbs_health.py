"""Whether the PBS server answers, and how many jobs it holds by state."""

from ideasdk.metrics.health import HealthCollector, IsolatedProbe

from typing import Optional
import json

QSTAT = ['/opt/pbs/bin/qstat', '-B', '-f', '-F', 'json']
# a server mid cycle reads Scheduling; with a 30 s scheduler_iteration a one-minute sample
# lands in a cycle now and then, and that is a working server
UP_STATES = ('Active', 'Scheduling')


class PbsHealthCollector(HealthCollector):
    name = 'pbs'
    interval_seconds = 60
    time_limit_seconds = 30
    PROBE_SECONDS = 20

    def __init__(self, argv=None):
        self._probe = IsolatedProbe(argv or QSTAT, self.PROBE_SECONDS)

    def collect(self, metrics) -> None:
        result = self._probe.run()
        server = self._server(result.output) if result.status == 'ok' else None
        state = str(server.get('server_state') or '') if server else ''
        if result.status != 'ok':
            reason = result.status
        elif server is None:
            # qstat exited 0 but its answer named no server: not ok, though not a dead server
            reason = 'unreadable'
        elif state in UP_STATES:
            reason = 'ok'
        else:
            reason = state.lower() or 'no_state'
        metrics.level('scheduler.pbs_up', 1 if reason == 'ok' else 0, reason=reason)
        if result.seconds is not None:
            metrics.level('scheduler.pbs_check_seconds', result.seconds)
        if not server:
            return
        for pair in str(server.get('state_count') or '').split():
            state, _, count = pair.partition(':')
            if count.isdigit():
                metrics.level('scheduler.jobs', int(count), state=state.lower())

    @staticmethod
    def _server(output: str) -> Optional[dict]:
        """the first server in qstat's answer, or None for anything else: never raises, so
        an answer it cannot read still sends pbs_up"""
        try:
            servers = json.loads(output).get('Server')
            server = next(iter(servers.values()), None)
        except (ValueError, AttributeError, TypeError):
            return None
        return server if isinstance(server, dict) else None
