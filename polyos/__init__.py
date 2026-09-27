"""PolyOS: a desktop environment for Debian, based on the PolyOS Scratch project."""

# Raising this on main publishes a new release: GitHub builds both ISOs and the website's
# Download buttons offer them (.github/workflows/iso.yml).
__version__ = "1.2.1"
# "stable", or "beta" to publish this version as a pre-release that only the Beta and Developer
# update channels get (Settings > Updates).
__channel__ = "stable"
