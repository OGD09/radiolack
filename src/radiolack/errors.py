"""Error families with distinct exit codes.

A disk problem (store unreadable, invalid or not writable) must never be
confused with a radio problem (receiver, I/Q stream or Gqrx control). Radio
disconnections that can recover (Gqrx closed, remote control lost) are not
errors: the monitor waits and reconnects.
"""


class RadioLackError(Exception):
    """Base class; ``exit_code`` is used by the command line."""

    exit_code = 1


class StorageError(RadioLackError):
    """Observation store or import source problem; the store was left intact."""

    exit_code = 3


class StoreFormatError(StorageError):
    """The file is not valid JSON or not in the expected format."""


class StoreMissingError(StorageError):
    """The observation store does not exist and creation was not requested."""


class StoreLockedError(StorageError):
    """Another RadioLack process is using the same store."""


class StoreWriteError(StorageError):
    """Writing to disk failed (full disk, read-only file system, permissions)."""


class ReceiverError(RadioLackError):
    """rtl_tcp, RTL-SDR device, local port or I/Q bridge problem."""

    exit_code = 4


class GqrxError(RadioLackError):
    """Gqrx answered unexpectedly or did not apply a requested setting."""

    exit_code = 5


class GqrxProtocolError(GqrxError):
    """Gqrx rejected a command or returned a malformed value."""


class SquelchVerificationError(GqrxError):
    """The squelch read back from Gqrx differs from the requested value."""


class StopRequested(Exception):
    """Raised by the SIGTERM handler for a clean shutdown."""
