from document_engine.domain.enums import MigrationItemState as S
from document_engine.domain.errors import InvalidStateTransition

# Transiciones permitidas para un elemento de migración (sección 9.3 de la spec).
ALLOWED_TRANSITIONS: dict[S, set[S]] = {
    S.DISCOVERED: {S.PLANNED, S.CANCELLED},
    S.PLANNED: {S.WAITING_REVIEW, S.READY, S.BLOCKED, S.CANCELLED},
    S.WAITING_REVIEW: {S.READY, S.BLOCKED, S.CANCELLED},
    S.READY: {S.CREATING_DIRECTORIES, S.CANCELLED, S.SKIPPED},
    # DOWNLOADED es alcanzable directamente para carpetas: crear el
    # directorio ya cumple la acción, no hay contenido que descargar.
    # COMPLETED también es alcanzable desde cualquier estado "en vuelo": es
    # la transición que usa RecoveryService cuando, tras una caída, el
    # destino remoto ya refleja una transferencia exitosa que la base de
    # datos no alcanzó a registrar (sección 9.5).
    S.CREATING_DIRECTORIES: {S.DOWNLOADING, S.DOWNLOADED, S.RETRY_PENDING, S.FAILED, S.BLOCKED, S.COMPLETED},
    S.DOWNLOADING: {S.DOWNLOADED, S.RETRY_PENDING, S.FAILED, S.BLOCKED, S.COMPLETED},
    S.DOWNLOADED: {S.UPLOADING, S.RETRY_PENDING, S.FAILED, S.COMPLETED},
    S.UPLOADING: {S.UPLOADED_TEMP, S.RETRY_PENDING, S.FAILED, S.COMPLETED},
    S.UPLOADED_TEMP: {S.VALIDATING, S.RETRY_PENDING, S.FAILED, S.COMPLETED},
    S.VALIDATING: {S.COMPLETED, S.RETRY_PENDING, S.FAILED},
    S.COMPLETED: set(),
    S.RETRY_PENDING: {
        S.CREATING_DIRECTORIES,
        S.DOWNLOADING,
        S.UPLOADING,
        S.VALIDATING,
        S.FAILED,
        S.CANCELLED,
    },
    S.FAILED: {S.RETRY_PENDING, S.BLOCKED, S.CANCELLED},
    S.BLOCKED: {S.WAITING_REVIEW, S.CANCELLED},
    S.SKIPPED: set(),
    S.CANCELLED: set(),
}


def ensure_valid_transition(current: S, target: S) -> None:
    if target == current:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStateTransition(
            f"Transición inválida de {current} a {target}",
            code="INVALID_STATE_TRANSITION",
        )


def transition(current: S, target: S) -> S:
    ensure_valid_transition(current, target)
    return target
