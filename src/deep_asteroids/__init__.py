try:
    from deep_asteroids._version import __version__
except ImportError:  # pragma: no cover -- not installed via setuptools_scm
    __version__ = "unknown"
