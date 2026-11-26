import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_ENV") == "development" or os.getenv("FLASK_DEBUG") == "1"

    print("🚀 Iniciando o Servidor ebvexpteam em modo modular...")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
