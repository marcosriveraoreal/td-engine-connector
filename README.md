# connector-td-engine

Conector ligero en Python que calcula medias horarias de un conjunto de tags
de TDengine (vía su API REST `/rest/sql`) y las sincroniza en SQL Server,
empaquetado como un único servicio de `docker-compose`.

## Qué hace

En cada ciclo:

1. Lee la lista de tags a sincronizar de [config/tags.txt](config/tags.txt) (uno por línea).
2. Pide a TDengine, para esos tags, la media horaria del valor dentro de una
   ventana relativa al día actual (`TODAY()`, medianoche de hoy en TDengine):
   ```sql
   SELECT tagname, _wstart, AVG(val) AS avg_val
   FROM `eic_data`.`eic_tags_float`
   WHERE tagname IN ('TAG1', 'TAG2', ...)
     AND ts >= TODAY() - 15h AND ts < TODAY() + 9h
   PARTITION BY tagname
   INTERVAL(1h)
   ```
3. Hace upsert (`MERGE`) del resultado en `dbo.EicTagsHourlyAvg`, una fila por
   `(TagName, WindowStart)`.
4. Repite según la planificación configurada (ver [Panel de monitorización](#panel-de-monitorización-dashboard-web)):
   por defecto la expresión cron de `SCHEDULE_CRON` (5 campos, evaluada en
   **hora de Madrid**), por ejemplo `*/5 * * * *` (cada 5 min) o `0 6 * * *`
   (una vez al día a las 06:00). Se puede cambiar en caliente, sin reiniciar
   el contenedor, desde la pestaña "Configuración" del panel web — o lanzar
   una sincronización puntual en cualquier momento con "Ejecutar ahora".

Como cada ciclo recalcula toda la ventana del día y hace upsert, el proceso
es idempotente y no necesita ningún watermark: si el contenedor se reinicia,
simplemente recalcula la ventana actual en el siguiente ciclo. Esto también
significa que las horas ya calculadas se van refrescando mientras lleguen
más datos para ellas dentro de la ventana.

## Puesta en marcha

1. Crear el esquema de destino en SQL Server (una sola vez):
   ```bash
   sqlcmd -S <host> -d <database> -U <user> -P <password> -i sql/init_schema.sql
   ```
2. Ajustar [config/tags.txt](config/tags.txt) con los tags que se quieran sincronizar.
3. Copiar y rellenar la configuración:
   ```bash
   cp .env.example .env
   ```
   Editar `.env` con las credenciales reales de TDengine y SQL Server.
4. Levantar el contenedor:
   ```bash
   docker compose up -d --build
   ```
5. Ver logs:
   ```bash
   docker compose logs -f td-connector
   ```
6. Abrir el panel de monitorización: `http://<host>:8080` (ver siguiente sección).

## Panel de monitorización (dashboard web)

El propio contenedor sirve, en el puerto `8080`, un panel con la identidad visual de
`COMPANY_NAME` para responder de un vistazo a "¿está cargando datos?, ¿va bien?, ¿se está
consumiendo todo lo que debería?":

- **Estado del último ciclo y próxima ejecución**: OK / con avisos / error, filas escritas,
  cuenta atrás hasta la siguiente sincronización — todas las horas en **hora de Madrid**.
- **Historial de ejecuciones**: últimos ciclos (hasta `WEB_UI_MAX_RUNS_HISTORY`) con origen
  (arranque / programado / manual), duración, filas procesadas, tags recibidos vs. esperados,
  qué tags concretos no llegaron y, si el ciclo falló, **el motivo en lenguaje llano** (p.ej.
  "No se pudo conectar con SQL Server — verifica MSSQL_HOST, MSSQL_PORT…") además del detalle
  técnico original.
- **Frescura de datos por tag**: para cada tag de [config/tags.txt](config/tags.txt), su último
  valor conocido en `MSSQL_TARGET_TABLE` y hace cuánto se actualizó — para detectar tags que
  se han quedado "parados" aunque el conector en general esté funcionando. Filtro de texto y
  botón "solo con problemas".
- **Logs en vivo**: las últimas líneas de log del proceso (`WEB_UI_MAX_LOG_LINES`), con filtro
  por nivel, pausa y auto-scroll.
- **Ejecución manual**: botón "Ejecutar ahora" (en el panel y en Configuración) para lanzar una
  sincronización inmediata sin esperar a la siguiente hora programada. No disponible en modo
  `RUN_ONCE` (el proceso termina justo tras el ciclo único).
- **Configuración de la planificación** (pestaña "Configuración"): cuándo se ejecuta, editable
  sin reiniciar el contenedor, en tres modos — cada intervalo fijo (minutos), horas concretas
  del día, o una expresión cron avanzada para casos como "solo días laborables". Todas las
  horas se interpretan en **hora de Madrid** (Europe/Madrid), con cambio automático de horario
  de verano/invierno. El cambio se persiste en `SCHEDULE_STATE_FILE` y tiene prioridad sobre
  `SCHEDULE_CRON` en arranques posteriores.

Todo se actualiza solo cada `WEB_UI_REFRESH_SECONDS` segundos (10s por defecto); no hay que
recargar la página.

El historial de ejecuciones y las líneas de log viven en memoria del proceso (se pierden al
reiniciar el contenedor); no son un sustituto del histórico de datos, que sigue viviendo en
`MSSQL_TARGET_TABLE`, ni de `docker compose logs`, que conserva más volumen en disco. La
planificación, en cambio, sí se persiste en disco (`SCHEDULE_STATE_FILE`, dentro del volumen
`td-connector-state`) y sobrevive a los reinicios.

### Acceso y seguridad

El panel exige iniciar sesión con una única cuenta de servicio (`WEB_UI_USER` /
`WEB_UI_PASSWORD`). Si se dejan ambas vacías, se genera un usuario `admin` con una contraseña
aleatoria al arrancar, que se imprime **una vez** en los logs del contenedor
(`docker compose logs td-connector`) y se pierde al reiniciar — para una contraseña estable,
defínelas en `.env` (recomendado en cuanto el puerto 8080 quede accesible fuera de tu propia
máquina). Tras 5 intentos fallidos seguidos desde la misma IP, el panel bloquea nuevos intentos
de inicio de sesión durante 60 segundos.

## Configuración (`.env`)

| Variable | Descripción | Por defecto |
|---|---|---|
| `TD_URL` | Endpoint REST de TDengine | *(obligatorio)* |
| `TD_USER` / `TD_PASSWORD` | Credenciales TDengine | *(obligatorio)* |
| `TD_DATABASE` | Base de datos TDengine | `eic_data` |
| `TD_TABLE` | Tabla/supertabla origen | `eic_tags_float` |
| `TD_TAGS_FILE` | Fichero con la lista de tags, uno por línea | `/app/config/tags.txt` |
| `TD_WINDOW_LOOKBACK_HOURS` | Horas hacia atrás desde `TODAY()` | `15` |
| `TD_WINDOW_LOOKAHEAD_HOURS` | Horas hacia adelante desde `TODAY()` | `9` |
| `TD_AGG_INTERVAL` | Tamaño de la ventana de agregación (sintaxis TDengine) | `1h` |
| `MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_DATABASE` | Conexión SQL Server | *(obligatorio)* |
| `MSSQL_USER` / `MSSQL_PASSWORD` | Credenciales SQL Server | *(obligatorio)* |
| `MSSQL_TARGET_TABLE` | Tabla destino | `dbo.EicTagsHourlyAvg` |
| `MSSQL_ENCRYPT` | `yes`\|`no` — cifrado de la conexión ODBC | `no` |
| `MSSQL_TRUST_SERVER_CERTIFICATE` | `yes`\|`no` | `yes` |
| `SCHEDULE_CRON` | Planificación inicial (cron, **hora de Madrid**); editable luego desde el panel | `*/5 * * * *` |
| `SCHEDULE_STATE_FILE` | Dónde se persiste la planificación editada desde el panel | `/app/state/schedule.json` |
| `RUN_ONCE` | Ejecuta un solo ciclo y termina (sin panel web ni ejecución manual) | `false` |
| `LOG_LEVEL` | Nivel de log | `INFO` |
| `HEALTHCHECK_GRACE_SECONDS` | Margen extra sobre el intervalo cron antes de marcar `unhealthy` | `300` |
| `WEB_UI_ENABLED` | Activa el panel web de monitorización | `true` |
| `WEB_UI_HOST` | Interfaz de escucha del panel | `0.0.0.0` |
| `WEB_UI_PORT` | Puerto del panel | `8080` |
| `WEB_UI_USER` / `WEB_UI_PASSWORD` | Cuenta única del panel; si se dejan vacías, se genera una contraseña temporal (ver arriba) | *(autogenerado)* |
| `WEB_UI_SECRET_KEY` | Clave de firma de sesión; si se deja vacía, se genera al arrancar (las sesiones no sobreviven a un reinicio) | *(autogenerado)* |
| `WEB_UI_MAX_RUNS_HISTORY` | Ciclos retenidos en memoria para el historial | `200` |
| `WEB_UI_MAX_LOG_LINES` | Líneas de log retenidas en memoria para el visor | `1000` |
| `WEB_UI_REFRESH_SECONDS` | Frecuencia de refresco del panel en el navegador | `10` |
| `COMPANY_NAME` | Nombre mostrado en la cabecera, login y distintivo del panel | `Atalaya Mining` |
| `PRODUCT_NAME` | Subtítulo del panel | `connector-td-engine` |
| `BRAND_ACCENT_COLOR` | Color de acento (hex) del panel | `#c8763a` |
| `BRAND_LOGO_URL` | Logo propio: URL http(s), data URI, o nombre de un fichero en `./assets` (se sirve en `/assets/<fichero>`, p.ej. `logo.png`); si se deja vacío, se muestra un distintivo con las iniciales de `COMPANY_NAME` | *(vacío)* |
| `ASSETS_DIR` | Carpeta servida en `/assets/` (contiene el logo) | `/app/assets` |

## Editar la lista de tags

[config/tags.txt](config/tags.txt) se copia dentro de la imagen al construirla, así que
normalmente basta con editar el fichero y hacer `docker compose up -d --build`.
Para poder editarlo sin reconstruir, descomenta el volumen correspondiente en
`docker-compose.yml`:
```yaml
volumes:
  - ./config/tags.txt:/app/config/tags.txt:ro
```

## Permisos mínimos recomendados en SQL Server

El usuario `MSSQL_USER` solo necesita, sobre la base de datos destino:
`SELECT`, `INSERT`, `UPDATE` en `dbo.EicTagsHourlyAvg`, y permiso para crear
tablas temporales (`#staging`), implícito para cualquier usuario con acceso
a `tempdb` en la sesión.

## Notas de diseño

- **Driver SQL Server**: se usa el driver ODBC oficial de Microsoft
  (`msodbcsql18` vía `pyodbc`), no FreeTDS/`pymssql`. Algunos SQL Server solo
  completan correctamente el handshake TDS con el driver oficial de
  Microsoft (igual que el JDBC de `mssql-jdbc`); con FreeTDS se puede recibir
  un error de conexión genérico incluso con credenciales correctas.
- **Sin watermark**: cada ciclo recalcula la ventana completa del día
  (`TODAY() - lookback` a `TODAY() + lookahead`) y hace upsert. No hace falta
  llevar un puntero de progreso porque el volumen de datos (tags × horas del
  día) es pequeño y acotado.
- **Deduplicación defensiva**: si TDengine devolviera dos filas para la misma
  clave `(TagName, WindowStart)` en el mismo lote (por ejemplo, por
  variaciones de espacios en blanco en el `tagname` — SQL Server ignora los
  espacios finales al comparar `VARCHAR`, así que `'X'` y `'X '` chocan en el
  `MERGE`), el conector normaliza (`strip()`) y se queda con la última
  ocurrencia antes de escribir.
- **Ejecución única (`RUN_ONCE=true`)**: útil si se prefiere orquestar la
  periodicidad con un scheduler externo (cron del host, Airflow, Kubernetes
  CronJob) en lugar del bucle interno.
- **Healthcheck adaptativo**: en lugar de un umbral fijo, `src/healthcheck.py`
  calcula cuál debería ser la última ejecución programada (leyendo la misma
  planificación persistida que usa el panel web, en hora de Madrid) y compara
  la antigüedad del heartbeat contra eso, por lo que funciona igual con un
  schedule de "cada 5 minutos" que con uno de "una vez al día".
- **Motivos de fallo legibles**: `src/errors.py` traduce las excepciones más
  comunes (timeouts, fallos de conexión, credenciales rechazadas, errores de
  TDengine o de SQL Server) a una categoría corta y una sugerencia de qué
  revisar, mostradas en el historial del panel junto al detalle técnico
  original.
