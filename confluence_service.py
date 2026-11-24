# confluence_service.py
import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

# Carrega chaves corretas mapeadas do seu .env
CONFLUENCE_BASE_URL = os.getenv("JIRA_URL", "").rstrip('/') + "/wiki"
CONFLUENCE_USER_EMAIL = os.getenv("JIRA_EMAIL")
CONFLUENCE_API_TOKEN = os.getenv("JIRA_TOKEN")
CONFLUENCE_TEMPLATE_ID = os.getenv("CONFLUENCE_TEMPLATE_ID", "491523")

_TEMPLATE_CACHE = {}

def get_confluence_template(template_id=None, force_refresh=False):
    """
    Busca o template direto da API do Confluence Cloud e armazena em cache.
    """
    global _TEMPLATE_CACHE
    tid = str(template_id or CONFLUENCE_TEMPLATE_ID)

    if not force_refresh and tid in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[tid]

    url = f"{CONFLUENCE_BASE_URL}/rest/api/template/{tid}"
    auth = HTTPBasicAuth(CONFLUENCE_USER_EMAIL, CONFLUENCE_API_TOKEN)
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, auth=auth, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Tenta pegar o corpo XHTML (storage format) ou ADF (editor format)
            template_body = data.get("body", {}).get("storage", {}).get("value")
            
            if not template_body:
                template_body = data.get("body", {}).get("editor", {}).get("value") or data.get("body")
            
            template_info = {
                "id": data.get("templateId"),
                "name": data.get("name"),
                "body": template_body
            }
            
            _TEMPLATE_CACHE[tid] = template_info
            print(f"✅ Template '{data.get('name')}' (ID: {tid}) carregado com sucesso!")
            return template_info
        else:
            print(f"⚠️ Erro ao buscar template no Confluence (Status {resp.status_code}): {resp.text}")
            return None
    except Exception as e:
        print(f"⚠️ Exceção ao conectar no Confluence: {e}")
        return None
