"""Python 3.6-compatible CamFlow runtime metadata."""

__version__ = "1.2.1"
__build__ = ""

# Installed Python packages use the generated readable asset module.  The
# standalone builder runs with no package and replaces the empty mappings.
if __package__:
    from .embedded_assets import EMBEDDED_ASSETS as _EMBEDDED_ASSETS, EMBEDDED_SKILLS as _EMBEDDED_SKILLS
else:
    EMBEDDED_SKILLS = {}
    EMBEDDED_ASSETS = {}
