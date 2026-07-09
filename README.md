# 🤖 FinanzAsist AI

**Sistema conversacional de finanzas personales potenciado por IA generativa, con persistencia en la nube y analítica en tiempo real.**

FinanzAsist AI transforma un simple mensaje de texto en Telegram —como *"gasté 4500 en el almuerzo"*— en un registro financiero estructurado, auditable y listo para el análisis de negocio. El sistema combina procesamiento de lenguaje natural (Gemini API), persistencia relacional (PostgreSQL sobre Supabase) y visualización ejecutiva (Power BI), habilitando un pipeline de datos completo: **de la conversación al insight**.

---

## 📐 Arquitectura de la Solución

El dato recorre cuatro capas desacopladas, cada una con una responsabilidad única (principio de *separation of concerns*):

```
┌──────────────┐     ┌────────────────┐     ┌──────────────────┐     ┌───────────────┐
│  1. INGESTIÓN │ ──▶ │ 2. PROCESAMIENTO│ ──▶ │  3. PERSISTENCIA  │ ──▶ │ 4. VISUALIZACIÓN│
│  Telegram Bot │     │   Gemini API    │     │ PostgreSQL/Supabase│    │    Power BI     │
└──────────────┘     └────────────────┘     └──────────────────┘     └───────────────┘
```

> 🖼️ **![Arquitectura de la Solución](images/draw_photo1.png)**

### 1. Ingestión — Telegram Bot API
El usuario interactúa mediante lenguaje 100% natural, sin comandos rígidos ni formularios. `python-telegram-bot` gestiona el *polling* de mensajes de forma asincrónica y enruta cada entrada según su tipo (comando o texto libre) hacia el handler correspondiente.

### 2. Procesamiento — Gemini API (NLU / Extracción Estructurada)
Cada mensaje de texto libre se envía al modelo `gemini-2.5-flash` junto con un **system prompt estricto** que fuerza al modelo a devolver una salida determinística en formato `concepto | monto | categoria | tipo`, eliminando ambigüedad, saludos o razonamiento visible (*chain-of-thought leakage*). La `temperature` se fija en `0.1` para maximizar la consistencia del parseo.

### 3. Persistencia — PostgreSQL sobre Supabase
Los datos estructurados se insertan mediante `SQLAlchemy` (con consultas parametrizadas vía `text()`, previniendo SQL Injection) en una tabla `movimientos` alojada en una instancia PostgreSQL administrada por Supabase.

### 4. Visualización — Power BI
Power BI se conecta directamente a la base de datos en la nube, permitiendo un dashboard *self-service* con actualización en tiempo real: totales de ingresos/gastos, evolución temporal del flujo de caja y desglose por categoría.

---

## 🔍 Análisis de Código Detallado

### 🧠 Extracción de datos con Gemini API

El núcleo de inteligencia del sistema reside en `procesar_gasto`. Se define un `system_instruction` que actúa como contrato de salida (*output contract*) con el modelo, garantizando un formato parseable sin post-procesamiento complejo:

```python
system_instruction = (
    "Sos un asistente experto en finanzas personales. Tu unica tarea es extraer los datos del"
    "mensaje del usuario y devolverlos exactamente en este formato de texto plano separado por barras"
    "sin introducciones, sin saludos y sin formato markdown:\n"
    "SÉ ESTRICTO. NO agregues introducciones, NO expliques tu razonamiento, NO agregues etiquetas como 'THOUGHT:'.\n"
    "concepto | monto | categoria | tipo\n\n"
    "Reglas:\n"
    "1. monto: Debe ser solo el numero entero, sin signos de pesos ni letras\n"
    "2. categoria: Clasifica el movimiento en una de estas categorias: Comida, Transporte, Servicios, "
    "Entretenimiento, Salud, Sueldo, Otros\n"
    "3. tipo: Debe ser estrictamente la palabra 'gasto' o 'ingreso'.\n\n"
    "Ejemplo si el usuario dice: 'Ayer gaste 2500 pesos en cargar la sube'\n"
    "Carga Sube | 2500 | Transporte | gasto"
)

response = ai_client.models.generate_content(
    model='gemini-2.5-flash',
    contents=user_text,
    config=types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.1  # Temperatura baja para maximizar precisión y consistencia
    )
)
```

**Puntos técnicos clave:**
- **Parsing defensivo:** la respuesta se separa con `split("|")` y se valida que existan al menos 4 partes antes de desestructurar, evitando `IndexError` ante respuestas mal formadas del LLM.
- **Sanitización del monto:** se filtran caracteres no numéricos (`c.isdigit() or c == '.'`) antes de castear a `float`, protegiendo contra alucinaciones de formato (ej. `$4.500,00`).
- **Manejo de errores end-to-end:** todo el flujo está envuelto en un `try/except` que devuelve un mensaje amigable al usuario en caso de fallo de la IA o de la base de datos, sin exponer detalles internos (*fail gracefully*).

### 🗄️ Conexión y escritura a la base de datos

La conexión se inicializa una única vez a nivel de módulo, reutilizando el `engine` de SQLAlchemy en todas las funciones (patrón *connection pooling*):

```python
engine = create_engine(DATABASE_URL)
```

La inserción utiliza **consultas parametrizadas**, la práctica estándar para prevenir inyección SQL:

```python
query = text("""
    INSERT INTO movimientos (user_id, concepto, categoria, monto, tipo, created_at)
    VALUES (:user_id, :concepto, :categoria, :monto, :tipo, :created_at)
""")

with engine.connect() as connection:
    connection.execute(query, {
        "user_id": user_id,
        "concepto": concepto,
        "categoria": categoria,
        "monto": monto,
        "tipo": tipo,
        "created_at": datetime.now(timezone.utc)
    })
    connection.commit()
```

### 📊 Consultas analíticas (agregación en el motor de datos)

Los comandos `/balance` y `/categorias` delegan la agregación a PostgreSQL en lugar de traer filas crudas y calcular en Python — una decisión de diseño correcta que minimiza el I/O de red y aprovecha los índices del motor:

```python
query = text("""
    SELECT categoria, SUM(monto) as total
    FROM movimientos
    WHERE user_id = :user_id AND LOWER(tipo) = 'gasto'
    GROUP BY categoria
    ORDER BY total DESC
""")
```

### ⏰ Reportes automáticos con APScheduler

`enviar_resumen_automatico` es invocada de forma **proactiva** (sin interacción del usuario) mediante `AsyncIOScheduler`, que dispara *jobs* tipo `cron`:

```python
scheduler = AsyncIOScheduler(timezone=timezone.utc)

scheduler.add_job(
    enviar_resumen_automatico,
    'cron',
    day_of_week='sun',
    hour=21,
    minute=0,
    args=[app.bot, Mi_chat_id, "semanal"]
)
```

> ⚠️ **Nota de arquitectura:** el `chat_id` destino está *hardcodeado* (`Mi_chat_id = 6167650305`) en lugar de leerse dinámicamente desde la tabla de usuarios. Esto funciona para un despliegue mono-usuario, pero es un punto de mejora prioritario para escalar a multi-tenant (ver [Roadmap](#-roadmap--próximos-pasos)).

---

## 🗃️ Modelo de Datos

Basado en las columnas referenciadas en las queries de `bot.py`, el esquema de la tabla `movimientos` es el siguiente:

```sql
CREATE TABLE movimientos (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    concepto    TEXT NOT NULL,
    categoria   TEXT NOT NULL,
    monto       NUMERIC(12, 2) NOT NULL,
    tipo        TEXT NOT NULL CHECK (tipo IN ('gasto', 'ingreso')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Índices recomendados para acelerar las consultas analíticas por usuario y rango de fechas
CREATE INDEX idx_movimientos_user_id ON movimientos (user_id);
CREATE INDEX idx_movimientos_created_at ON movimientos (created_at);
```

| Columna      | Tipo             | Descripción                                              |
|--------------|------------------|-----------------------------------------------------------|
| `id`         | `BIGSERIAL`      | Identificador único autoincremental del movimiento.       |
| `user_id`    | `BIGINT`         | ID de usuario de Telegram (`from_user.id`).                |
| `concepto`   | `TEXT`           | Descripción libre extraída por Gemini (ej. "Carga Sube").  |
| `categoria`  | `TEXT`           | Comida, Transporte, Servicios, Entretenimiento, Salud, Sueldo, Otros. |
| `monto`      | `NUMERIC(12,2)`  | Valor numérico del movimiento.                             |
| `tipo`       | `TEXT`           | `'gasto'` o `'ingreso'`.                                   |
| `created_at` | `TIMESTAMPTZ`    | Timestamp UTC de creación del registro.                    |

---

## 📈 Métricas Analíticas (DAX — Power BI)

![Dashboard Ejecutivo Interactivo - FinanzAsist AI](images/dashboard.png)

El dashboard modela las métricas de negocio mediante **DAX (Data Analysis Expressions)** sobre la tabla `movimientos`, calculando medidas agregadas en tiempo de consulta en lugar de pre-calcularlas en la capa de aplicación. Esto permite que el usuario final filtre por fecha, categoría o usuario sin degradar el rendimiento del pipeline transaccional.

**Medida — Balance Neto:**

```dax
Balance Neto = 
CALCULATE(SUM('public movimientos'[monto]), 'public movimientos'[tipo] = "ingreso") 
- 
CALCULATE(SUM('public movimientos'[monto]), 'public movimientos'[tipo] = "gasto")
```

Esta medida utiliza `CALCULATE` para modificar el contexto de filtro sobre la columna `tipo`, aislando ingresos y gastos en dos sub-cálculos independientes antes de obtener el neto — el patrón estándar en Power BI para KPIs de flujo de caja.

---

## ⚙️ Guía de Instalación y Despliegue

### 1. Clonar el repositorio y crear el entorno virtual

```bash
git clone https://github.com/tu-usuario/finanzasist-ai.git
cd finanzasist-ai

python -m venv venv

# Activar el entorno virtual
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

### 2. Instalar dependencias

```bash
pip install python-telegram-bot apscheduler python-dotenv google-genai sqlalchemy psycopg2-binary
```

> 💡 Se recomienda congelar las versiones exactas una vez validado el entorno: `pip freeze > requirements.txt`

### 3. Configurar variables de entorno

Crear un archivo `.env` en la raíz del proyecto (⚠️ **nunca** subir este archivo al control de versiones — debe estar en `.gitignore`):

```env
TELEGRAM_TOKEN=tu_token_de_botfather
DATABASE_URL=postgresql://usuario:password@host:puerto/postgres
GEMINI_API_KEY=tu_api_key_de_google_ai_studio
```

### 4. Validar la conexión a la base de datos

```bash
python test_db.py
```

Salida esperada:

```
=========================================
🚀 CONEXION EXITOSA A SUPABASE (SQL)! 🚀
Hora del servidor en la nube: 2026-07-03 ...
=========================================
```

### 5. Ejecutar el bot

```bash
python bot.py
```

---

## 🗺️ Roadmap / Próximos pasos

- [x] **Multi-usuario dinámico:** reemplazar el `chat_id` hardcodeado en `main()` por una tabla `usuarios` con suscripción opt-in a reportes automáticos.
- [x] **Edición y borrado de movimientos:** comando `/deshacer` para revertir el último registro cargado por error.
- [x] **Presupuestos por categoría:** alertas proactivas cuando un usuario supera un límite mensual definido.
- [x] **Soporte multi-moneda:** normalización de montos para usuarios fuera de Argentina.
- [x] **Tests automatizados:** cobertura con `pytest` para el parser de Gemini y las queries SQL (actualmente solo existe `test_db.py` como *smoke test* manual).
- [x] **CI/CD:** pipeline de despliegue continuo (GitHub Actions) hacia un entorno productivo (Railway / Render / Fly.io).
- [x] **Logging estructurado:** reemplazar los `print()` por `logging` con niveles y rotación de archivos.
- [x] **Migraciones versionadas:** introducir Alembic para el control de esquema de la tabla `movimientos`.

---

## 🗺️ Roadmap Senior / Próxima fase

### ⚙️ Arquitectura y Concurrencia
- [x] **Migrar de threading a FastAPI/ASGI:** reemplazar el thread paralelo de healthcheck por un servidor ASGI real corriendo en el mismo proceso, con endpoint `/health` nativo.
- [] **Webhooks en vez de polling:** migrar `python-telegram-bot` de modo polling a webhook, reduciendo latencia y costo de cómputo en Render.
- [x] **Desacoplar el scheduler del bot:** correr APScheduler (o migrar a un worker con colas) en un servicio separado, para que un caído no tumbe al otro.

### 🗄️ Base de Datos
- [ ] **Connection pooling explícito:** configurar `pool_size`, `max_overflow` y `pool_pre_ping=True` en el `engine` para evitar conexiones muertas por timeout de Supabase.
- [ ] **Índice compuesto `(user_id, created_at)`:** optimizar las queries de `/balance` y `/categorias`, que filtran por ambas columnas simultáneamente.
- [ ] **Migrar a `Session` de SQLAlchemy:** reemplazar `engine.connect()` crudo por manejo de sesiones con rollback transaccional automático.
- [ ] **Particionamiento por fecha:** evaluar particionar `movimientos` por mes a medida que crece el volumen de filas.

### 🧩 Patrones de Diseño
- [ ] **Repository Pattern:** extraer toda la lógica SQL a una capa `repositories/`, desacoplada de los handlers de Telegram.
- [ ] **Strategy Pattern para el proveedor de IA:** encapsular la llamada a Gemini detrás de una interfaz común, para poder intercambiar modelos sin tocar los handlers.
- [ ] **Cola de mensajes (Redis + worker):** desacoplar la recepción del mensaje del procesamiento con IA/DB, evitando bloqueos ante picos de tráfico.

### 🛡️ Resiliencia
- [ ] **Retries con backoff exponencial** (`tenacity`) en las llamadas a Gemini y a la base de datos.
- [ ] **Circuit breaker para Gemini:** cortar llamadas ante fallos repetidos en vez de reintentar en loop.
- [ ] **Idempotencia en reportes automáticos:** registrar envíos ya realizados para evitar duplicados ante reinicios del proceso.

### 📊 Observabilidad
- [ ] **Métricas estructuradas:** volumen de mensajes/hora, tasa de error de parseo de Gemini, latencia p95 de la IA.
- [ ] **Logging con correlación:** incluir `user_id` y `request_id` en cada log del ciclo de vida de un mensaje.
- [ ] **Alertas sobre el propio sistema:** notificación proactiva si la tasa de error de Gemini supera un umbral.

### 🔐 Seguridad
- [ ] **Rate limiting por usuario:** prevenir abuso de cuota de Gemini o saturación de la DB.
- [ ] **Validación estricta del esquema de salida de la IA:** `Enum` para `categoria` y `tipo`, evitando datos sucios en la tabla.
- [ ] **Rotación periódica de secrets:** confirmar uso de variables de entorno nativas de Render y establecer rotación de la API key de Gemini.

### 🧪 Testing
- [ ] **Tests de contrato para el parser de Gemini:** casos borde (montos con texto, mezcla de idiomas) con la API mockeada.
- [ ] **Tests de integración con DB efímera:** validar migraciones de Alembic y queries reales vía `testcontainers` o SQLite en memoria.

<p align="center"><sub>Construido con 🧉 usando Python, Gemini API, Supabase y Power BI.</sub></p>
