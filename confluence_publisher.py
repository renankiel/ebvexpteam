import json
import os
import requests
from requests.auth import HTTPBasicAuth

# Load environment variables
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFLUENCE_SPACE_ID = os.getenv("CONFLUENCE_SPACE_ID", "1048580")
CONFLUENCE_SPACE_KEY = os.getenv("CONFLUENCE_SPACE_KEY", "CRMB2B")
JIRA_URL = os.getenv("JIRA_URL")
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
            # Look for exact or partial matches to replace
            for placeholder, replacement in repl_dict.items():
                if placeholder in text_val:
                    node["text"] = text_val.replace(placeholder, str(replacement))
        
        # Traverse link marks to update destination URLs
        if "marks" in node:
            for mark in node["marks"]:
                if mark.get("type") == "link" and "attrs" in mark and "href" in mark["attrs"]:
                    href_val = mark["attrs"]["href"]
                    for placeholder, replacement in repl_dict.items():
                        if placeholder in href_val:
                            mark["attrs"]["href"] = href_val.replace(placeholder, str(replacement))

        # Recursively scan children nodes
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
        # 1. Read the ADF template
        with open(template_path, "r", encoding="utf-8") as f:
            adf_document = json.load(f)
            
        # 2. Build the replacements map
        replacements = {
            "CRMB2B-30977": ticket_data.get("ticket", ""),
            "Renan Kiel": ticket_data.get("assignee", "Unassigned"),
            "Pipeline Health Agent Deployment (Saúde Pipe P1)": ticket_data.get("description", "Reason for change"),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20297": ticket_data.get("pr_to_prod", ""),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20296": ticket_data.get("pr_devint", ""),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20295": ticket_data.get("pr_qa", ""),
            "https://github.com/Equifax/3519_US_Salesforce4BIZ_CSA/pull/20298": ticket_data.get("pr_uat", ""),
        }
        
        # 3. Apply the replacements to the copy of ADF document
        replace_placeholders_in_adf(adf_document, replacements)
        
        # 4. Prepare Confluence API Endpoint (REST API v2)
        confluence_endpoint = f"{JIRA_URL}/wiki/api/v2/pages"
        
        payload = {
            "spaceId": CONFLUENCE_SPACE_ID,  # 🟢 Fixed: Using the verified numeric space ID
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
        
        # 5. Send POST request using authenticated session
        response = requests.post(
            confluence_endpoint,
            json=payload,
            headers=headers,
            auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
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
