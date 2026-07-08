import logging
from database import engine, text

#Funcion para enviar resumenes automaticos desacoplada
async def enviar_resumen_automatico(bot, chat_id, tipo_reporte="semanal"):
    # definimos los filtros segun el reporte de sql
    if tipo_reporte == "semanal":
        sql_filter = "created at >= NOW() - INTERVAL '7 days'"
        titulo = "🗓️ *REPORTE ANALÍTICO MENSUAL*"
    else:
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
            f"-----------------------------------------\n"
            f"{emoji_balance} *Balance del período:* ${neto:,.2f}\n\n"
            f"_Este es un reporte automático generado por tu Pipeline de datos._"
        )

        # enviamos el mensaje pero de forma proactiva
        await bot.send_message(chat_id=chat_id, text=reporte, parse_mode="Markdown")
        logging.info(f"🚀 Reporte automático ({tipo_reporte}) enviado con éxito al ID {chat_id}")

    except Exception as e:
        logging.error(f"❌ Error al enviar reporte automático {tipo_reporte}: {e}", exc_info=True)