import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Vamos a cargar las variables de entorno desde el archivo .env
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ Error: No se encontro la variable DATABASE_URL en el archivo .env")
else:
    try:
        # Creamos la conexion con la base de datos
        engine = create_engine(DATABASE_URL)

        # Aca hacemos una consulta SQL de prueba
        with engine.connect() as connection:
            result = connection.execute(text("SELECT NOW();")) 
            print("=========================================")
            print("🚀 CONEXION EXITOSA A SUPABASE (SQL)! 🚀")
            print(f"Hora del servidor en la nube: {result.fetchone()[0]}")
            print("=========================================")

    except Exception as e:
        print("❌ Error al conectar con la base de datos:")
        print(e)