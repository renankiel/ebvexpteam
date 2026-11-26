import os
import re
import json
import copy
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from app.utils.adf_helpers import extract_plain_text_adf

load_dotenv()

CONFLUENCE_SPACE_ID = os.getenv("CONFLUENCE_SPACE_ID", "1048580")
CONFLUENCE_SPACE_KEY = os.getenv("CONFLUENCE_SPACE_KEY")
CONFLUENCE_TECH_ACTIVITIES_ID = os.getenv("CONFLUENCE_TECH_ACTIVITIES_ID", "950273")
JIRA_URL = (os.getenv("JIRA_URL") or "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
CONFLUENCE_TEMPLATE_ID = os.getenv("CONFLUENCE_TEMPLATE_ID", "491523")

_TEMPLATE_CACHE = {}
_TECH_ACTIVITIES_CACHE = None


def get_confluence_template(template_id=None, force_refresh=False):
    global _TEMPLATE_CACHE
    tid = str(template_id or CONFLUENCE_TEMPLATE_ID)

    if not force_refresh and tid in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[tid]

    url = f"{JIRA_URL}/wiki/rest/api/template/{tid}"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, auth=auth, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            template_body = data.get("body", {}).get("storage", {}).get("value")
            if not template_body:
                template_body = data.get("body", {}).get("editor", {}).get("value") or data.get("body")

            template_info = {
                "id": data.get("templateId"),
                "name": data.get("name"),
                "body": template_body
            }
            _TEMPLATE_CACHE[tid] = template_info
            return template_info
        return None
    except Exception as e:
        print(f"⚠️ Exceção ao conectar no Confluence: {e}")
        return None


def get_confluence_template_data(template_id=None, force_refresh=False):
    global _TEMPLATE_CACHE
    tid = str(template_id or CONFLUENCE_TEMPLATE_ID)

    if not force_refresh and tid in _TEMPLATE_CACHE and "content" in _TEMPLATE_CACHE[tid]:
        return copy.deepcopy(_TEMPLATE_CACHE[tid])

    url = f"{JIRA_URL}/wiki/rest/api/template/{tid}?expand=body.atlas_doc_format"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    resp = requests.get(url, auth=auth, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Erro ao buscar template {tid} no Confluence (Status {resp.status_code}): {resp.text}")

    data = resp.json()
    body = data.get("body", {})
    adf_val = body.get("atlas_doc_format", {}).get("value")
    if adf_val:
        adf_dict = json.loads(adf_val) if isinstance(adf_val, str) else adf_val
        result = {
            "representation": "atlas_doc_format",
            "content": adf_dict
        }
        _TEMPLATE_CACHE[tid] = result
        return copy.deepcopy(result)

    raise ValueError(f"O template {tid} foi encontrado no Confluence, mas seu formato ADF veio vazio.")


def replace_placeholders_in_adf(node, repl_dict):
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


def set_confluence_page_full_width(page_id):
    if not page_id:
        return

    prop_url = f"{JIRA_URL}/wiki/api/v2/pages/{page_id}/properties"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    properties_to_set = [
        {"key": "content-appearance-published", "value": "full-width"},
        {"key": "content-appearance-draft", "value": "full-width"}
    ]

    for prop in properties_to_set:
        try:
            requests.post(prop_url, json=prop, headers=headers, auth=auth, timeout=10)
        except Exception as e:
            print(f"⚠️ Erro ao aplicar propriedade {prop['key']} na página {page_id}: {e}")


def publish_to_confluence(ticket_data):
    try:
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
            created_page_data = response.json()
            created_page_id = created_page_data.get("id")
            if created_page_id:
                try:
                    set_confluence_page_full_width(created_page_id)
                except Exception as e:
                    print(f"⚠️ Não foi possível aplicar layout Wide: {e}")

            page_url = f"{JIRA_URL}/wiki{created_page_data.get('_links', {}).get('webui')}"
            return {"success": True, "url": page_url}
        else:
            return {
                "success": False,
                "message": f"Confluence API returned status code {response.status_code}: {response.text}"
            }

    except Exception as e:
        return {"success": False, "message": str(e)}


def resolve_confluence_page_id(page_url, ticket_key):
    if page_url:
        match = re.search(r"/pages/(\d+)", page_url)
        if match:
            return match.group(1)

        match_param = re.search(r"[?&]pageId=(\d+)", page_url)
        if match_param:
            return match_param.group(1)

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
    endpoint = f"{JIRA_URL}/wiki/rest/api/content/{attachment_id}"
    resp = requests.delete(
        endpoint,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        timeout=20
    )
    return resp.status_code in [200, 204]


def upload_attachment_to_confluence(page_id, filename, file_bytes, mime_type="application/octet-stream"):
    endpoint = f"{JIRA_URL}/wiki/rest/api/content/{page_id}/child/attachment"
    headers = {"X-Atlassian-Token": "nocheck"}
    files = {"file": (filename, file_bytes, mime_type or "application/octet-stream")}

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

            if filename in existing_attachments:
                old_att_id = existing_attachments[filename]
                delete_confluence_attachment(old_att_id)

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
        return {"success": False, "message": str(e)}


def get_tech_activities_content_from_confluence(force_refresh=False):
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
    page_content = adf_doc.get("content", [])
    if not page_content:
        raise ValueError("A página do Confluence não possui conteúdo no seu documento ADF.")

    _TECH_ACTIVITIES_CACHE = page_content
    return copy.deepcopy(page_content)


def parse_confluence_approvals(page_id):
    statuses = {"DEVINT": False, "QA": False, "UAT": False}
    if not page_id:
        return statuses

    try:
        base_url = (JIRA_URL or "").rstrip("/")
        url = f"{base_url}/wiki/api/v2/pages/{page_id}"
        headers = {"Accept": "application/json"}

        resp = requests.get(
            url,
            params={"body-format": "atlas_doc_format"},
            headers=headers,
            auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
            timeout=15
        )

        if resp.status_code != 200:
            return statuses

        page_data = resp.json()
        body_val = page_data.get("body", {}).get("atlas_doc_format", {}).get("value", None)

        adf_body = None
        if isinstance(body_val, str):
            try:
                adf_body = json.loads(body_val)
            except Exception:
                pass
        elif isinstance(body_val, dict):
            adf_body = body_val

        if not isinstance(adf_body, dict):
            return statuses

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

        for table in tables:
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
                if len(data_rows) > 1 and len(data_rows[1]) > status_col_idx:
                    statuses["UAT"] = is_cell_approved(data_rows[1][status_col_idx])
                if len(data_rows) > 2 and len(data_rows[2]) > status_col_idx:
                    statuses["QA"] = is_cell_approved(data_rows[2][status_col_idx])
                if len(data_rows) > 3 and len(data_rows[3]) > status_col_idx:
                    statuses["DEVINT"] = is_cell_approved(data_rows[3][status_col_idx])
                break

    except Exception as e:
        print(f"⚠️ [DEBUG SIGNOFF] Exceção durante parsing: {e}")

    return statuses
