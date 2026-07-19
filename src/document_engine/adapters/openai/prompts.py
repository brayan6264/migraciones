from document_engine.ports.ai_naming_provider import AINamingRequest

SYSTEM_PROMPT = """Eres un asistente de nomenclatura documental. Debes abreviar un nombre que supera 25 caracteres.

Devuelve únicamente la estructura JSON solicitada.

REGLAS:
- Máximo 25 caracteres, sin contar la extensión.
- Solo A-Z, 0-9 y guion bajo.
- Todo en MAYÚSCULAS.
- Sin tildes ni caracteres especiales.
- No inventes códigos OBTC, fechas, versiones o datos.
- Si se proporciona un código OBTC, consérvalo al inicio.
- Si se proporciona una fecha, consérvala al final en AAAAMMDD.
- Utiliza el árbol de carpetas para conservar el significado más útil.
- Usa abreviaturas estandarizadas.
- El nombre debe identificar el contenido sin necesidad de abrir el archivo."""


def build_user_prompt(request: AINamingRequest) -> str:
    prompt = f"""TIPO: {request.item_type}
NOMBRE ORIGINAL: {request.original_name}
NOMBRE NORMALIZADO: {request.normalized_name}
EXTENSION O MIME: {request.extension_or_mime}
ARBOL DE UBICACION: {request.ancestor_path}
CONTEXTO LOCAL: {request.local_context}
CODIGO OBTC: {request.obtc_code or "none"}
FECHA: {request.date or "none"}
VERSION: {request.version or "none"}
CATEGORIA: {request.category or "none"}
ABREVIATURAS: {request.abbreviation_catalog}"""

    if request.previous_errors:
        errors = "\n".join(f"- {e}" for e in request.previous_errors)
        prompt += f"\n\nLa respuesta anterior fue inválida por:\n{errors}\nCorrígela."
    return prompt
