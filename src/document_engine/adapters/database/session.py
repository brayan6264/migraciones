from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from document_engine.settings import get_settings


def make_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    # `timeout` (segundos que sqlite3 espera un lock antes de fallar) usa el
    # default de 5s si no se especifica; lo subimos porque con varios workers
    # en paralelo + peticiones HTTP concurrentes tocando el mismo archivo, un
    # choque de locks bajo carga real es mucho más probable que en desarrollo
    # con una sola conexión a la vez.
    if not url.startswith("sqlite"):
        return create_engine(url)

    connect_args = {"check_same_thread": False, "timeout": 30}
    if ":memory:" in url:
        # En pruebas: una sola conexión compartida (StaticPool), si no cada
        # conexión vería su propia base en memoria vacía.
        engine = create_engine(url, connect_args=connect_args, poolclass=StaticPool)
    else:
        # NullPool: cada sesión abre su propia conexión y la cierra al terminar,
        # sin un pool acotado que pueda AGOTARSE. Es deliberado: con varios
        # workers en paralelo procesando archivos enormes (varios GB), si un
        # elemento supera el timeout duro su hilo se abandona sin devolver la
        # conexión; con un QueuePool acotado esas conexiones colgadas se
        # acumulaban hasta agotar el pool y tumbaban HASTA los GET de estado.
        # Las conexiones SQLite son baratas de abrir, así que no hay costo real.
        engine = create_engine(url, connect_args=connect_args, poolclass=NullPool)

    if ":memory:" not in url:
        # WAL permite lectores concurrentes + un escritor sin bloquearse entre
        # sí (a diferencia del journal por defecto, que serializa todo). Es
        # clave ahora que `POST .../run` procesa varios elementos en paralelo,
        # cada uno con su propia sesión escribiendo estados y eventos. No se
        # aplica a `:memory:` (cada conexión vería su propia base vacía).
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Iterator[Session]:
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
