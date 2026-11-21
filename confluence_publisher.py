import json
import os
import re
import requests
from requests.auth import HTTPBasicAuth

# Load environment variables
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFLUENCE_SPACE_ID = os.getenv("CONFLUENCE_SPACE_ID", "1048580")
CONFLUENCE_SPACE_KEY = os.getenv("CONFLUENCE_SPACE_KEY", "CRMB2B")
JIRA_URL = (os.getenv("JIRA_URL") or "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")


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
    Loads 'novo 26.json', compiles real ticket data, and creates a page on Confluence.
    """
    template_path = os.path.join(BASE_DIR, "novo 26.json")

    if not os.path.exists(template_path):
        return {"success": False, "message": f"Template file '{template_path}' not found."}

    try:
        with open(template_path, "r", encoding="utf-8") as f:
            adf_document = json.load(f)

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
            page_data = response.json()
            page_url = f"{JIRA_URL}/wiki{page_data.get('_links', {}).get('webui')}"
            return {"success": True, "url": page_url}
        else:
            return {
                "success": False,
                "message": f"Confluence API returned status code {response.status_code}: {response.text}"
            }

    except Exception as e:
        return {"success": False, "message": str(e)}


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
