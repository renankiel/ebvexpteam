import os
import re
import requests
from confluence_service import get_confluence_template
from auth import init_auth, login_required
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request
from jira import JIRA

# =====================================================================
# 🟢 1. LOAD ENVIRONMENT VARIABLES FIRST
# =====================================================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
env_path = os.path.join(BASE_DIR, ".env")
env_path_txt = os.path.join(BASE_DIR, ".env.txt")

if os.path.exists(env_path):
    print(f"✅ Found .env file at: {env_path}")
    load_dotenv(env_path)
elif os.path.exists(env_path_txt):
    print(f"ℹ️ Found .env.txt file at: {env_path_txt}. Loading...")
    load_dotenv(env_path_txt)
else:
    print(f"❌ WARNING: No .env or .env.txt found in: {BASE_DIR}")

from confluence_publisher import (
    publish_to_confluence,
    sync_attachments_to_confluence,
    resolve_confluence_page_id,
    get_existing_confluence_attachments
)
from servicenow_publisher import create_servicenow_change

app = Flask(__name__)
init_auth(app)

# =====================================================================
# 🔑 CREDENTIALS LOADED DYNAMICALLY FROM YOUR .ENV FILE
# =====================================================================
JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")
CONFLUENCE_SPACE_ID = os.getenv("CONFLUENCE_SPACE_ID", "1048580")

STATUS_CANDIDATES = [
    "DEV BACKLOG",
    "ON HOLD",
    "IN DEV",
    "DEV COMPLETE",
    "READY FOR TEST",
    "IN TEST"
]

TYPE_TRANSLATION = {
    "história": "Story",
    "historia": "Story",
    "tarefa": "Task",
    "subtarefa": "Subtask",
    "bug": "Bug",
    "erro": "Bug"
}

READY_FOR_PRODUCTION_TICKETS = [
    {
        "snow_state": "exists",
        "snow_url": "https://dev489662.service-now.com/nav_to.do?uri=change_request.do?sysparm_query=number=CHG1326437",
        "snow_number": "CHG1326437",
        "ticket": "CRMB2B-31284",
        "type": "Story",
        "type_lower": "story",
        "cloud": "Sales",
        "cloud_lower": "sales",
        "status": "Ready For Production",
        "status_lower_dashed": "ready-for-production",
        "alm_page_state": "ready",
        "alm_page_url": "",
        "assignee": "Renan Kiel",
        "assignee_initial": "R",
        "manual": "No",
        "manual_lower": "no",
        "logs": "Yes",
        "logs_lower": "yes",
        "pr_to_prod": "20945",
        "pr_validated": "No",
        "pr_validated_lower": "no",
        "signoffs": {
            "DEVINT": True,
            "QA": True,
            "UAT": True
        },
        "execution_order": "",
        "change_hour": "12/11 07:00"
    }
]

CHART_DATA = {
    "labels": ["Today", "Tomorrow", "Day After", "In 3 Days", "In 4 Days"],
    "counts": [2, 3, 4, 1, 5]
}


# =====================================================================
# 🧩 ADF PARSING, TABLE INSPECTION & UPDATING FUNCTIONS
# =====================================================================
def extract_plain_text_adf(adf_node):
    """Recursively traverses any ADF JSON block and merges all text elements."""
    if not isinstance(adf_node, dict):
        return ""

    if adf_node.get("type") == "text":
        return adf_node.get("text", "")

    # 🟢 NOVO: Suporte a nós de Status do Jira ADF (como DONE e ERROR)
    if adf_node.get("type") == "status":
        return adf_node.get("attrs", {}).get("text", "")

    # Suporte a nós de Data do Jira ADF
    if adf_node.get("type") == "date":
        timestamp_ms = adf_node.get("attrs", {}).get("timestamp")
        if timestamp_ms:
            try:
                import datetime
                dt = datetime.datetime.utcfromtimestamp(int(timestamp_ms) / 1000.0)
                return dt.strftime("%d/%m")
            except:
                pass

    text_pieces = []
    for child in adf_node.get("content", []):
        piece = extract_plain_text_adf(child)
        if piece:
            text_pieces.append(piece)

    separator = " | " if adf_node.get("type") in ["tableRow", "doc"] else " "
    return separator.join(text_pieces).strip()


def extract_prd_pr_adf(adf_description):
    """Extracts PRD PR value from the last row of the description table containing a 'PRD' header."""
    if not isinstance(adf_description, dict):
        return ""

    try:
        tables = [
            block for block in adf_description.get("content", [])
            if isinstance(block, dict) and block.get("type") == "table"
        ]

        for table in tables:
            rows = table.get("content", [])
            prd_column_index = None
            data_rows = []

            for row in rows:
                if row.get("type") != "tableRow":
                    continue

                cells = row.get("content", [])
                if not cells:
                    continue

                cell_types = [c.get("type") for c in cells]
                if "tableHeader" in cell_types:
                    for idx, cell in enumerate(cells):
                        header_text = extract_plain_text_adf(cell).upper()
                        if "PRD" in header_text:
                            prd_column_index = idx
                else:
                    data_rows.append(cells)

            if prd_column_index is not None and data_rows:
                last_row_cells = data_rows[-1]
                if prd_column_index < len(last_row_cells):
                    target_cell = last_row_cells[prd_column_index]
                    prd_value = extract_plain_text_adf(target_cell).strip()
                    if prd_value:
                        return prd_value

    except Exception as e:
        print(f"⚠️ Error parsing ADF table: {e}")

    return ""


def find_url_and_text_in_node(node):
    """Recursively searches inside an ADF node for a link (Card or classic text link)."""
    if isinstance(node, list):
        for item in node:
            url, text = find_url_and_text_in_node(item)
            if url:
                return url, text
    elif isinstance(node, dict):
        if node.get("type") == "inlineCard":
            url = node.get("attrs", {}).get("url", "").strip()
            text = url.rstrip("/").split("/")[-1] if url else ""
            return url, text

        if node.get("type") == "text":
            for mark in node.get("marks", []):
                if mark.get("type") == "link":
                    url = mark.get("attrs", {}).get("href", "").strip()
                    text = node.get("text", "").strip()
                    if url:
                        return url, text
            raw_text = node.get("text", "").strip()
            if raw_text.startswith("http://") or raw_text.startswith("https://"):
                return raw_text, raw_text

        for child in node.get("content", []):
            url, text = find_url_and_text_in_node(child)
            if url:
                return url, text

    return "", ""


def node_contains_media(node):
    """Recursively checks if an ADF node contains a media file."""
    if isinstance(node, list):
        return any(node_contains_media(item) for item in node)
    elif isinstance(node, dict):
        node_type = node.get("type", "")
        if node_type in ["mediaInline", "media", "mediaSingle"]:
            return True

        attrs = node.get("attrs", {})
        if attrs.get("type") == "file" and node_type.startswith("media"):
            return True

        for child in node.get("content", []):
            if node_contains_media(child):
                return True

    return False


def collect_media_nodes_from_node(node, collected_list):
    """Recursively collects all 'mediaInline' / 'media' attribute dictionaries inside a given ADF node."""
    if isinstance(node, list):
        for item in node:
            collect_media_nodes_from_node(item, collected_list)
    elif isinstance(node, dict):
        node_type = node.get("type", "")
        if node_type in ["mediaInline", "media"]:
            attrs = node.get("attrs", {})
            if attrs:
                collected_list.append(attrs)

        for child in node.get("content", []):
            collect_media_nodes_from_node(child, collected_list)


def find_panel_table(adf_description, panel_keyword):
    """Finds the Info Panel matching panel_keyword and returns its following table block."""
    if not isinstance(adf_description, dict):
        return None

    blocks = adf_description.get("content", [])
    target_keyword = panel_keyword.upper()

    for idx, block in enumerate(blocks):
        if isinstance(block, dict) and block.get("type") == "panel":
            panel_text = extract_plain_text_adf(block).upper()

            if target_keyword == "SNOW" and "ALM PAGE" in panel_text:
                continue

            if target_keyword in panel_text:
                for next_idx in range(idx + 1, len(blocks)):
                    next_block = blocks[next_idx]
                    if not isinstance(next_block, dict):
                        continue

                    next_type = next_block.get("type")
                    if next_type == "table":
                        return next_block

                    if next_type == "panel" or extract_plain_text_adf(next_block).strip():
                        break

                return None

    return None


def get_link_cell_from_table(table_node):
    """Locates the 'LINK' column inside a table node and returns its first data cell."""
    if not isinstance(table_node, dict) or table_node.get("type") != "table":
        return None

    rows = table_node.get("content", [])
    link_col_index = 0
    data_rows = []

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue

        cells = row.get("content", [])
        if not cells:
            continue

        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            for idx, cell in enumerate(cells):
                header_text = extract_plain_text_adf(cell).upper()
                if "LINK" in header_text:
                    link_col_index = idx
                    break
        else:
            data_rows.append(cells)

    if not data_rows:
        return None

    first_data_row = data_rows[0]
    if link_col_index < len(first_data_row):
        return first_data_row[link_col_index]
    return first_data_row[0]


def check_deployment_results_logs(description_adf):
    """Returns 'Yes' if every row contains a media file in 'DEPLOYMENT RESULTS' table, else 'No'."""
    table_node = find_panel_table(description_adf, "DEPLOYMENT RESULTS")
    if not table_node:
        return "No"

    rows = table_node.get("content", [])
    data_rows = []

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        cells = row.get("content", [])
        if not cells:
            continue

        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            continue
        data_rows.append(cells)

    if not data_rows:
        return "No"

    for cells in data_rows:
        file_cell = cells[-1]
        if not node_contains_media(file_cell):
            return "No"

    return "Yes"


def extract_deployment_results_attachments(issue_raw, description_adf):
    """Extracts all media files referenced in the 'DEPLOYMENT RESULTS' table."""
    table_node = find_panel_table(description_adf, "DEPLOYMENT RESULTS")
    if not table_node:
        return []

    media_attrs_list = []
    for row in table_node.get("content", []):
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        cells = row.get("content", [])
        if not cells:
            continue
        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            continue

        collect_media_nodes_from_node(cells[-1], media_attrs_list)

    if not media_attrs_list:
        return []

    issue_attachments = issue_raw.get("fields", {}).get("attachment", []) or []
    attachments_by_name = {}
    for att in issue_attachments:
        fname = att.get("filename")
        if fname:
            attachments_by_name[fname] = {
                "filename": fname,
                "content_url": att.get("content"),
                "mime_type": att.get("mimeType", "application/octet-stream")
            }

    matched_attachments = []
    seen_filenames = set()

    for m_attr in media_attrs_list:
        alt_name = (m_attr.get("alt") or "").strip()
        if alt_name and alt_name in attachments_by_name and alt_name not in seen_filenames:
            matched_attachments.append(attachments_by_name[alt_name])
            seen_filenames.add(alt_name)

    if not matched_attachments and issue_attachments:
        for fname, att_info in attachments_by_name.items():
            if fname not in seen_filenames:
                matched_attachments.append(att_info)
                seen_filenames.add(fname)

    return matched_attachments


def inspect_alm_page_status(description_adf):
    """Inspects 'ALM PAGE' status."""
    table_node = find_panel_table(description_adf, "ALM PAGE")
    if not table_node:
        return "none", ""

    link_cell = get_link_cell_from_table(table_node)
    if not link_cell:
        return "none", ""

    url, _ = find_url_and_text_in_node(link_cell)
    if url:
        return "exists", url
    return "ready", ""


def inspect_snow_status(description_adf):
    """Inspects 'SNOW' status."""
    table_node = find_panel_table(description_adf, "SNOW")
    if not table_node:
        return "none", "", ""

    link_cell = get_link_cell_from_table(table_node)
    if not link_cell:
        return "none", "", ""

    url, text = find_url_and_text_in_node(link_cell)
    if url:
        display_number = text if text else "View SNOW"
        return "exists", url, display_number
    return "ready", "", ""


def extract_change_hour_from_snow(description_adf):
    """
    Finds the SNOW panel table, extracts the values from the 'DATE' and 'TIME' columns,
    and returns them formatted as 'DATE TIME'.
    """
    table_node = find_panel_table(description_adf, "SNOW")
    if not table_node:
        return ""

    rows = table_node.get("content", [])
    date_col_index = None
    time_col_index = None
    data_rows = []

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue

        cells = row.get("content", [])
        if not cells:
            continue

        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            for idx, cell in enumerate(cells):
                header_text = extract_plain_text_adf(cell).upper().strip()
                if "DATE" in header_text:
                    date_col_index = idx
                elif "TIME" in header_text:
                    time_col_index = idx
        else:
            data_rows.append(cells)

    if not data_rows:
        return ""

    first_data_row = data_rows[0]
    date_val = ""
    time_val = ""

    if date_col_index is not None and date_col_index < len(first_data_row):
        date_val = extract_plain_text_adf(first_data_row[date_col_index]).strip()

    if time_col_index is not None and time_col_index < len(first_data_row):
        time_val = extract_plain_text_adf(first_data_row[time_col_index]).strip()

    if date_val and time_val:
        return f"{date_val} {time_val}"
    elif date_val:
        return date_val
    elif time_val:
        return time_val

    return ""

def extract_execution_order_from_snow(description_adf):
    """
    Localiza a tabela abaixo do Info Panel 'SNOW', busca a coluna 'ORDER'
    e retorna o valor contido nela.
    """
    table_node = find_panel_table(description_adf, "SNOW")
    if not table_node:
        return ""

    rows = table_node.get("content", [])
    order_col_index = None
    data_rows = []

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue

        cells = row.get("content", [])
        if not cells:
            continue

        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            for idx, cell in enumerate(cells):
                header_text = extract_plain_text_adf(cell).upper().strip()
                # Procura por qualquer variação de "ORDER" ou "EXECUTION ORDER"
                if "ORDER" in header_text:
                    order_col_index = idx
                    break
        else:
            data_rows.append(cells)

    if not data_rows or order_col_index is None:
        return ""

    first_data_row = data_rows[0]
    if order_col_index < len(first_data_row):
        return extract_plain_text_adf(first_data_row[order_col_index]).strip()

    return ""


def extract_alm_call_from_alm_page(description_adf):
    """
    Localiza a tabela abaixo do Info Panel 'ALM PAGE', busca a coluna 'ALM CALL'
    e retorna o valor contido nela (que já é convertido para DD/MM pelo extract_plain_text_adf).
    """
    table_node = find_panel_table(description_adf, "ALM PAGE")
    if not table_node:
        return "—"

    rows = table_node.get("content", [])
    alm_call_col_index = None
    data_rows = []

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue

        cells = row.get("content", [])
        if not cells:
            continue

        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            for idx, cell in enumerate(cells):
                header_text = extract_plain_text_adf(cell).upper().strip()
                if "ALM CALL" in header_text:
                    alm_call_col_index = idx
                    break
        else:
            data_rows.append(cells)

    if not data_rows or alm_call_col_index is None:
        return "—"

    first_data_row = data_rows[0]
    if alm_call_col_index < len(first_data_row):
        val = extract_plain_text_adf(first_data_row[alm_call_col_index]).strip()
        return val if val else "—"

    return "—"


def parse_date_for_sorting(date_str):
    """
    Converte strings de datas nos formatos 'DD/MM hh:mi' ou 'DD/MM' para datetime
    permitindo uma ordenação cronológica precisa no Python.
    """
    import datetime
    if not date_str or date_str in ["—", "N/A"]:
        return datetime.datetime(9999, 12, 31, 23, 59)
    
    current_year = datetime.datetime.now().year
    
    # Tenta formato com hora: 'DD/MM hh:mi'
    try:
        return datetime.datetime.strptime(f"{date_str} {current_year}", "%d/%m %H:%M %Y")
    except ValueError:
        pass
        
    # Tenta formato apenas data: 'DD/MM'
    try:
        return datetime.datetime.strptime(f"{date_str} {current_year}", "%d/%m %Y")
    except ValueError:
        pass
        
    return datetime.datetime(9999, 12, 31, 23, 59)


def sort_alm_call_groups(group_name):
    """
    Garante que as seções de data fiquem em ordem cronológica
    e coloca o 'To Be Defined' sempre no fim da fila.
    """
    import datetime
    if group_name == "To Be Defined":
        return datetime.datetime(9999, 12, 31, 23, 59)
    return parse_date_for_sorting(group_name)


def parse_confluence_approvals(page_id):
    """
    Busca o corpo ADF ao vivo da página do Confluence e extrai o status de aprovação:
      - Linha 1 de dados (Índice 0 - Executive): Desconsiderar
      - Linha 2 de dados (Índice 1): UAT
      - Linha 3 de dados (Índice 2): QA
      - Linha 4 de dados (Índice 3): DEVINT
    """
    import json  # 🟢 CORREÇÃO: Importado localmente para garantir que esteja definido!
    
    statuses = {"DEVINT": False, "QA": False, "UAT": False}
    if not page_id:
        print("⚠️ [DEBUG SIGNOFF] Nenhum page_id fornecido. Retornando False.")
        return statuses

    try:
        base_url = (JIRA_URL or "").rstrip("/")
        url = f"{base_url}/wiki/api/v2/pages/{page_id}"
        headers = {"Accept": "application/json"}

        print(f"📡 [DEBUG SIGNOFF] Consultando Confluence Page ID: {page_id}")
        resp = requests.get(
            url,
            params={"body-format": "atlas_doc_format"},
            headers=headers,
            auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
            timeout=15
        )

        print(f"📥 [DEBUG SIGNOFF] HTTP Status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"❌ [DEBUG SIGNOFF] Erro HTTP Confluence: {resp.text[:200]}")
            return statuses

        page_data = resp.json()
        body_val = page_data.get("body", {}).get("atlas_doc_format", {}).get("value", None)

        adf_body = None
        if isinstance(body_val, str):
            try:
                adf_body = json.loads(body_val)
            except Exception as json_err:
                print(f"❌ [DEBUG SIGNOFF] Erro ao decodificar JSON ADF: {json_err}")
        elif isinstance(body_val, dict):
            adf_body = body_val

        if not isinstance(adf_body, dict):
            print("⚠️ [DEBUG SIGNOFF] ADF vazio ou inválido na resposta da API.")
            return statuses

        # 1. Encontra todas as tabelas no ADF
        tables = []
        def find_tables(node):
            if isinstance(node, list):
                for item in node:
                    find_tables(item)
            elif isinstance(node, dict):
                if node.get("type") == "table":
                    tables.append(node)
                for child in node.get("content", []):
                    find_tables(child)

        find_tables(adf_body)

        # 2. Avalia se uma célula possui taskItem marcado como 'DONE'
        def is_cell_approved(cell_node):
            state = "TODO"
            def check_state(n):
                nonlocal state
                if isinstance(n, list):
                    for item in n:
                        check_state(item)
                elif isinstance(n, dict):
                    if n.get("type") == "taskItem":
                        state = n.get("attrs", {}).get("state", "TODO")
                    for child in n.get("content", []):
                        check_state(child)
            check_state(cell_node)
            return state == "DONE"

        # 3. Percorre as tabelas procurando a coluna STATUS
        for t_idx, table in enumerate(tables):
            rows = table.get("content", [])
            status_col_idx = None
            data_rows = []

            for row in rows:
                if not isinstance(row, dict) or row.get("type") != "tableRow":
                    continue
                cells = row.get("content", [])
                if not cells:
                    continue

                row_texts = [extract_plain_text_adf(c).upper().strip() for c in cells]
                if "STATUS" in row_texts:
                    status_col_idx = row_texts.index("STATUS")
                else:
                    data_rows.append(cells)

            if status_col_idx is not None and data_rows:
                print(f"✅ [DEBUG SIGNOFF] Tabela de aprovações encontrada com {len(data_rows)} linhas de dados.")

                # Linha 2 de dados -> UAT
                if len(data_rows) > 1 and len(data_rows[1]) > status_col_idx:
                    statuses["UAT"] = is_cell_approved(data_rows[1][status_col_idx])

                # Linha 3 de dados -> QA
                if len(data_rows) > 2 and len(data_rows[2]) > status_col_idx:
                    statuses["QA"] = is_cell_approved(data_rows[2][status_col_idx])

                # Linha 4 de dados -> DEVINT
                if len(data_rows) > 3 and len(data_rows[3]) > status_col_idx:
                    statuses["DEVINT"] = is_cell_approved(data_rows[3][status_col_idx])

                print(f"🎯 [DEBUG SIGNOFF] Resultado das aprovações extraídas: {statuses}")
                break

    except Exception as e:
        print(f"⚠️ [DEBUG SIGNOFF] Exceção durante parsing: {e}")

    return statuses 

def update_panel_table_link_adf(adf_description, panel_keyword, new_url, display_text=None):
    """Updates the LINK cell of the table following the Info Panel."""
    table_node = find_panel_table(adf_description, panel_keyword)
    if not table_node:
        return False

    link_cell = get_link_cell_from_table(table_node)
    if not link_cell:
        return False

    if display_text:
        link_cell["content"] = [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": str(display_text),
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {
                                    "href": new_url
                                }
                            }
                        ]
                    }
                ]
            }
        ]
    else:
        link_cell["content"] = [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "inlineCard",
                        "attrs": {
                            "url": new_url
                        }
                    }
                ]
            }
        ]

    return True


def update_jira_description_adf(ticket_id, updated_adf):
    """Overwrites Jira issue description with updated ADF JSON."""
    endpoint = f"{JIRA_URL}/rest/api/3/issue/{ticket_id}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    payload = {
        "fields": {
            "description": updated_adf
        }
    }

    response = requests.put(
        endpoint,
        json=payload,
        headers=headers,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    )

    if response.status_code in [200, 204]:
        return True, "Jira description updated successfully."
    else:
        return False, f"Failed to update Jira (Status {response.status_code}): {response.text}"


def find_sprint_field_key(issue_fields):
    """
    Detecta dinamicamente qual campo customfield_XXXX representa o campo de Sprint do Jira.
    """
    for key, value in issue_fields.items():
        if key.startswith("customfield_") and isinstance(value, list) and len(value) > 0:
            first_val = value[0]
            if isinstance(first_val, str) and "com.atlassian.greenhopper.service.sprint.Sprint" in first_val:
                return key
            if hasattr(first_val, "id") and hasattr(first_val, "name") and ("sprint" in str(type(first_val)).lower() or "sprint" in str(first_val.__class__).lower()):
                return key
            if isinstance(first_val, dict) and "id" in first_val and "name" in first_val:
                return key
    return None


def extract_sprints(sprint_field):
    """
    Extrai uma lista de dicionários contendo ID e Nome de todas as Sprints que o ticket possui.
    """
    if not sprint_field:
        return []
    
    sprints = []
    for item in sprint_field:
        if isinstance(item, str):
            # Parse do formato de string clássica do Greenhopper
            import re
            id_match = re.search(r"id=(\d+)", item)
            name_match = re.search(r"name=([^,\]]+)", item)
            if id_match and name_match:
                try:
                    sprints.append({
                        "id": int(id_match.group(1)),
                        "name": name_match.group(1).strip()
                    })
                except ValueError:
                    pass
        elif hasattr(item, "id") and hasattr(item, "name"):
            try:
                sprints.append({
                    "id": int(item.id),
                    "name": item.name
                })
            except (ValueError, TypeError):
                pass
        elif isinstance(item, dict):
            try:
                sprints.append({
                    "id": int(item.get("id")),
                    "name": item.get("name")
                })
            except (ValueError, TypeError):
                pass
    return sprints


def check_double_attachments(issue_raw, description_adf, alm_state, alm_url, issue_key):
    """
    Realiza a checagem dupla dos anexos:
    1. Verifica se todos os arquivos referenciados no ADF existem anexados no Jira.
    2. Verifica se esses mesmos arquivos já foram enviados e existem no Confluence.
    """
    table_node = find_panel_table(description_adf, "DEPLOYMENT RESULTS")
    if not table_node:
        return "No"

    # Encontra todos os nós de media referenciados na tabela do ADF
    media_attrs_list = []
    for row in table_node.get("content", []):
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        cells = row.get("content", [])
        if not cells:
            continue
        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            continue
        collect_media_nodes_from_node(cells[-1], media_attrs_list)

    if not media_attrs_list:
        return "No"

    # Obtém os anexos válidos correspondidos no Jira
    jira_attachments = extract_deployment_results_attachments(issue_raw, description_adf)
    
    # 1. Se o número de anexos válidos no Jira for menor que o referenciado, faltam arquivos no Jira
    if len(jira_attachments) < len(media_attrs_list):
        return "No"

    # 2. Se a página do Confluence não existe ou não pôde ser resolvida, é "No" (pois os logs ainda não estão lá)
    if alm_state != "exists" or not alm_url:
        return "No"

    confluence_page_id = resolve_confluence_page_id(alm_url, issue_key)
    if not confluence_page_id:
        return "No"

    # Obtém a lista real de anexos da página do Confluence
    confluence_attachments = get_existing_confluence_attachments(confluence_page_id)

    # 3. Verifica se cada anexo do Jira existe no Confluence com o mesmo nome de arquivo
    for att in jira_attachments:
        filename = att.get("filename")
        if not filename or filename not in confluence_attachments:
            return "No"

    return "Yes"


def extract_prd_status_adf(adf_description):
    """
    Localiza a tabela no ADF da descrição e varre o cabeçalho 'PRD' para
    encontrar se existe um elemento de status do Jira marcando 'DONE' ou 'ERROR'.
    """
    if not isinstance(adf_description, dict):
        return "—"

    try:
        tables = [
            block for block in adf_description.get("content", [])
            if isinstance(block, dict) and block.get("type") == "table"
        ]

        for table in tables:
            rows = table.get("content", [])
            for row in rows:
                if row.get("type") != "tableRow":
                    continue

                cells = row.get("content", [])
                if not cells:
                    continue

                cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
                if "tableHeader" in cell_types:
                    for idx, cell in enumerate(cells):
                        header_text = extract_plain_text_adf(cell).upper()
                        # Verifica se é a coluna PRD
                        if "PRD" in header_text:
                            status_val = "—"
                            
                            # Varre a célula de cabeçalho para achar o nó 'status'
                            def find_status_node(node):
                                nonlocal status_val
                                if isinstance(node, list):
                                    for item in node:
                                        find_status_node(item)
                                elif isinstance(node, dict):
                                    if node.get("type") == "status":
                                        status_val = node.get("attrs", {}).get("text", "").upper().strip()
                                    for child in node.get("content", []):
                                        find_status_node(child)
                                        
                            find_status_node(cell)
                            
                            if status_val == "DONE":
                                return "Yes"
                            elif status_val == "ERROR":
                                return "No"
                            else:
                                return "—"

    except Exception as e:
        print(f"⚠️ Error parsing PRD status: {e}")

    return "—"


# =====================================================================
# 🔄 JIRA REST API v3 INTEGRATION
# =====================================================================
def fetch_jira_data():
    candidates_groups = {}
    done_list = []
    ready_prod_list = []

    if not JIRA_URL or not JIRA_EMAIL or not JIRA_TOKEN or not JIRA_PROJECT_KEY:
        print("⚠️ Error: Jira credentials missing or incomplete in .env!")
        error_group = [
            {
                "version": f"{JIRA_PROJECT_KEY or 'CRMB2B'} - No Version Defined",
                "tickets": []
            }
        ]
        return error_group, [], []

    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        jql_query = f'project = "{JIRA_PROJECT_KEY}" ORDER BY created DESC'
        issues = jira.search_issues(jql_query, maxResults=100)

        # Descoberta dinâmica do ID do campo Sprint
        sprint_field_key = None
        for issue in issues:
            sprint_field_key = find_sprint_field_key(issue.raw.get("fields", {}))
            if sprint_field_key:
                break

        for issue in issues:
            # 1. Filtro da Label "BVISTA"
            labels = issue.fields.labels or []
            labels_upper = [lbl.upper() for lbl in labels]
            if "BVISTA" not in labels_upper:
                continue  # Pula o ticket se não tiver a label BVISTA

            current_status = issue.fields.status.name.upper()
            
            # 🟢 CORREÇÃO: Declarada aqui no início para ser usada por todas as outras funções do loop
            description_adf = issue.raw.get("fields", {}).get("description")

            native_type = issue.fields.issuetype.name
            formatted_type = TYPE_TRANSLATION.get(native_type.lower(), native_type)

            assignee_name = (
                issue.fields.assignee.displayName
                if issue.fields.assignee
                else "Unassigned"
            )

            labels_lower = [lbl.lower() for lbl in labels]
            if "service" in labels_lower:
                cloud_val = "Service"
            elif "sales" in labels_lower:
                cloud_val = "Sales"
            else:
                cloud_val = "—"

            # 2. Avaliação das Labels e modo manual
            has_pull_request = "pull-request" in labels_lower
            has_manually = "manually" in labels_lower

            if has_pull_request:
                manual_val = "No"
            elif has_manually:
                manual_val = "Yes"
            else:
                manual_val = "—"

            readable_description = (
                extract_plain_text_adf(description_adf)
                if description_adf
                else "No description"
            )

            prd_pr = extract_prd_pr_adf(description_adf)
            
            # 🟢 NOVO: Extrai dinamicamente o status do cabeçalho PRD do Jira
            prd_status = extract_prd_status_adf(description_adf)

            # Carrega os estados da página do Confluence ALM
            alm_state, alm_url = inspect_alm_page_status(description_adf)

            # Checagem dupla de LOGS (Jira <-> Confluence)
            logs_status = check_double_attachments(issue.raw, description_adf, alm_state, alm_url, issue.key)

            if manual_val == "Yes":
                logs_val = "N/A"
                pr_to_prod_val = "N/A"
                pr_validated_val = "N/A"  # Mantém N/A caso seja manual
            else:
                logs_val = logs_status
                pr_to_prod_val = prd_pr if prd_pr else "—"
                pr_validated_val = prd_status  # 🟢 ATUALIZADO: Recebe "Yes", "No" ou "—"

            # 5. Carrega o estado do painel SNOW e extrai Sprints do Jira
            snow_state, snow_url, snow_num = inspect_snow_status(description_adf)

            sprint_raw = issue.raw.get("fields", {}).get(sprint_field_key) if sprint_field_key else None
            sprints_list = extract_sprints(sprint_raw)

            sprint_name = f"{JIRA_PROJECT_KEY} - No Version Defined"
            oldest_sprint_id = 999999999  # Sem sprint vai para o fim da fila

            if sprints_list:
                oldest_sprint = min(sprints_list, key=lambda x: x["id"])
                sprint_name = oldest_sprint["name"]
                oldest_sprint_id = oldest_sprint["id"]

            # 6. Resolve Confluence approvals em tempo real se a página existir
            signoff_statuses = {"DEVINT": False, "QA": False, "UAT": False}
            if alm_state == "exists":
                confluence_page_id = resolve_confluence_page_id(alm_url, issue.key)
                if confluence_page_id:
                    signoff_statuses = parse_confluence_approvals(confluence_page_id)

            ticket_obj = {
                "snow_state": snow_state,
                "snow_url": snow_url,
                "snow_number": snow_num or "View SNOW",
                "ticket": issue.key,
                "type": formatted_type,
                "type_lower": formatted_type.lower(),
                "cloud": cloud_val,
                "cloud_lower": cloud_val.lower(),
                "status": current_status,
                "status_lower_dashed": current_status.lower().replace(' ', '-'),
                "description": readable_description,
                "alm_page_state": alm_state,
                "alm_page_url": alm_url,
                "assignee": assignee_name,
                "assignee_initial": assignee_name[0] if assignee_name else "?",
                "manual": manual_val,
                "manual_lower": manual_val.lower(),
                "logs": logs_val,
                "logs_lower": logs_val.lower().replace('/', '-'),
                "pr_to_prod": pr_to_prod_val,
                "pr_validated": pr_validated_val,
                "pr_validated_lower": pr_validated_val.lower().replace('/', '-'),
                "signoffs": signoff_statuses,
                "alm_call": extract_alm_call_from_alm_page(description_adf),
                "execution_order": extract_execution_order_from_snow(description_adf),
                "change_hour": extract_change_hour_from_snow(description_adf),
                "sprint_id": oldest_sprint_id
            }

            if current_status == "DONE":
                done_list.append(ticket_obj)
            elif current_status == "READY FOR PRODUCTION":
                ready_prod_list.append(ticket_obj)
            else:
                if sprint_name not in candidates_groups:
                    candidates_groups[sprint_name] = []
                candidates_groups[sprint_name].append(ticket_obj)

        # 🟢 ORDENAÇÃO DE CANDIDATES (Sprints)
        sorted_candidate_group_names = sorted(
            candidates_groups.keys(),
            key=lambda name: min(t["sprint_id"] for t in candidates_groups[name])
        )

        formatted_candidates = [
            {"version": name, "tickets": candidates_groups[name]}
            for name in sorted_candidate_group_names
        ]

        # 🟢 AGRUPAMENTO E ORDENAÇÃO DE READY FOR PRODUCTION (Agrupado por ALM Call)
        ready_prod_groups = {}
        for ticket in ready_prod_list:
            alm_call_val = ticket.get("alm_call", "—")
            group_name = alm_call_val if (alm_call_val and alm_call_val != "—") else "To Be Defined"

            if group_name not in ready_prod_groups:
                ready_prod_groups[group_name] = []
            ready_prod_groups[group_name].append(ticket)

        # Ordena os cabeçalhos de deploy por ordem cronológica (To Be Defined por último)
        sorted_ready_group_names = sorted(ready_prod_groups.keys(), key=sort_alm_call_groups)

        formatted_ready_prod = []
        for name in sorted_ready_group_names:
            # Ordena os tickets de cada grupo internamente por 'Change Hour' de forma crescente
            sorted_tickets_in_group = sorted(
                ready_prod_groups[name],
                key=lambda t: parse_date_for_sorting(t.get("change_hour"))
            )
            formatted_ready_prod.append({
                "version": name,
                "tickets": sorted_tickets_in_group
            })

                # Geração dinâmica de dados do Gráfico de Linhas (ALM Call)
        chart_counts = {}
        for ticket in ready_prod_list:
            alm_call_val = ticket.get("alm_call")
            if alm_call_val and alm_call_val != "—":
                chart_counts[alm_call_val] = chart_counts.get(alm_call_val, 0) + 1

        sorted_chart_labels = sorted(chart_counts.keys(), key=parse_date_for_sorting)
        sorted_chart_counts = [chart_counts[label] for label in sorted_chart_labels]

        chart_data = {
            "labels": sorted_chart_labels,
            "counts": sorted_chart_counts
        }

        # 🟢 NOVO: Geração dinâmica de dados do Gráfico de Candidates por Status
        candidates_status_counts = {}
        for sprint_name, tickets in candidates_groups.items():
            for ticket in tickets:
                status = ticket.get("status", "UNKNOWN")
                candidates_status_counts[status] = candidates_status_counts.get(status, 0) + 1

        # Ordena as labels dos status alfabeticamente
        sorted_status_labels = sorted(candidates_status_counts.keys())
        sorted_status_counts = [candidates_status_counts[label] for label in sorted_status_labels]

        candidates_chart_data = {
            "labels": sorted_status_labels,
            "counts": sorted_status_counts
        }

        if not formatted_candidates:
            formatted_candidates = [
                {
                    "version": f"{JIRA_PROJECT_KEY} - No Version Defined",
                    "tickets": []
                }
            ]

        if not formatted_ready_prod:
            formatted_ready_prod = [
                {
                    "version": "To Be Defined",
                    "tickets": []
                }
            ]

        # Retorna ambos os dados de gráfico
        return formatted_candidates, done_list, formatted_ready_prod, chart_data, candidates_chart_data

    except Exception as error:
        print(f"⚠️ Error connecting to Jira API: {error}")
        error_group = [
            {
                "version": f"{JIRA_PROJECT_KEY or 'CRMB2B'} - No Version Defined",
                "tickets": []
            }
        ]
        return error_group, [], [], {"labels": [], "counts": []}, {"labels": [], "counts": []}


# =====================================================================
# 🌐 FLASK ROUTING & ACTION ENDPOINTS
# =====================================================================
@app.route("/")
@login_required
def home():
    candidates_data, done_data, ready_prod_data, chart_data, candidates_chart_data = fetch_jira_data()  # 🟢 Atualizado

    return render_template(
        "index.html",
        candidates=candidates_data,
        ready_prod_tickets=ready_prod_data,  # 🟢 Passando dinamicamente do Jira
        done_tickets=done_data,
        chart_data=chart_data,
        candidates_chart_data=candidates_chart_data,
        jira_url=JIRA_URL or "https://atlassian.net"
    )


@app.route("/api/refresh", methods=["GET"])
@login_required
def api_refresh():
    candidates_data, done_data, ready_prod_data, chart_data, candidates_chart_data = fetch_jira_data()  # 🟢 Atualizado
    return jsonify({
        "success": True,
        "candidates": candidates_data,
        "ready_prod_tickets": ready_prod_data,  # 🟢 Retornando dinamicamente para o JS
        "done_tickets": done_data,
        "chart_data": chart_data,
        "candidates_chart_data": candidates_chart_data
    })



@app.route("/publish-page/<ticket_id>", methods=["POST"])
@login_required
def publish_page(ticket_id):
    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        issue = jira.issue(ticket_id)
        description_adf = issue.raw.get("fields", {}).get("description")

        alm_state, existing_url = inspect_alm_page_status(description_adf)

        if alm_state == "none":
            return jsonify({
                "success": False,
                "message": f"Ticket {ticket_id} is missing the 'ALM PAGE' Info Panel or its table."
            })

        if alm_state == "exists":
            return jsonify({
                "success": True,
                "already_exists": True,
                "url": existing_url
            })

        readable_description = (
            extract_plain_text_adf(description_adf)
            if description_adf
            else "No description"
        )
        prd_pr = extract_prd_pr_adf(description_adf)
        assignee_name = (
            issue.fields.assignee.displayName
            if issue.fields.assignee
            else "Unassigned"
        )

        ticket_data = {
            "ticket": ticket_id,
            "assignee": assignee_name,
            "description": readable_description,
            "pr_to_prod": prd_pr,
            "pr_devint": "",
            "pr_qa": "",
            "pr_uat": ""
        }

        confluence_result = publish_to_confluence(ticket_data)
        if not confluence_result.get("success"):
            return jsonify(confluence_result)

        generated_url = confluence_result.get("url")

        update_panel_table_link_adf(description_adf, "ALM PAGE", generated_url, display_text=None)
        jira_ok, jira_msg = update_jira_description_adf(ticket_id, description_adf)

        if not jira_ok:
            return jsonify({
                "success": False,
                "message": f"Confluence page created ({generated_url}), but failed to update Jira: {jira_msg}"
            })

        return jsonify({
            "success": True,
            "already_exists": False,
            "url": generated_url
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/update-page/<ticket_id>", methods=["POST"])
@login_required
def update_page(ticket_id):
    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        issue = jira.issue(ticket_id)
        description_adf = issue.raw.get("fields", {}).get("description")

        alm_state, existing_url = inspect_alm_page_status(description_adf)
        if alm_state != "exists" or not existing_url:
            return jsonify({
                "success": False,
                "message": f"Confluence ALM Page does not exist yet for ticket {ticket_id}."
            })

        attachments_to_sync = extract_deployment_results_attachments(issue.raw, description_adf)

        sync_result = sync_attachments_to_confluence(
            ticket_key=ticket_id,
            page_url=existing_url,
            attachments_to_sync=attachments_to_sync
        )
        return jsonify(sync_result)

    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/create-snow/<ticket_id>", methods=["POST"])
@login_required
def create_snow(ticket_id):
    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        issue = jira.issue(ticket_id)
        description_adf = issue.raw.get("fields", {}).get("description")

        snow_state, existing_snow_url, existing_num = inspect_snow_status(description_adf)

        if snow_state == "none":
            return jsonify({
                "success": False,
                "message": f"Ticket {ticket_id} is missing the 'SNOW' Info Panel or its table."
            })

        if snow_state == "exists":
            return jsonify({
                "success": True,
                "already_exists": True,
                "url": existing_snow_url,
                "number": existing_num
            })

        readable_description = (
            extract_plain_text_adf(description_adf)
            if description_adf
            else "No description"
        )
        prd_pr = extract_prd_pr_adf(description_adf)
        assignee_name = (
            issue.fields.assignee.displayName
            if issue.fields.assignee
            else "Unassigned"
        )
        summary_text = issue.fields.summary if hasattr(issue.fields, "summary") else ticket_id

        ticket_data = {
            "ticket": ticket_id,
            "summary": summary_text,
            "assignee": assignee_name,
            "description": readable_description,
            "pr_to_prod": prd_pr
        }

        snow_result = create_servicenow_change(ticket_data)
        if not snow_result.get("success"):
            return jsonify(snow_result)

        generated_snow_url = snow_result.get("url")
        chg_number = snow_result.get("number", "View SNOW")

        update_panel_table_link_adf(description_adf, "SNOW", generated_snow_url, display_text=chg_number)
        jira_ok, jira_msg = update_jira_description_adf(ticket_id, description_adf)

        if not jira_ok:
            return jsonify({
                "success": False,
                "message": f"ServiceNow CHG created ({generated_snow_url}), but failed to update Jira: {jira_msg}"
            })

        return jsonify({
            "success": True,
            "already_exists": False,
            "url": generated_snow_url,
            "number": chg_number
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


if __name__ == "__main__":
    app.run(debug=True)
