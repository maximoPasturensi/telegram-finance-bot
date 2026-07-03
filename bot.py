import os
import asyncio
from datetime import datetime, timezone, timedelta, date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types
from sqlalchemy import create_engine, text

# Vamos a cargar las variables del entorno
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Iniciamos SQLAlchemy y el cliente del gemiini
engine = create_engine(DATABASE_URL)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "👋 ¡Hola! Soy tu asistente de finanzas personales.\n\n"
        "Anota tus gastos o ingresos de forma natural. Por ejemplo:\n"
        "✍️ *Gasté 4500 en almuerzo* o *Cobré 80000 de sueldo*\n\n"
        "Yo me encargo de procesarlo y guardarlo en tu base de datos."
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")


# Procesamos el mensaje con IA
async def procesar_gasto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.message.from_user.id

    # Enviamos un indicador de que el bot esta escribiendo, mientras gemini piensa
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Prompt del sistema para obligar a gemini a devolver un solo formato con estructura
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
        print(f"🤖IA decodifico: {ia_result}") # Vemos en la consola que proceso

        # Separamos el texto usando barras
        parts = [p.strip() for p in ia_result.split("|")]
        if len(parts) < 4:
            raise ValueError(" La IA no contiene las 4 partes: {ia_result}")
        
        concepto, monto_str, categoria, tipo = parts[:4]

        # Limpiamos el monto
        monto_str = "".join(c for c in monto_str if c.isdigit() or c == '.')
        monto = float(monto_str)

        # Insertamos los datos en supabase usando SQL Alchemy
        query= text("""
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
            connection.commit() # Confiramos que se guarde en la base de datos

        # Respondemos con exito al usuario en Telegram
        icono = "🔻" if tipo.lower() == "gasto" else "🔹"
        mensaje_ok = (
            f"✅ *¡Movimiento anotado!*\n\n"
            f"{icono} *Tipo:* {tipo.capitalize()}\n"
            f"📝 *Concepto:* {concepto}\n"
            f"💰 *Monto:* ${monto:,.2f}\n"
            f"🗂️ *Categoría:* {categoria}"
        )
        await update.message.reply_text(mensaje_ok, parse_mode="Markdown")
    
    except Exception as e:
        print(f"❌ Error general: {e}")
        await update.message.reply_text(
            "😅 Che, no entendí bien el movimiento o hubo un problema al guardarlo."
            "¿Me lo podrás repetir de otra forma? Ej: 'Gasto 3000 en verdulería'"
        )

# Comando /balance para el analisis de datos
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Consulta analitica en SQL: Aca vamos a agrupar y sumamos por el tipo para el usuario actual
    query = text("""
        SELECT tipo, SUM(monto) as total
        FROM movimientos
        WHERE user_id = :user_id
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
        print(f"❌ Error al generar balance: {e}")
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
        print(f"❌ Error al generar reporte por categorías: {e}")
        await update.message.reply_text("💥 Error al conectar con la base de datos para clasificar tus gastos.")

#Funcion para envias resumenes automaticos
async def enviar_resumen_automatico(bot, chat_id, tipo_reporte="semanal"):
    #definimos los filtros segun el tipo de reporte en sql
    if tipo_reporte == "semanal":
        #filtro 7 dias
        sql_filter = "created_at >= NOW() - INTERVAL '7 days'"
        titulo = "🗓️ *REPORTE ANALÍTICO SEMANAL*"
    else:
        #filtro primer dia del mes
        sql_filter = "created_at >= DATE_TRUNC('month', NOW())"
        titulo = "🗓️ *REPORTE ANALÍTICO MENSUAL*"

    query = text(f"""
        SELECT tipo, SUM(monto) as total
        FROM movimientos
        WHERE user_id = :user_id AND {sql_filter}
        GROUP BY tipo
    """)

    try:
        with engine.connect() as connection:
            result = connection.execute(query, dict(user_id=int(chat_id))).fetchall()

        totales = {"ingreso": 0.0, "gasto": 0.0}
        for fila in result:
            totales[fila[0].lower()] = float(fila[1])

        ingresos = totales["ingreso"]
        gastos = totales["gasto"]
        neto = ingresos - gastos
        emoji_balance = "💰" if neto >= 0 else "⚠️"

        reporte = (
            f"{titulo}\n"
            f"--- Automatic Summary ---\n\n"
            f"🔹 *Ingresos en el período:* ${ingresos:,.2f}\n"
            f"🔻 *Gastos en el período:* ${gastos:,.2f}\n"
            f"---"
            f"{emoji_balance} *Balance del período:* ${neto:,.2f}\n\n"
            f"_Este es un reporte automático generado por tu Pipeline de datos._"
        )

        #Enviamos el mensaje de forma PROACTIVA (osea sin que el usuario lo pida)
        await bot.send_message(chat_id=chat_id, text=reporte, parse_mode="Markdown")
        print(f"🚀 Reporte automático ({tipo_reporte}) enviado con éxito al ID {chat_id}")
    
    except Exception as e:
        print(f"❌ Error al enviar reporte automático {tipo_reporte}: {e}")

# funcion principal que arrancamos el bot
async def main():
    if not all([TELEGRAM_TOKEN, DATABASE_URL, GEMINI_API_KEY]):
        print("❌ Error: Faltan variables de entorno en el archivo .env")
        return
    
    print("🚀 Arrancando el bot de finanzas ... Presiona Ctrl+C para detenerlo")

    # Contruimos la aplicacion de telegram
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance)) # handler del balance
    app.add_handler(CommandHandler("categorias", categorias)) # handler de categorias
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_gasto))

    # Configuramos tareas automaticas
    scheduler = AsyncIOScheduler(timezone=timezone.utc)

    # remplazamos aca el id de telegram
    Mi_chat_id = 6167650305

    # Este es el programador semanal
    scheduler.add_job(
        enviar_resumen_automatico,
        'cron',
        day_of_week='sun',
        hour=21,
        minute=0,
        args=[app.bot, Mi_chat_id, "semanal"]
    )

    # el programador mensual
    scheduler.add_job(
        enviar_resumen_automatico,
        'cron',
        day=1,
        hour=9,
        minute=0,
        args=[app.bot, Mi_chat_id, "mensual"]
    )

    #El reloj de fondo
    scheduler.start()

        # Empezamos el motodo de polling (escucha de mensajes)
    await app.initialize()
    await app.updater.start_polling()
    await app.start()

        # Aca mantenemos el bot corriendo
    import asyncio
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot detenido de forma segura por el usuario. ¡Hasta luego!")
        
        

        