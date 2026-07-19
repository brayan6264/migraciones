from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


class ItemType(StrEnum):
    FILE = "FILE"
    FOLDER = "FOLDER"
    SHORTCUT = "SHORTCUT"


class SnapshotStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Priority(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


PRIORITY_VALUES: dict[Priority, int] = {
    Priority.CRITICAL: 100,
    Priority.HIGH: 75,
    Priority.NORMAL: 50,
    Priority.LOW: 25,
}


class SelectorKind(StrEnum):
    EXPLICIT_IDS = "EXPLICIT_IDS"
    FOLDER_RECURSIVE = "FOLDER_RECURSIVE"
    PATH_PREFIX = "PATH_PREFIX"
    SEARCH_RESULT = "SEARCH_RESULT"


class RenameMethod(StrEnum):
    UNCHANGED = "UNCHANGED"
    RULE_BASED = "RULE_BASED"
    AI_ASSISTED = "AI_ASSISTED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    COLLISION_RESOLUTION = "COLLISION_RESOLUTION"


class PlannedAction(StrEnum):
    CREATE_FOLDER = "CREATE_FOLDER"
    DOWNLOAD = "DOWNLOAD"
    EXPORT = "EXPORT"
    UPLOAD = "UPLOAD"
    VALIDATE = "VALIDATE"
    SKIP = "SKIP"
    BLOCK = "BLOCK"


class MigrationItemState(StrEnum):
    DISCOVERED = "DISCOVERED"
    PLANNED = "PLANNED"
    WAITING_REVIEW = "WAITING_REVIEW"
    READY = "READY"
    CREATING_DIRECTORIES = "CREATING_DIRECTORIES"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    UPLOADING = "UPLOADING"
    UPLOADED_TEMP = "UPLOADED_TEMP"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ValidationLevel(StrEnum):
    BASIC = "BASIC"
    STRONG = "STRONG"
    STRICT = "STRICT"
