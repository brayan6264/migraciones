import pytest
from hypothesis import given
from hypothesis import strategies as st

from document_engine.domain.naming_rules import (
    MAX_BASE_LENGTH,
    NAME_PATTERN,
    NamingRulesEngine,
    normalize_name,
    resolve_collision,
    sanitize_base,
    split_base_and_extension,
    strip_diacritics,
)

ABBREVIATIONS = {
    "INFORME": "INF",
    "BIMESTRAL": "BIM",
    "ACTA": "ACT",
    "COMITE": "COMITE",
    "SOPORTE": "SOP",
    "PAGO": "PAGO",
}


def test_strips_diacritics():
    assert strip_diacritics("Diagnóstico") == "Diagnostico"
    assert strip_diacritics("Ñandú") == "Nandu"


def test_uppercases_result():
    result = normalize_name("informe.pdf", abbreviations=ABBREVIATIONS)
    assert result.base == result.base.upper()


def test_replaces_special_characters_and_spaces_with_underscore():
    result = normalize_name("Reporte (final) #2025.pdf")
    assert "(" not in result.base
    assert ")" not in result.base
    assert "#" not in result.base
    assert " " not in result.base
    assert NAME_PATTERN.match(result.base)


def test_collapses_and_strips_underscores():
    assert sanitize_base("  __Hola---Mundo__  ") == "HOLA_MUNDO"
    assert sanitize_base("___") == ""


def test_extension_kept_separate_and_lowercased():
    base, ext = split_base_and_extension("Archivo.PDF")
    assert base == "Archivo"
    assert ext == "pdf"


def test_name_without_extension_has_empty_extension():
    base, ext = split_base_and_extension("SOLONOMBRE")
    assert base == "SOLONOMBRE"
    assert ext == ""


@pytest.mark.parametrize("length", [1, 10, 25])
def test_names_up_to_25_chars_never_need_ai(length):
    original = "A" * length
    result = normalize_name(original)
    assert len(result.base) == length
    assert result.needs_ai is False


@pytest.mark.parametrize("length", [26, 30, 100])
def test_names_over_25_chars_need_ai(length):
    original = "A" * length
    result = normalize_name(original)
    assert result.needs_ai is True


def test_obtc_code_preserved_at_start():
    result = normalize_name("Diagnostico anual.pdf", obtc_code="147")
    assert result.base.startswith("147_")


def test_date_kept_at_end():
    result = normalize_name(
        "Informe Bimestral.pdf",
        abbreviations=ABBREVIATIONS,
        obtc_code="147",
        version=2,
        date="20260709",
    )
    assert result.base == "147_INF_BIM_V02_20260709"
    assert result.base.endswith("20260709")


def test_version_format_v_two_digits():
    result = normalize_name("Reporte.pdf", version=2)
    assert "_V02" in result.base
    result9 = normalize_name("Reporte.pdf", version=9)
    assert "_V09" in result9.base


@pytest.mark.parametrize("n,expected", [(1, "01"), (9, "09"), (12, "12")])
def test_consecutive_uses_two_digits(n, expected):
    result = normalize_name("Informe.pdf", abbreviations=ABBREVIATIONS, consecutive=n)
    assert result.base.endswith(expected)


def test_valid_pattern_examples_from_spec():
    for name in ["147_DIAGNOSTICO", "INF_BIM_01", "ACT_COMITE_03", "SOP_PAGO_02", "01_INF_BIM_01"]:
        assert NAME_PATTERN.match(name)


def test_never_calls_ai_for_short_names_even_if_not_descriptive():
    result = normalize_name("X.pdf")
    assert result.needs_ai is False


def test_abbreviation_catalog_from_yaml_uppercases_keys(tmp_path):
    yaml_path = tmp_path / "abbrev.yml"
    yaml_path.write_text("informe: inf\n", encoding="utf-8")
    engine = NamingRulesEngine.from_yaml(yaml_path)
    result = engine.normalize("Informe mensual.pdf")
    assert "INF" in result.base.split("_")


# --- Colisiones (sección 5.4) ---------------------------------------------------


def test_no_collision_returns_candidate_unchanged():
    resolution = resolve_collision("INF_BIM_01", existing_bases=set())
    assert resolution.final_base == "INF_BIM_01"
    assert resolution.suffix_used is None
    assert resolution.requires_review is False


def test_collision_appends_first_available_consecutive_suffix():
    existing = {"INF_BIM_01"}
    resolution = resolve_collision("INF_BIM_01", existing_bases=existing)
    assert resolution.final_base == "INF_BIM_01_01"
    assert resolution.suffix_used == "_01"
    assert resolution.requires_review is False


def test_collision_skips_taken_suffixes():
    existing = {"INF_BIM_01", "INF_BIM_01_01", "INF_BIM_01_02"}
    resolution = resolve_collision("INF_BIM_01", existing_bases=existing)
    assert resolution.final_base == "INF_BIM_01_03"


def test_collision_result_is_stable_between_calls():
    existing = {"INF_BIM_01"}
    first = resolve_collision("INF_BIM_01", existing_bases=existing)
    second = resolve_collision("INF_BIM_01", existing_bases=existing)
    assert first == second


def test_collision_reserves_space_within_max_length():
    long_base = "A" * MAX_BASE_LENGTH
    resolution = resolve_collision(long_base, existing_bases={long_base}, max_length=MAX_BASE_LENGTH)
    assert len(resolution.final_base) <= MAX_BASE_LENGTH
    assert resolution.final_base.endswith("_01")


def test_collision_fallback_after_99_attempts_marks_requires_review():
    base = "DOC"
    existing = {base} | {f"{base}_{n:02d}" for n in range(1, 100)}
    resolution = resolve_collision(base, existing_bases=existing)
    assert resolution.requires_review is True
    assert len(resolution.final_base) <= MAX_BASE_LENGTH
    assert resolution.final_base not in existing


def test_collision_never_silently_overwrites():
    existing = {"REPORTE"}
    resolution = resolve_collision("REPORTE", existing_bases=existing)
    assert resolution.final_base != "REPORTE"
    assert resolution.final_base not in existing


# --- Propiedad: cualquier texto sanitizado cumple el patrón o queda vacío -------


@given(st.text(min_size=0, max_size=80))
def test_sanitize_base_always_matches_pattern_or_is_empty(text):
    result = sanitize_base(text)
    assert result == "" or NAME_PATTERN.match(result)
    assert "__" not in result
    assert not result.startswith("_")
    assert not result.endswith("_")
