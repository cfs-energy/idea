"""Compatibility hook for existing clusters; validate_job now sizes missing CPUs."""

import pbs  # type: ignore[import-unresolved]

pbs.event().accept()
