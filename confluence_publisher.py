import os
import re
import json
import copy
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# Carrega variáveis do arquivo .env
load_dotenv()

# Configurações do Ambiente
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFLUENCE_SPACE_ID = os.getenv("CONFLUENCE_SPACE_ID")
CONFLUENCE_SPACE_KEY = os.getenv("CONFLUENCE_SPACE_KEY")
CONFLUENCE_TECH_ACTIVITIES_ID = os.getenv("CONFLUENCE_TECH_ACTIVITIES_ID", "950273")
JIRA_URL = (os.getenv("JIRA_URL") or "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")

# ID do Template obtido no Confluence
CONFLUENCE_TEMPLATE_ID = os.getenv("CONFLUENCE_TEMPLATE_ID")

# Cache em memória para evitar requisições redundantes ao Confluence
_TEMPLATE_CACHE = {}
_TECH_ACTIVITIES_CACHE = None

def get_confluence_template_data(template_id=None, force_refresh=False):
    """
    Substitui a dependência do 'novo 26.json' local buscando o 
    template original diretamente no Confluence Cloud via API.
    """
    global _TEMPLATE_CACHE
    tid = str(template_id or CONFLUENCE_TEMPLATE_ID)

    # Se já estiver cacheado e não for forçado o refresh, retorna a cópia limpa do cache
    if not force_refresh and tid in _TEMPLATE_CACHE:
        return copy.deepcopy(_TEMPLATE_CACHE[tid])

    # API de templates do Confluence solicitando o formato de documento (ADF)
    url = f"{JIRA_URL}/wiki/rest/api/template/{tid}?expand=body.atlas_doc_format"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    resp = requests.get(url, auth=auth, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Erro ao buscar template {tid} no Confluence (Status {resp.status_code}): {resp.text}"
        )

    data = resp.json()
    body = data.get("body", {})

    # Tenta obter o corpo do template em Atlassian Document Format (JSON)
    adf_val = body.get("atlas_doc_format", {}).get("value")
    if adf_val:
        adf_dict = json.loads(adf_val) if isinstance(adf_val, str) else adf_val
        result = {
            "representation": "atlas_doc_format",
            "content": adf_dict
        }
        # Salva no cache em memória
        _TEMPLATE_CACHE[tid] = result
        return copy.deepcopy(result)

    raise ValueError(f"O template {tid} foi encontrado no Confluence, mas seu formato ADF veio vazio.")


def replace_placeholders_in_adf(node, repl_dict):
    """
    Recursively navigates the ADF JSON tree and replaces specific text values
    based on a dictionary of placeholders and replacements.
    """
    if isinstance(node, list):
        for item in node:
            replace_placeholders_in_adf(item, repl_dict)
    elif isinstance(node, dict):
        if node.get("type") == "text" and "text" in node:
            text_val = node["text"]
            for placeholder, replacement in repl_dict.items():
                if placeholder in text_val:
                    node["text"] = text_val.replace(placeholder, str(replacement))

        if "marks" in node:
            for mark in node["marks"]:
                if mark.get("type") == "link" and "attrs" in mark and "href" in mark["attrs"]:
                    href_val = mark["attrs"]["href"]
                    for placeholder, replacement in repl_dict.items():
                        if placeholder in href_val:
                            mark["attrs"]["href"] = href_val.replace(placeholder, str(replacement))

        for key, val in node.items():
            if isinstance(val, (dict, list)) and key != "marks":
                replace_placeholders_in_adf(val, repl_dict)


def publish_to_confluence(ticket_data):
    """
    Loads template from Confluence Cloud, compiles real ticket data, and creates a page.
    """
    try:
        # 🟢 ATUALIZADO: Busca o template diretamente da API do Confluence
        try:
            template_data = get_confluence_template_data()
            adf_document = template_data["content"]
        except Exception as err:
            return {"success": False, "message": f"Falha ao obter template online: {str(err)}"}

        replacements = {
            "CRMB2B-30977": ticket_data.get("ticket", ""),
            "Renan Kiel": ticket_data.get("assignee", "Unassigned"),
            "Pipeline Health Agent Deployment (Saúde Pipe P1)": ticket_data.get("description", "Reason for change"),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20297": ticket_data.get("pr_to_prod", ""),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20296": ticket_data.get("pr_devint", ""),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20295": ticket_data.get("pr_qa", ""),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20298": ticket_data.get("pr_uat", ""),
        }

        replace_placeholders_in_adf(adf_document, replacements)

        confluence_endpoint = f"{JIRA_URL}/wiki/api/v2/pages"

        payload = {
            "spaceId": CONFLUENCE_SPACE_ID,
            "status": "current",
            "title": f"SMT.{CONFLUENCE_SPACE_KEY}.{ticket_data.get('ticket')}",
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(adf_document)
            }
        }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        response = requests.post(
            confluence_endpoint,
            json=payload,
            headers=headers,
            auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
            timeout=30
        )

        if response.status_code in [200, 201]:
            # 🟢 Declaração limpa do json de resposta
            created_page_data = response.json()
            created_page_id = created_page_data.get("id")
            
            # 🟢 Executa a chamada para definir o formato Wide (Full-Width)
            if created_page_id:
                try:
                    set_confluence_page_full_width(created_page_id)
                except Exception as e:
                    print(f"⚠️ Não foi possível aplicar layout Wide: {e}")

            # 🟢 Monta a URL final usando a chave criada com segurança
            page_url = f"{JIRA_URL}/wiki{created_page_data.get('_links', {}).get('webui')}"
            return {"success": True, "url": page_url}
        else:
            return {
                "success": False,
                "message": f"Confluence API returned status code {response.status_code}: {response.text}"
            }


    except Exception as e:
        return {"success": False, "message": str(e)}


def set_confluence_page_full_width(page_id):
    """
    Define a página criada como Wide (Full-Width) tanto para visualização quanto para edição.
    Utiliza a API v2 de Content Properties do Confluence Cloud.
    """
    if not page_id:
        return

    prop_url = f"{JIRA_URL}/wiki/api/v2/pages/{page_id}/properties"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    # As duas propriedades oficiais que controlam a largura da página
    properties_to_set = [
        {"key": "content-appearance-published", "value": "full-width"},
        {"key": "content-appearance-draft", "value": "full-width"}
    ]

    for prop in properties_to_set:
        try:
            resp = requests.post(prop_url, json=prop, headers=headers, auth=auth, timeout=10)
            if resp.status_code not in [200, 201]:
                # Se já existia, a API pode retornar erro, o que é esperado
                pass
        except Exception as e:
            print(f"⚠️ Erro ao aplicar propriedade {prop['key']} na página {page_id}: {e}")


def resolve_confluence_page_id(page_url, ticket_key):
    """
    Extracts the numeric Confluence pageId from the URL (e.g., /pages/123456/... or viewpage.action?pageId=123456).
    If the URL does not contain the numeric ID, queries Confluence API v2 by title.
    """
    if page_url:
        # 1. Tenta extrair do formato amigável: /pages/12345678/
        match = re.search(r"/pages/(\d+)", page_url)
        if match:
            return match.group(1)
        
        # 2. Tenta extrair de parâmetros clássicos: viewpage.action?pageId=12345678
        match_param = re.search(r"[?&]pageId=(\d+)", page_url)
        if match_param:
            return match_param.group(1)

    # Fallback: lookup page by title in Confluence
    expected_title = f"SMT.{CONFLUENCE_SPACE_KEY}.{ticket_key}"
    search_url = f"{JIRA_URL}/wiki/api/v2/pages"
    params = {
        "space-id": CONFLUENCE_SPACE_ID,
        "title": expected_title
    }
    headers = {"Accept": "application/json"}

    resp = requests.get(
        search_url,
        params=params,
        headers=headers,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        timeout=20
    )

    if resp.status_code == 200:
        results = resp.json().get("results", [])
        if results:
            return str(results[0].get("id"))

    return None


def get_existing_confluence_attachments(page_id):
    """
    Returns a dictionary mapping {filename: attachment_id} for all attachments
    currently present on the given Confluence page.
    """
    endpoint = f"{JIRA_URL}/wiki/rest/api/content/{page_id}/child/attachment?limit=200"
    headers = {"Accept": "application/json"}

    resp = requests.get(
        endpoint,
        headers=headers,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        timeout=20
    )

    existing_map = {}
    if resp.status_code == 200:
        for att in resp.json().get("results", []):
            title = att.get("title")
            att_id = att.get("id")
            if title and att_id:
                existing_map[title] = att_id

    return existing_map


def delete_confluence_attachment(attachment_id):
    """
    Deletes an existing attachment in Confluence by its ID so it is never duplicated.
    """
    endpoint = f"{JIRA_URL}/wiki/rest/api/content/{attachment_id}"
    resp = requests.delete(
        endpoint,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        timeout=20
    )
    return resp.status_code in [200, 204]


def upload_attachment_to_confluence(page_id, filename, file_bytes, mime_type="application/octet-stream"):
    """
    Uploads a file to the specified Confluence page using multipart/form-data.
    """
    endpoint = f"{JIRA_URL}/wiki/rest/api/content/{page_id}/child/attachment"
    headers = {
        "X-Atlassian-Token": "nocheck"
    }
    files = {
        "file": (filename, file_bytes, mime_type or "application/octet-stream")
    }

    resp = requests.post(
        endpoint,
        headers=headers,
        files=files,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        timeout=60
    )

    if resp.status_code in [200, 201]:
        return True, ""
    return False, f"Status {resp.status_code}: {resp.text}"


def sync_attachments_to_confluence(ticket_key, page_url, attachments_to_sync):
    """
    Receives a list of Jira attachment dicts [{'filename': ..., 'content_url': ..., 'mime_type': ...}].
    1. Resolves the target Confluence page ID.
    2. Checks existing attachments on that Confluence page.
    3. If a file with the same name already exists, deletes it first.
    4. Downloads the file from Jira and uploads it to Confluence.
    """
    if not attachments_to_sync:
        return {
            "success": False,
            "message": "No media files found in the DEPLOYMENT RESULTS table for this ticket."
        }

    page_id = resolve_confluence_page_id(page_url, ticket_key)
    if not page_id:
        return {
            "success": False,
            "message": f"Could not resolve Confluence Page ID for ticket {ticket_key}."
        }

    try:
        existing_attachments = get_existing_confluence_attachments(page_id)
        uploaded_count = 0

        for att in attachments_to_sync:
            filename = att.get("filename")
            content_url = att.get("content_url")
            mime_type = att.get("mime_type", "application/octet-stream")

            if not filename or not content_url:
                continue

            # 1. Download file binary content from Jira
            jira_file_resp = requests.get(
                content_url,
                auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
                timeout=30
            )
            if jira_file_resp.status_code != 200:
                return {
                    "success": False,
                    "message": f"Failed to download '{filename}' from Jira (Status {jira_file_resp.status_code})."
                }

            # 2. If file with the same name already exists on Confluence, delete it first
            if filename in existing_attachments:
                old_att_id = existing_attachments[filename]
                delete_confluence_attachment(old_att_id)

            # 3. Upload fresh file to Confluence
            ok, err_msg = upload_attachment_to_confluence(
                page_id=page_id,
                filename=filename,
                file_bytes=jira_file_resp.content,
                mime_type=mime_type
            )
            if not ok:
                return {
                    "success": False,
                    "message": f"Failed to upload '{filename}' to Confluence: {err_msg}"
                }

            uploaded_count += 1

        return {
            "success": True,
            "uploaded_count": uploaded_count,
            "message": f"Successfully synced {uploaded_count} attachment(s) to Confluence."
        }

    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


def get_tech_activities_content_from_confluence(force_refresh=False):
    """
    Busca o conteúdo completo da página do Confluence (ID 950273) em formato ADF
    e retorna a lista de nós do seu corpo.
    """
    global _TECH_ACTIVITIES_CACHE
    if not force_refresh and _TECH_ACTIVITIES_CACHE:
        return copy.deepcopy(_TECH_ACTIVITIES_CACHE)

    url = f"{JIRA_URL}/wiki/api/v2/pages/{CONFLUENCE_TECH_ACTIVITIES_ID}?body-format=atlas_doc_format"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    resp = requests.get(url, auth=auth, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Erro ao buscar página {CONFLUENCE_TECH_ACTIVITIES_ID} no Confluence: {resp.status_code} - {resp.text}")

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