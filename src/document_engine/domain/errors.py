class DocumentEngineError(Exception):
    """Base para errores de dominio con un código interno estable."""

    code: str = "UNKNOWN_ERROR"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class TransientError(DocumentEngineError):
    """Error recuperable: reintentar con backoff."""


class PermanentError(DocumentEngineError):
    """Error no recuperable: no reintentar, requiere intervención o bloqueo."""


class InvalidStateTransition(DocumentEngineError):
    code = "INVALID_STATE_TRANSITION"


# Códigos estables referenciados en la spec (sección 16)
DRIVE_PERMISSION_DENIED = "DRIVE_PERMISSION_DENIED"
DRIVE_ITEM_NOT_FOUND = "DRIVE_ITEM_NOT_FOUND"
DRIVE_EXPORT_UNSUPPORTED = "DRIVE_EXPORT_UNSUPPORTED"
FTP_AUTH_FAILED = "FTP_AUTH_FAILED"
FTP_RESUME_UNSUPPORTED = "FTP_RESUME_UNSUPPORTED"
FTP_WRITE_DENIED = "FTP_WRITE_DENIED"
NAME_MISSING_OBTC = "NAME_MISSING_OBTC"
NAME_AI_INVALID_OUTPUT = "NAME_AI_INVALID_OUTPUT"
NAME_COLLISION_UNRESOLVED = "NAME_COLLISION_UNRESOLVED"
VALIDATION_SIZE_MISMATCH = "VALIDATION_SIZE_MISMATCH"
VALIDATION_HASH_MISMATCH = "VALIDATION_HASH_MISMATCH"
