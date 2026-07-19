import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from document_engine.adapters.database.models import Base
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.database.models import NameDecision
from document_engine.application.naming_service import NamingAssistantService, validate_ai_suggestion
from document_engine.domain.enums import MigrationItemState, RenameMethod
from document_engine.domain.errors import TransientError
from document_engine.domain.naming_rules import NamingRulesEngine
from document_engine.ports.ai_naming_provider import AINamingResponse
from tests.unit.fakes import FakeAINamingProvider


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def make_batch(db) -> MigrationBatchModel:
    batch = MigrationBatchModel(snapshot_id="snap-1", name="ola", priority=50, status="PLANNED")
    db.add(batch)
    db.commit()
    return batch


def make_waiting_item(
    db,
    batch_id: str,
    *,
    source_name: str = "Documento extremadamente descriptivo y largo para probar la IA.pdf",
    source_path: str = "ROOT/Carpeta A/Documento extremadamente descriptivo y largo para probar la IA.pdf",
    planned_destination_path: str = "ROOT/CARPETA_A/DOCUMENTO_EXTREMADAMENTE_DESCRIP",
    idempotency_key: str | None = None,
) -> MigrationItemModel:
    import uuid

    item = MigrationItemModel(
        batch_id=batch_id,
        source_item_id=str(uuid.uuid4()),
        source_path=source_path,
        source_name=source_name,
        source_mime_type="application/pdf",
        source_size=100,
        item_type="FILE",
        priority=50,
        planned_destination_path=planned_destination_path,
        planned_destination_name=planned_destination_path.rsplit("/", 1)[-1],
        extension="pdf",
        rename_method=RenameMethod.AI_ASSISTED.value,
        state=MigrationItemState.WAITING_REVIEW.value,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def naming_engine():
    return NamingRulesEngine({})


def test_valid_ai_response_marks_item_ready(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item = make_waiting_item(db, batch.id)

    provider = FakeAINamingProvider(
        [AINamingResponse(suggested_name="DOC_LARGO_IA", reason="resume", confidence=0.9, requires_review=False)]
    )
    service = NamingAssistantService(db, provider, naming_engine)

    resolved = service.resolve_item(item.id)

    assert resolved.state == MigrationItemState.READY.value
    assert resolved.planned_destination_name == "DOC_LARGO_IA.pdf"
    assert resolved.rename_method == RenameMethod.AI_ASSISTED.value
    assert len(provider.calls) == 1


def test_invalid_then_valid_retry_succeeds(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item = make_waiting_item(db, batch.id)

    provider = FakeAINamingProvider(
        [
            AINamingResponse(suggested_name="nombre con espacios", reason="x", confidence=0.5, requires_review=False),
            AINamingResponse(suggested_name="DOC_CORREGIDO", reason="corregido", confidence=0.8, requires_review=False),
        ]
    )
    service = NamingAssistantService(db, provider, naming_engine)

    resolved = service.resolve_item(item.id)

    assert len(provider.calls) == 2
    assert provider.calls[1].previous_errors  # se enviaron los errores de validación
    assert resolved.planned_destination_name == "DOC_CORREGIDO.pdf"
    assert resolved.state == MigrationItemState.READY.value


def test_invalid_twice_falls_back_deterministically(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item = make_waiting_item(db, batch.id)

    bad = AINamingResponse(suggested_name="nombre con espacios y más de 25 caracteres", reason="x", confidence=0.2, requires_review=False)
    provider = FakeAINamingProvider([bad, bad])
    service = NamingAssistantService(db, provider, naming_engine)

    resolved = service.resolve_item(item.id)

    assert len(provider.calls) == 2
    assert resolved.state == MigrationItemState.WAITING_REVIEW.value
    decision = db.query(NameDecision).filter_by(migration_item_id=item.id).one()
    assert decision.fallback_reason is not None
    assert decision.requires_review is True


def test_transient_provider_error_triggers_fallback(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item = make_waiting_item(db, batch.id)

    provider = FakeAINamingProvider([TransientError("timeout")])
    service = NamingAssistantService(db, provider, naming_engine)

    resolved = service.resolve_item(item.id)

    assert resolved.state == MigrationItemState.WAITING_REVIEW.value
    decision = db.query(NameDecision).filter_by(migration_item_id=item.id).one()
    assert "AI_PROVIDER_ERROR" in decision.fallback_reason


def test_cache_avoids_second_ai_call_for_same_input(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item_a = make_waiting_item(db, batch.id, source_path="ROOT/Carpeta A/Igual.pdf")
    item_b = make_waiting_item(
        db,
        batch.id,
        source_path="ROOT/Carpeta A/Igual.pdf",
        source_name=item_a.source_name,
        planned_destination_path="ROOT/CARPETA_A/OTRO_DESTINO",
    )

    provider = FakeAINamingProvider(
        [AINamingResponse(suggested_name="DOC_CACHEADO", reason="x", confidence=0.9, requires_review=False)]
    )
    service = NamingAssistantService(db, provider, naming_engine)

    service.resolve_item(item_a.id)
    resolved_b = service.resolve_item(item_b.id)

    assert len(provider.calls) == 1  # segunda resolución usó la caché
    # Ambos ítems caen en la misma carpeta destino, así que aunque el nombre
    # venga de caché, la colisión real contra el hermano ya resuelto se
    # detecta y resuelve con sufijo.
    assert resolved_b.planned_destination_name == "DOC_CACHEADO_01.pdf"


def test_collision_between_two_ai_suggestions_gets_suffixed(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item_a = make_waiting_item(
        db,
        batch.id,
        source_path="ROOT/Carpeta A/Primero muy largo nombre.pdf",
        planned_destination_path="ROOT/CARPETA_A/PRIMERO_MUY_LARGO_NOMBRE",
    )
    item_b = make_waiting_item(
        db,
        batch.id,
        source_path="ROOT/Carpeta A/Segundo muy largo nombre.pdf",
        planned_destination_path="ROOT/CARPETA_A/SEGUNDO_MUY_LARGO_NOMBRE",
    )

    provider = FakeAINamingProvider(
        [
            AINamingResponse(suggested_name="DOC_COMUN", reason="a", confidence=0.9, requires_review=False),
            AINamingResponse(suggested_name="DOC_COMUN", reason="b", confidence=0.9, requires_review=False),
        ]
    )
    service = NamingAssistantService(db, provider, naming_engine)

    resolved_a = service.resolve_item(item_a.id)
    resolved_b = service.resolve_item(item_b.id)

    assert resolved_a.planned_destination_name == "DOC_COMUN.pdf"
    assert resolved_b.planned_destination_name == "DOC_COMUN_01.pdf"
    assert resolved_b.rename_method == RenameMethod.COLLISION_RESOLUTION.value


def test_resolve_item_rejects_non_ai_items_without_force(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item = make_waiting_item(db, batch.id)
    item.rename_method = RenameMethod.RULE_BASED.value
    db.commit()

    provider = FakeAINamingProvider(
        [AINamingResponse(suggested_name="DOC_X", reason="x", confidence=0.9, requires_review=False)]
    )
    service = NamingAssistantService(db, provider, naming_engine)

    from document_engine.domain.errors import DocumentEngineError

    with pytest.raises(DocumentEngineError):
        service.resolve_item(item.id)


def test_requires_review_true_from_ai_keeps_waiting_review(naming_engine):
    db = make_session()
    batch = make_batch(db)
    item = make_waiting_item(db, batch.id)

    provider = FakeAINamingProvider(
        [AINamingResponse(suggested_name="DOC_INCIERTO", reason="poca info", confidence=0.3, requires_review=True)]
    )
    service = NamingAssistantService(db, provider, naming_engine)

    resolved = service.resolve_item(item.id)

    assert resolved.state == MigrationItemState.WAITING_REVIEW.value
    assert resolved.planned_destination_name == "DOC_INCIERTO.pdf"


# --- validate_ai_suggestion (unitaria pura) --------------------------------


def test_validate_rejects_too_long():
    errors = validate_ai_suggestion("A" * 30, obtc_code=None, date=None)
    assert errors


def test_validate_rejects_invalid_chars():
    errors = validate_ai_suggestion("doc con espacios", obtc_code=None, date=None)
    assert errors


def test_validate_requires_obtc_prefix():
    errors = validate_ai_suggestion("OTRO_NOMBRE", obtc_code="147", date=None)
    assert any("OBTC" in e for e in errors)
    assert not validate_ai_suggestion("147_NOMBRE", obtc_code="147", date=None)


def test_validate_requires_date_suffix():
    errors = validate_ai_suggestion("NOMBRE_SIN_FECHA", obtc_code=None, date="20260709")
    assert any("fecha" in e for e in errors)
    assert not validate_ai_suggestion("NOMBRE_20260709", obtc_code=None, date="20260709")
