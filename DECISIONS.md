# Decisiones técnicas y limitaciones conocidas

## Decisiones técnicas

- **Clean Architecture / Ports & Adapters**: `domain/` no depende de nada;
  `application/` orquesta casos de uso sobre puertos (`ports/`);
  `adapters/` implementa esos puertos contra Google Drive, FTP/FTPS, OpenAI,
  SQLAlchemy y el filesystem local. La API (`api/`) y el worker (`worker/`,
  `scripts/run_worker.py`) son procesos separados que comparten la base de
  datos, tal como pide la sección 14 de la especificación.
- **Sin Celery**: el worker propio (`worker/lease_manager.py` +
  `application/migration_service.py`) implementa leases, heartbeats,
  recuperación y reintentos directamente sobre la base de datos, como
  permite explícitamente la sección 14.
- **Orden de prioridad + DFS sin columna de secuencia**: en vez de persistir
  un número de orden fijo (que se volvería inconsistente si la prioridad
  cambia en pausa), el orden se calcula en tiempo real ordenando por
  `(-priority, planned_destination_path)`. El separador `/` ordena antes que
  cualquier carácter alfanumérico en ASCII, así que el orden lexicográfico
  de la ruta ya produce DFS (una rama se completa antes de pasar a la
  siguiente).
- **Planning es el dry-run**: `PlanningService.generate_plan` nunca escribe
  en el destino. El modo `dry-run` de la sección 3 no es un modo aparte, es
  simplemente no llamar a `start`.
- **Caché de decisiones de IA reutilizando el modelo de datos**: en vez de
  una caché externa, `NamingAssistantService` reutiliza
  `NameDecision.input_fingerprint` como clave de caché persistente en la
  base de datos, lo que además la hace sobrevivir reinicios.
- **`COMPLETED` alcanzable desde cualquier estado "en vuelo"**: se añadió
  explícitamente a la máquina de estados para que `RecoveryService` pueda
  saltar directo a `COMPLETED` cuando, tras una caída, el destino remoto ya
  refleja una transferencia exitosa que la base de datos no alcanzó a
  registrar (sección 9.5).
- **Validación STRICT reutiliza `download_to`**: se agregó un método no
  estrictamente parte de la lista mínima del puerto de destino
  (`download_to`, `get_checksum`) porque la sección 10 exige poder
  re-descargar y comparar hash para el nivel STRICT.

## Limitaciones conocidas (documentadas, no ocultas)

1. **`POST /migration-batches/{id}/start` es síncrono**: procesa hasta
   `max_items` elementos dentro de la misma petición HTTP. Es adecuado para
   demostraciones y lotes pequeños. Para volúmenes grandes de producción,
   usar `scripts/run_worker.py` como proceso independiente de larga
   duración (o varios, gracias al mecanismo de leases).
2. **`SELECT ... FOR UPDATE` no implementado en `claim_next_item`**: SQLite
   no lo soporta bien y el desarrollo se hizo contra SQLite. Con PostgreSQL
   en producción y múltiples workers, se recomienda añadirlo para eliminar
   una condición de carrera entre el `SELECT` y el `UPDATE` del lease
   (documentado con un comentario en `worker/lease_manager.py`).
3. **Resolución de accesos directos (shortcuts) no implementada**: los
   elementos `SHORTCUT` se marcan `BLOCKED` en Planning. La sección 4.1 lo
   permite ("cuando el destino esté dentro del alcance autorizado y no
   genere ciclos"), pero se dejó fuera del MVP por complejidad y riesgo de
   ciclos mal manejados.
4. **Detección de código OBTC no implementada**: `NamingRulesEngine` acepta
   `obtc_code` como parámetro, pero no hay lógica automática que lo extraiga
   de metadatos o de la jerarquía de carpetas — la sección 24, punto 9,
   señala explícitamente que el formato de los códigos OBTC y su asociación
   a cada documento deben verificarse con la organización antes de
   producción. Implementar la extracción automática requiere esa
   definición previa.
5. **`GET /discovery-runs/{id}/pause` y `/resume` devuelven 501**: el
   discovery corre de forma síncrona en este MVP (una llamada, un
   resultado). Pausar/reanudar solo tiene sentido si discovery se ejecuta
   como un proceso de larga duración, lo cual queda fuera de alcance.
6. **`get_checksum` del adaptador FTP siempre devuelve `None`**: la mayoría
   de servidores FTP no exponen una extensión de hash estándar. La
   validación STRONG funciona igual (compara tamaño), pero no compara hash
   remoto salvo que se implemente la extensión específica del servidor real
   usado en producción.
7. **Reanudación de descarga recalcula el SHA-256 completo**: al reanudar
   una descarga parcial (`TempFileStorage.append_stream`), el hash se
   recalcula leyendo el archivo completo en vez de mantener el estado
   incremental del digest a través de reinicios del proceso. Es correcto
   pero no es lo más eficiente para archivos muy grandes con muchas
   interrupciones.
8. **Reprocesar un elemento `COMPLETED`** no está expuesto vía API
   (`POST /migration-items/{id}/reprocess` lo rechaza explícitamente): el
   principio de no-repetición (sección 9.1) hace que esto requiera una
   decisión operativa explícita, no un botón genérico.

## Aspectos que la especificación pide no asumir (sección 24) y siguen abiertos

Antes de producción, definir junto con la organización: si el servidor real
es FTP, FTPS o SFTP; si soporta `REST` y rename atómico; si expone hashes
remotos; sensibilidad a mayúsculas/minúsculas del filesystem destino; límites
reales de longitud de ruta; carpeta raíz exacta de Drive; modo de
autenticación (cuenta de servicio vs OAuth); formato de los códigos OBTC;
catálogo oficial de abreviaturas; formato de exportación de Docs/Sheets/
Slides; y política organizacional para compartir nombres de documentos con
OpenAI.
