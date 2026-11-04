# connector-td-engine

Conector de sincronización de datos para Atalaya Mining. Extrae medias horarias ponderadas por tiempo (TWA) de TDengine y las sincroniza a SQL Server, con dashboard web embebido.

## Propósito

Cada ciclo de sincronización:
1. Lee la lista de tags de `config/tags.txt` (212 tags industriales)
2. Consulta TDengine via REST API: `TWA(val)` por tag, en ventana horaria configurable
3. Hace upsert en `dbo.EicTagsHourlyAvg` de SQL Server (staging table + MERGE)

Diseño idempotente: recalcula la ventana completa cada ciclo, sin watermark.

## Stack

- **Python 3.12** (no frameworks pesados, 8 dependencias)
- **Flask + Waitress** — dashboard web en el mismo proceso
- **pyodbc + Microsoft ODBC Driver 18** — SQL Server (no FreeTDS, incompatible con este servidor)
- **requests + tenacity** — TDengine REST API con reintentos (5 intentos, backoff exponencial)
- **croniter** — scheduling con cron/interval/horas específicas, zona Europe/Madrid
- **Docker** — despliegue, logs a stdout, healthcheck vía `src/healthcheck.py`

## Estructura

```
src/
  main.py            # bucle principal, señales SIGTERM/SIGINT
  config.py          # carga de .env
  tdengine_client.py # cliente REST de TDengine
  sqlserver_client.py# upsert con staging table + MERGE
  webapp.py          # dashboard Flask (~1000 líneas, HTML/CSS/JS inline sin CDN)
  monitoring.py      # RunTracker, LogBufferHandler (in-memory, thread-safe)
  schedule_config.py # cron/interval/times + persistencia en state/schedule.json
  date_config.py     # ventana de consulta (shift_yesterday, turno_diario, range)
  errors.py          # mapeo excepciones → mensajes accionables para el usuario
  logging_conf.py    # formato ISO8601, stdout, waitress/werkzeug a WARNING
  healthcheck.py     # healthcheck adaptativo (adapta el margen al intervalo de cron)
config/
  tags.txt           # lista de tags, uno por línea
sql/
  init_schema.sql    # DDL de EicTagsHourlyAvg (ejecutar una vez en SQL Server)
```

## Esquema de SQL Server

Tabla única `dbo.EicTagsHourly`:

| Columna     | Tipo         | Descripción                                       |
|-------------|--------------|---------------------------------------------------|
| TagName     | VARCHAR(128) | Nombre del tag industrial                         |
| WindowStart | DATETIME2(0) | Inicio de la ventana horaria (UTC, sin fracción)  |
| AggType     | VARCHAR(4)   | `'twa'` = media ponderada / `'last'` = horómetro |
| WindowEnd   | DATETIME2(0) | Calculada: WindowStart + 1 hora (persistida)      |
| Value       | FLOAT        | Valor de la agregación                            |
| SyncedAt    | DATETIME2(3) | Timestamp UTC de la última sincronización         |

- PK: `(TagName, WindowStart, AggType)` — permite coexistir TWA y LAST del mismo tag en la misma hora
- Índice: `(WindowStart, WindowEnd, AggType) INCLUDE (TagName, Value)` para Power BI
- DDL en `sql/init_schema.sql` — ejecutar manualmente antes del primer arranque

## Estado del proyecto

**Producción / maduro.** No hay TODOs ni código incompleto. Desplegado activamente con credenciales reales en `.env`.

Dos detalles menores (no bloqueantes):
- `TD_TBNAME_PATTERN` se carga en `config.py` pero la consulta TDengine no la usa (dead config)
- Modo `turno_diario` en `date_config.py` existe en la lógica pero no está expuesto en la validación del API endpoint `/api/date-range`

## Entorno de despliegue

- TDengine: `192.168.150.231:6041` (REST API)
- SQL Server: `172.16.1.55:1457` (ODBC Driver 18, `encrypt=no`)
- Dashboard web: `http://localhost:8080`
- Estado persistente (volumen Docker): `/app/state/` → `schedule.json`, `date_range.json`, `heartbeat`

## Comandos habituales

```bash
# Arrancar
docker compose up -d

# Ver logs en vivo
docker compose logs -f

# Ejecutar solo una vez (para pruebas)
RUN_ONCE=true docker compose run --rm td-connector

# Inicializar schema en SQL Server (solo primera vez)
# Ejecutar sql/init_schema.sql contra la BD destino
```

## Notas de arquitectura

- **Zona horaria**: toda la lógica de scheduling en Europe/Madrid (ZoneInfo); conversiones a UTC explícitas antes de enviar a TDengine
- **Thread safety**: RunTracker, LogBufferHandler, ScheduleStore, DateRangeStore usan locks
- **UI sin dependencias externas**: HTML/CSS/JS embebido en `webapp.py`, funciona offline
- **Credenciales**: contraseña del dashboard se auto-genera en primer arranque si no se configura en `.env`; throttle por IP (5 fallos → bloqueo 60s)
- **No amender estado compartido**: SQL Server es la fuente de verdad duradera; en memoria solo el buffer de últimas 200 ejecuciones y 1000 líneas de log
