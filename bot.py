import os
import logging
import threading
import asyncio
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer
from datetime import datetime, timezone, timedelta, date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types
from sqlalchemy import text
from pydantic import BaseModel, Field
from fastapi import FastAPI, Request, Response, status
from tasks import enviar_resumen_automatico
from database import engine
import uvicorn

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-finance-bot-1-6bbq.onrender.com")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

fastapi_app = FastAPI(title="FinanzAsist API")

app = Application.builder().token(TELEGRAM_TOKEN).build()

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Endpoints de fast api
@fastapi_app.get("/health")
def health_check():
    """Endpoint nativo para que Render sepa que el bot sigue vivo"""
    return {"status": "ok", "bot_active": True}

@fastapi_app.post("/webhook")
async def telegram_webhook(request: Request):
    """Recibe las actualizaciones de Telegram en tiempo real y las procesa."""
    try:
        json_data = await request.json()
        update = Update.de_json(json_data, app.bot)
        await app.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logging.error(f"❌ Error procesando el webhook de Telegram: {e}", exc_info=True)
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
class GastoEstructurado(BaseModel):
    monto: float
    concepto: str
    categoria: str
    tipo: str = Field(description="'gasto' o 'ingreso'")
    moneda: str = Field(description="Codigo ISO 4217 (ej: ARS, USD, EUR). Por defecto ARS")

# configuracion de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] $(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"), # guardamos todo en este archivo
        logging.StreamHandler() # mostramos en consola
    ]
)

# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Da la bienvenida al usuario y lo registra en Supabase si es nuevo."""
    chat_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name

    logging.info (f"👤 Usuario ejecutando /start: {first_name} ({chat_id})")

    # guardamos parametros 
    parametros_usuario = [{"chat_id": int(chat_id), "username": username, "first_name": first_name}]

    #query sql para insertar o no hacer nada
    query_registrar = text("""
        INSERT INTO usuarios (chat_id, username, first_name)
        VALUES (:chat_id, :username, :first_name) 
        ON CONFLICT (chat_id) DO NOTHING;               
    """)

    try:
         #abrimos la conexcion
         with engine.connect() as connection:
             connection.execute(query_registrar, parametros_usuario)
             connection.commit()

         mensaje_bienvenida = (
            f"¡Hola, {first_name}! 👋\n\n"
            "Bienvenido a tu bot de finanzas personales.\n"
            "A partir de ahora, todo lo que anotes acá se guardará en tu cuenta privada.\n\n"
            "✍️ **¿Cómo anotar un gasto?**\n"
            "Simplemente escribí algo como: `gaste 1500 en almuerzo` o `pagué 5000 de internet`."
        )
         await update.message.reply_text(mensaje_bienvenida, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"❌ Error al registrar usuario en /start: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Hubo un problema al iniciar tu cuenta. Por favor, intentá de nuevo más tarde.")

# Procesamos el mensaje con IA
async def procesar_gasto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.effective_user.id

    # Enviamos un indicador de que el bot esta escribiendo, mientras gemini piensa
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Prompt del sistema para obligar a gemini a devolver un solo formato con estructura
    system_instruction = (
        "Sos un asistente experto en finanzas personales. Tu unica tarea es extraer los datos del"
        "mensaje del usuario y devolverlos exactamente en este formato de texto plano separado por barras"
        "sin introducciones, sin saludos y sin formato markdown:\n"
        "SÉ ESTRICTO. NO agregues introducciones, NO expliques tu razonamiento, NO agregues etiquetas como 'THOUGHT:'.\n"
        "concepto | monto | categoria | tipo | moneda\n\n"
        "Reglas:\n"
        "1. monto: Debe ser solo el numero entero o decimal, sin signos de pesos ni letras\n"
        "2. categoria: Clasifica el movimiento en una de estas categorias: Comida, Transporte, Servicios, "
        "Entretenimiento, Salud, Sueldo, Otros\n"
        "3. tipo: Debe ser estrictamente la palabra 'gasto' o 'ingreso'.\n"
        "4. moneda: Debe ser el código ISO de 3 letras en mayúsculas (ej: ARS, USD, EUR, BRL). "
        "Si el usuario no especifica explícitamente la moneda (ej: 'Gasté 3500 en un café'), asumí 'ARS'. "
        "Si dice dólares usa 'USD', si dice euros usa 'EUR'.\n\n"
        "Ejemplo si el usuario dice: 'Ayer gaste 2500 pesos en cargar la sube'\n"
        "Carga Sube | 2500 | Transporte | gasto | ARS\n"
        "Ejemplo si el usuario dice: 'Pagué 15 dólares de Netflix'\n"
        "Netflix | 15 | Entretenimiento | gasto | USD"
    )

    try:
        # Llamamos a la API de Gemini (Usando google-genai)
        response = ai_client.models.generate_content(
            model ='gemini-2.5-flash',
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1 # Temperatura baja para que sea preciso
            )
        )

        ia_result = response.text
        if not ia_result:
            raise ValueError("Gemini devolvio una respuesta vacia")
        
        ia_result = ia_result.strip()  # Limpiamos espacios al inicio y final
        logging.info(f"🤖IA decodifico: {ia_result}") # Vemos en la consola que proceso

        # Separamos el texto usando barras
        parts = [p.strip() for p in ia_result.split("|")]
        if len(parts) < 5:
            raise ValueError(" La IA no contiene las 5 partes: {ia_result}")
        
        concepto, monto_str, categoria, tipo, moneda = parts[:5]

        # Limpiamos el monto
        monto_str = "".join(c for c in monto_str if c.isdigit() or c == '.')
        monto = float(monto_str)

        # Insertamos los datos en supabase usando SQL Alchemy
        query= text("""
            INSERT INTO movimientos (chat_id, concepto, categoria, monto, tipo, moneda, created_at)
            VALUES (:chat_id, :concepto, :categoria, :monto, :tipo, :moneda, :created_at)
        """)

        with engine.connect() as connection:
            connection.execute(query, {
                "chat_id": int(user_id),
                "concepto": concepto,
                "categoria": categoria,
                "monto": monto,
                "tipo": tipo,
                "moneda": moneda,
                "created_at": datetime.now(timezone.utc)
            })
            connection.commit() # Confiramos que se guarde en la base de datos

        # integracion de la alerta del presupuesto
        alerta_mensaje = await verificar_alerta_presupuesto(int(user_id), categoria)

        # Respondemos con exito al usuario en Telegram
        icono = "🔻" if tipo.lower() == "gasto" else "🔹"
        mensaje_ok = (
            f"✅ *¡Movimiento anotado!*\n\n"
            f"{icono} *Tipo:* {tipo.capitalize()}\n"
            f"📝 *Concepto:* {concepto}\n"
            f"💰 *Monto:* {moneda} ${monto:,.2f}\n" # muestra ars, usd, etc.
            f"🗂️ *Categoría:* {categoria}"
        )

        # si la funcion encontro un presupuesto hay aviso, y se lo sumamos abajo
        if alerta_mensaje:
            mensaje_ok += f"\n\n====================\n{alerta_mensaje}"
        
        await update.message.reply_text(mensaje_ok, parse_mode="Markdown")
    
    except Exception as e:
        logging.error(f"❌ Error general: {e}", exc_info=True)
        await update.message.reply_text(
            "😅 Che, no entendí bien el movimiento o hubo un problema al guardarlo."
            "¿Me lo podrás repetir de otra forma? Ej: 'Gasto 3000 en verdulería'"
        )

async def verificar_alerta_presupuesto(chat_id: int, categoria_gasto: str):
    """Compara el total gastado en el mes contra el límite configurado por el usuario."""

    # Query para traer el limite a CIERTA categoria
    query_limite = text("""
        SELECT limite_mensual FROM presupuesto
        WHERE chat_id = :chat_id AND categoria = :categoria
    """)

    #query para sumar todos los gastos de la categoria en el mes actual
    query_suma_mes = text("""
        SELECT COALESCE (SUM(monto), 0) FROM movimientos
        WHERE chat_id = :chat_id
        AND categoria = :categoria
        AND tipo = 'gasto'
        AND EXTRACT(MONTH FROM created_at) = EXTRACT(MONTH FROM NOW())
        AND EXTRACT(YEAR FROM created_at) = EXTRACT(YEAR FROM NOW())
    """)

    try:
        with engine.connect() as connection:
            # buscamos si tiene limite seteado por el usuario
            limite_res = connection.execute(query_limite, {"chat_id": chat_id, "categoria": categoria_gasto}).fetchone()
            if not limite_res:
                return None # No tiene presupuesto configurado
            
            limite = float(limite_res[0])

            # calculamos cuanto viene gastado en el mes
            total_gastado = float(connection.execute(query_suma_mes, {"chat_id": chat_id, "categoria": categoria_gasto}).scalar())

            # si se paso o esta carca , armamos un mensaje aviso
            if total_gastado > limite:
                exceso = total_gastado - limite
                return f"🚨 *¡ALERTA DE PRESUPUESTO!*\n\nTe pasaste del límite mensual de *{categoria_gasto}* por *${exceso:,.2f}*.\n📉 Limite: ${limite:,.2f} | Gastado: ${total_gastado:,.2f}"
            elif total_gastado >= (limite * 0.85): #alertamos previo al 85% del uso
                disponible = limite - total_gastado
                return f"⚠️ *Cuidado! Presupuesto ajustado*\n\nYa consumiste mas del 85% de *{categoria_gasto}*.\nTe quedan solo *${disponible:,.2f}* para el resto del mes."
            
    except Exception as e:
        logging.error(f"❌ Error al verificar presupuesto: {e}", exc_info=True)
    
    return None

async def reportes_comando(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite al usuario desactivar o desactivar los reportes semanales automaticos."""
    user_id = update.effective_user.id

    #query para ver el estado actual del usuario
    query_buscar = text("SELECT suscrito_reportes FROM usuarios WHERE chat_id = :chat_id")

    #query para alternar
    query_update = text("""
        UPDATE usuarios
        SET suscrito_reportes = NOT suscrito_reportes
        WHERE chat_id = :chat_id
        RETURNING suscrito_reportes
    """)

    try:
        with engine.connect() as connection:
            # primero chequeamos si el usuario existe
            user_exists = connection.execute(query_buscar, {"chat_id": int(user_id)}).fetchone()

            if not user_exists:
                await update.message.reply_text("❌ primero tenes que iniciar el bot /start")
                return
            
            # ejecutamos el cambio y guardamos el estado
            result = connection.execute(query_update, {"chat_id": int(user_id)})
            nuevo_estado = result.scalar()
            connection.commit()

        # respondemos segun el estado de la base de datos
        if nuevo_estado:
            mensaje = (
                "🔔 *Reportes activados!*\n\n"
                "A partir de ahora vas a recibir tus resumenes financieros automaticamente. 📊"
            )
        else:
            mensaje = (
                "🔕 *Reportes desactivos!*\n\n"
                "Ya no te enviaremos resumenes semanales automaticos. Podes volver a activarlo cuando uses este mismo comando"
            )

        await update.message.reply_text(mensaje, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"❌ Error en comando /reportes: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Hubo un problema al intentar cambiar tu suscripcion a los reportes")

# Comando /balance para el analisis de datos
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Consulta analitica en SQL: Aca vamos a agrupar y sumamos por el tipo para el usuario actual
    query = text("""
        SELECT tipo, SUM(monto) as total
        FROM movimientos
        WHERE chat_id = :user_id
        GROUP BY tipo
    """)

    try:
        with engine.connect() as connection:
            result = connection.execute(query, {"user_id": user_id}).fetchall()

        #procesamos los resultados de la base de datos
        totales = {"ingreso": 0.0, "gasto": 0.0}
        for fila in result:
            #fila[0] es el tipo (ingreso/gasto) y fila[1] es la suma de monto
            totales[fila[0].lower()] = float(fila[1])

        ingresos =totales["ingreso"]
        gastos = totales["gasto"]
        neto = ingresos - gastos

        # definimos un emoji segun el estado del balance
        emoji_balance = "💰" if neto >= 0 else "⚠️"

        # aca vamos a construir el mensaje para el usuario
        reporte = (
            f"📊 *RESUMEN FINANCIERO ACTUAL*\n"
            f"---"
            f"🔹 *Total Ingresos:* ${ingresos:,.2f}\n"
            f"🔻 *Total Gastos:* ${gastos:,.2f}\n"
            f"---"
            f"{emoji_balance} *Balance Neto:* ${neto:,.2f}"
        )
        await update.message.reply_text(reporte, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"❌ Error al generar balance: {e}", exc_info=True)
        await update.message.reply_text(
            "💥 Hubo un error al conectar con la base de datos para generar tu reporte."
        )

# Aca creamos el /categorias para ver en que se nos va los gastos
async def categorias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Consulta SQL analitica, filtramos los gastos y lo agrupamos por las categorias y ordenamos por mayor am enor
    query = text("""
        SELECT categoria, SUM(monto) as total
        FROM movimientos
        WHERE user_id = :user_id AND LOWER(tipo) = 'gasto'
        GROUP BY categoria
        ORDER BY total DESC
     """)
    
    try:
        with engine.connect() as connection:
            result = connection.execute(query, {"user_id": user_id}).fetchall()

        if not result:
            await update.message.reply_text("📋 Aún no tenés gastos registrados para categorizar.")
            return
        
        # Diccionario de emojis para las analiticas (categorizadas)
        emojis_cat = {
            "comida": "🍔",
            "transporte": "🚗",
            "servicios": "💡",
            "entretenimiento": "🎬",
            "salud": "🏥",
            "otros": "📦"
        }

        # Construimos el cuerpo del reporte
        reporte = "📊 *GASTOS POR CATEGORÍA*\n"
        reporte += "--- (De mayotr a menor) ---\n"

        total_gastos_general = 0.0
        lineas_reporte = []

        # Primero recorremos para calcular el total acumulado de gastos
        for fila in result:
            total_gastos_general += float(fila[1])

        # Armamos el desglose con  el porcertaje de cada categoria
        for fila in result:
            cat_nombre = fila[0]
            cat_total = float(fila[1])

            # Calculamos que porcentaje representa esa categoria SOBRE el total de gastos
            porcentaje = (cat_total / total_gastos_general) * 100 if total_gastos_general > 0 else 0

            emoji = emojis_cat.get(cat_nombre.lower(), "🗂️")
            lineas_reporte.append(f"{emoji} *{cat_nombre.capitalize()}:* ${cat_total:,.2f} _({porcentaje:.1f}%)_")

        reporte += "\n".join(lineas_reporte)
        reporte +=f"\n\n💳 *Suma Total de Gastos:* ${total_gastos_general:,.2f}"

        await update.message.reply_text(reporte, parse_mode="Markdown")
    
    except Exception as e:
        logging.error(f"❌ Error al generar reporte por categorías: {e}", exc_info=True)
        await update.message.reply_text("💥 Error al conectar con la base de datos para clasificar tus gastos.")

async def deshacer_comando(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # nos aseguramos d el id del usuario
    parametros_usuario = [{"chat_id": int(user_id)}]

    # 1 query para buscar el ultimo movimiento de este usuario especifico
    query_buscar = text("""
        SELECT id, concepto, monto, tipo
        FROM movimientos
        WHERE chat_id = :chat_id
        ORDER BY created_at DESC
        LIMIT 1
    """)

    try:
        with engine.connect() as connection:
            result = connection.execute(query_buscar, parametros_usuario).fetchone()

            # Aca si el usuario no tiene movimientos
            if not result:
                await update.message.reply_text("❌ No encontré ningún movimiento reciente para deshacer.")
                return
            
            # Si lo encuentra, extraemos los datos al usuario
            id_registro, concepto, monto, tipo = result

            # query para elminar el registro unico por su ID
            parametros_eliminar = [{"id": int(id_registro)}]
            query_eliminar = text("DELETE FROM movimientos WHERE id = :id")

            connection.execute(query_eliminar, parametros_eliminar)
            connection.commit()

            #Mensaje de confirmacion
            await update.message.reply_text(
                f"🗑️ *¡Movimiento eliminado con éxito!*\n\n"
                f"🔹 *Concepto:* {concepto}\n"
                f"🔹 *Monto:* ${monto:.2f}\n"
                f"🔹 *Tipo:* {tipo.capitalize()}",
                parse_mode="Markdown"
            )

    except Exception as e:
        logging.error(f"Error en comando /deshacer: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Ocurrió un error al intentar borrar el último registro.")

# funcion principal que arrancamos el bot
async def main():
    #lineas de prueba
    logging.info(f"🔍 Probando Token en Render: '{TELEGRAM_TOKEN[:10]}...'")
    logging.info(f"🔍 Probando URL DB en Render: '{DATABASE_URL[:15]}...'")

    if not all([TELEGRAM_TOKEN, DATABASE_URL, GEMINI_API_KEY]):
        logging.error("❌ Error: Faltan variables de entorno en el archivo .env")
        return

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance)) # handler del balance
    app.add_handler(CommandHandler("reportes", reportes_comando)) # handler de reportes
    app.add_handler(CommandHandler("categorias", categorias)) # handler de categorias
    app.add_handler(CommandHandler("deshacer", deshacer_comando)) # handler de deshacer
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_gasto))

    # Configuramos tareas automaticas
    scheduler = AsyncIOScheduler(timezone=timezone.utc)

    # remplazamos aca el id de telegram
    Mi_chat_id = 6167650305

    scheduler.add_job(enviar_resumen_automatico, 'cron', day_of_week='sun', hour=21, minute=0, args=[app.bot, Mi_chat_id, "semanal"])
    scheduler.add_job(enviar_resumen_automatico, 'cron', day=1, hour=9, minute=0, args=[app.bot, Mi_chat_id, "mensual"])

    # Servidor ASGI (fastAPI) + configuracion del bot
    # configuramos uvicorn para que maneje la app de fastapi
    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        loop="asyncio"
    )
    server = uvicorn.Server(config)

    # creamos una tarea en segundo plano para el server
    asyncio.create_task(server.serve())

    # arrancamos el planificador automatico de reportes
    scheduler.start()

    # iniciamos el bot de telegram de forma asincronica
    await app.initialize()
    await app.start()

    weebhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    await app.bot.set_webhook(url=weebhook_url)

    logging.info("🕸️ Webhook configurado con éxito en: {webhook_url}")

    #mantenemos el proceso vivo mientras la fast api no se detenga
    while not server.should_exit:
        await asyncio.sleep(1)

    # cierre limpio 
    await app.bot.delete_webhook()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\n🛑 Bot detenido de forma segura por el usuario. ¡Hasta luego!")
        
        

        