#!/bin/bash
# ideactl in a task's shell. The scheduler package also installs a console script by this name; it
# took the path and answered "No such command" for everything the documented CLI does. Its own
# commands still reach it, in the scheduler task: they talk to the scheduler running there, and
# from any other task they failed with a traceback.
case "${1:-}" in
  ami-builder|app-module-clean-up|jobs|logs|provision-always-on-nodes)
    if [[ -n "${IDEA_CONTAINER_ROLE:-}" && "${IDEA_CONTAINER_ROLE}" != scheduler ]]; then
      echo "ideactl $1 runs in the scheduler task's shell (container scheduler-container); this is the ${IDEA_CONTAINER_ROLE} task" >&2
      exit 2
    fi
    exec idea-scheduler-cli "$@" ;;
  *) exec node "${IDEACTL_MAIN:-/opt/idea/ideactl/dist/src/cli/main.js}" "$@" ;;
esac
