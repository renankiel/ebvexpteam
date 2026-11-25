import os
import requests
from requests.auth import HTTPBasicAuth

# Carrega configurações do ServiceNow do .env
SNOW_INSTANCE = os.getenv("SNOW_INSTANCE")  # ex: dev12345
SNOW_USER = os.getenv("SNOW_USER")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD")

def create_servicenow_change_request(payload_data):
    """
    Cria uma requisição de Change no ServiceNow baseado no payload fornecido.
    """
    if not SNOW_INSTANCE or not SNOW_USER or not SNOW_PASSWORD:
        raise RuntimeError("Credenciais do ServiceNow não configuradas no ambiente.")

    url = f"https://{SNOW_INSTANCE}.service-now.com/api/now/table/change_request"
    auth = HTTPBasicAuth(SNOW_USER, SNOW_PASSWORD)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(url, auth=auth, headers=headers, json=payload_data, timeout=15)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Erro ao criar Change no ServiceNow: {resp.status_code} - {resp.text}")
        
        return resp.json().get("result", {})
    except Exception as e:
        print(f"⚠️ Erro de integração com ServiceNow: {e}")
        raise e