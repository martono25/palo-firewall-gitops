"""fwgitops — GitOps firewall automation for Palo Alto Strata Cloud Manager.

See docs/DESIGN.md for the full architecture. Build order starts with the
tag/identity convention (`fwgitops.tags`), the shared contract every other
subsystem depends on.
"""

# ONE SOURCE OF TRUTH: pyproject.toml, read at import.
#
# This was a hardcoded "1.0.0" while pyproject said 2.3.0 and the repo was 139
# commits past the v2.3.0 tag. It is not decoration — `evidence.py` stamps it
# into every bundle as `compiled.compiler_version`, so every audit record on
# `main` claimed it was produced by a version that shipped months earlier. An
# assessor tracing a defect to the tool that made it would have been sent to the
# wrong code.
#
# Two constants for one fact drift the moment somebody bumps the one they can
# see. Reading the installed metadata means there is only ever one.
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("fwgitops")
except PackageNotFoundError:  # not installed (a source checkout, or a build)
    __version__ = "0.0.0+unknown"
