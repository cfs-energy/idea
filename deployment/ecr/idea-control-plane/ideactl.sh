#!/bin/bash
# ideactl in a task's shell. The scheduler package also installs a console script by this name; it
# took the path and answered "No such command" for everything the documented CLI does. Its own
# commands still reach it.
case "${1:-}" in
  ami-builder|app-module-clean-up|jobs|logs|provision-always-on-nodes) exec idea-scheduler-cli "$@" ;;
  *) exec node "${IDEACTL_MAIN:-/opt/idea/ideactl/dist/src/cli/main.js}" "$@" ;;
esac
