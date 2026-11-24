#!/bin/bash
# ideactl in a task's shell. Each module package also installs a console script by this name; the
# scheduler's took the path and answered "No such command" for everything the documented CLI does.
# A module's own commands still reach its CLI, in that module's task: they talk to the application
# running there, and from any other task they failed with a traceback.
role="${IDEA_CONTAINER_ROLE:-}"
case "${1:-}" in
  # every module has these two; outside a task the scheduler's copy answers, as it did before
  app-module-clean-up|logs) owner="${role:-scheduler}" ;;
  ami-builder|jobs|provision-always-on-nodes) owner=scheduler ;;
  accounts|groups|ldap) owner=cluster-manager ;;
  batch-create-sessions|build-desktop-image|cleanup-orphaned-schedules|merge-software-stacks|reindex-software-stacks|reindex-user-sessions|terminate-sessions|update-base-stacks) owner=vdc ;;
  *) exec node "${IDEACTL_MAIN:-/opt/idea/ideactl/dist/src/cli/main.js}" "$@" ;;
esac
case "${owner}" in
  scheduler) cli=idea-scheduler-cli container=scheduler-container ;;
  cluster-manager) cli=idea-cluster-manager-cli container=cluster-manager-container ;;
  vdc) cli=idea-vdc-cli container=controller-container ;;
  *)
    echo "ideactl $1 runs in the cluster-manager, vdc or scheduler task's shell; this is the ${role} task" >&2
    exit 2 ;;
esac
if [[ -n "${role}" && "${role}" != "${owner}" ]]; then
  echo "ideactl $1 runs in the ${owner} task's shell (container ${container}); this is the ${role} task" >&2
  exit 2
fi
exec "${cli}" "$@"
