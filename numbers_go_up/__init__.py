try:
    from numbers_go_up._version import version as __version__
except ImportError:
    # Local checkout: no git metadata, or _version.py not yet generated
    # (pip install -e . / python -m setuptools_scm).
    __version__ = "0.0.0+unknown"
