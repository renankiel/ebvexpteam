import os
import json
import copy
import requests
from requests.auth import HTTPBasicAuth

# Carrega variáveis globais de ambiente
JIRA_URL = (os.getenv("JIRA_URL") or "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
CONFLUENCE_TECH_ACTIVITIES_ID = os.getenv("CONFLUENCE_TECH_ACTIVITIES_ID", "950273")

# Cache em memória para o template ADF
_TECH_ACTIVITIES_CACHE = None

def get_tech_activities_content_from_confluence(force_refresh=False):
    """
    Busca o conteúdo completo da página do Confluence (ID 950273) em formato ADF
    e retorna a lista de nós do seu corpo.
    """
    global _TECH_ACTIVITIES_CACHE
    if not force_refresh and _TECH_ACTIVITIES_CACHE is not None:
        return copy.deepcopy(_TECH_ACTIVITIES_CACHE)

    if not JIRA_URL or not JIRA_EMAIL or not JIRA_TOKEN:
        raise RuntimeError("Credenciais do Jira/Confluence incompletas no ambiente.")

    url = f"{JIRA_URL}/wiki/api/v2/pages/{CONFLUENCE_TECH_ACTIVITIES_ID}?body-format=atlas_doc_format"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, auth=auth, headers=headers, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Erro ao buscar página {CONFLUENCE_TECH_ACTIVITIES_ID} no Confluence: "
                f"{resp.status_code} - {resp.text}"
            )

        page_data = resp.json()
        body_val = page_data.get("body", {}).get("atlas_doc_format", {}).get("value")
        if not body_val:
            raise ValueError("O corpo da página do Confluence veio vazio ou não está em formato ADF.")

        adf_doc = json.loads(body_val) if isinstance(body_val, str) else body_val

        # Pega todos os blocos de primeiro nível da página do Confluence
        page_content = adf_doc.get("content", [])
        if not page_content:
            raise ValueError("A página do Confluence não possui conteúdo no seu documento ADF.")

        _TECH_ACTIVITIES_CACHE = page_content
        return copy.deepcopy(page_content)

    except Exception as e:
        print(f"⚠️ Erro ao obter template do Confluence: {e}")
        raise e
