"""Shared gateway exception types."""


class MultiplexConfigError(RuntimeError):
    """A profile multiplexer configuration is invalid and must fail startup.

    Transient adapter failures are retried, but a multiplex configuration error
    requires an operator correction and therefore aborts startup cleanly.
    """
