"""PolyOS: a desktop environment for Debian, based on the PolyOS Scratch project."""

# Raising this on main publishes a new release: GitHub builds both ISOs and the website's
# Download buttons offer them (.github/workflows/iso.yml).
__version__ = "1.4.1"
# "stable", or "beta" to publish this version as a pre-release that only the Beta and Developer
# update channels get (Settings > Updates).
__channel__ = "stable"
# Which editions this release is for: () is everyone. ("developer",) sends it only to Developer
# edition computers (a GitHub pre-release; others stay on the newest release for everyone).
__editions__: tuple[str, ...] = ()
