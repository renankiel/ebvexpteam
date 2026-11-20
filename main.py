import os
import re
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify
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

from confluence_publisher import publish_to_confluence

app = Flask(__name__)

# =====================================================================
# 🔑 CREDENTIALS LOADED DYNAMICALLY FROM YOUR .ENV FILE
# =====================================================================
JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

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
        "snow": "CHG1326437",
        "ticket": "CRMB2B-31284",
        "type": "Story",
        "cloud": "Sales",
        "status": "Ready For Production",
        "alm_page_state": "ready",
        "alm_page_url": "",
        "assignee": "Renan Kiel",
        "manual": "No",
        "logs": "No",
        "pr_to_prod": "20945",
        "pr_validated": "No",
        "dev_signoff": "Yes",
        "qa_signoff": "Yes",
        "uat_signoff": "Yes",
        "execution_order": "",
        "change_hour": "12/11 07:00"
    }
]

CHART_DATA = {
    "labels": ["Today", "Tomorrow", "Day After", "In 3 Days", "In 4 Days"],
    "counts": [2, 3, 4, 1, 5]
}


# =====================================================================
# 🧩 ADF PARSING & STATE EXTRACTION FUNCTIONS
# =====================================================================
def extract_plain_text_adf(adf_node):
    """Recursively traverses any ADF JSON block and merges all text elements."""
    if not isinstance(adf_node, dict):
        return ""

    if adf_node.get("type") == "text":
        return adf_node.get("text", "")

    text_pieces = []
    for child in adf_node.get("content", []):
        piece = extract_plain_text_adf(child)
        if piece:
            text_pieces.append(piece)

    separator = " | " if adf_node.get("type") in ["tableRow", "doc"] else " "
    return separator.join(text_pieces).strip()


def extract_prd_pr_adf(adf_description):
    """Extracts PRD PR value from the last row of the description table."""
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

            if data_rows:
                last_row_cells = data_rows[-1]

                if prd_column_index is not None and prd_column_index < len(last_row_cells):
                    target_cell = last_row_cells[prd_column_index]
                elif len(last_row_cells) >= 4:
                    target_cell = last_row_cells[3]
                else:
                    target_cell = last_row_cells[-1]

                prd_value = extract_plain_text_adf(target_cell).strip()
                if prd_value:
                    return prd_value

    except Exception as e:
        print(f"⚠️ Error parsing ADF table: {e}")

    return ""


def find_inline_card_url_in_node(node):
    """Finds the URL of the first 'inlineCard' inside a given node."""
    if isinstance(node, list):
        for item in node:
            url = find_inline_card_url_in_node(item)
            if url:
                return url
    elif isinstance(node, dict):
        if node.get("type") == "inlineCard":
            return node.get("attrs", {}).get("url", "")

        for child in node.get("content", []):
            url = find_inline_card_url_in_node(child)
            if url:
                return url

    return ""


def inspect_alm_page_status(description_adf):
    """
    Inspects the Jira description ADF for the ALM PAGE panel.
    Returns:
        (state, url)
        - state: 'none' (panel does not exist)
        - state: 'ready' (panel exists and URL ends with CRMB2B-0 -> ready to create)
        - state: 'exists' (panel exists and URL points to an actual page -> already created)
    """
    if not isinstance(description_adf, dict):
        return "none", ""

    for block in description_adf.get("content", []):
        if isinstance(block, dict) and block.get("type") == "panel":
            panel_text = extract_plain_text_adf(block).upper()

            if "ALM PAGE" in panel_text:
                card_url = find_inline_card_url_in_node(block).strip()

                # If URL ends with CRMB2B-0 (or placeholder), it is ready for creation
                if card_url.endswith("CRMB2B-0") or not card_url:
                    return "ready", card_url
                else:
                    # It already points to a real page!
                    return "exists", card_url

    return "none", ""


def update_inline_card_in_node(node, new_url):
    """Recursively updates the URL of an inlineCard."""
    if isinstance(node, list):
        for item in node:
            if update_inline_card_in_node(item, new_url):
                return True
    elif isinstance(node, dict):
        if node.get("type") == "inlineCard":
            if "attrs" not in node or not isinstance(node["attrs"], dict):
                node["attrs"] = {}
            node["attrs"]["url"] = new_url
            return True

        for child in node.get("content", []):
            if update_inline_card_in_node(child, new_url):
                return True

    return False


def update_alm_page_panel_adf(adf_description, new_url):
    """Updates the ALM PAGE Info Panel inlineCard with the new Confluence URL."""
    if not isinstance(adf_description, dict):
        return False

    for block in adf_description.get("content", []):
        if isinstance(block, dict) and block.get("type") == "panel":
            panel_text = extract_plain_text_adf(block).upper()

            if "ALM PAGE" in panel_text:
                if update_inline_card_in_node(block, new_url):
                    return True

                panel_content = block.setdefault("content", [])
                inline_card_paragraph = {
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

                if len(panel_content) >= 2:
                    panel_content[1] = inline_card_paragraph
                else:
                    panel_content.append(inline_card_paragraph)

                return True

    return False


def update_jira_description_adf(ticket_id, updated_adf):
    """Overwrites Jira issue description with the updated ADF JSON."""
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


# =====================================================================
# 🔄 JIRA REST API v3 INTEGRATION
# =====================================================================
def fetch_jira_data():
    candidates_groups = {}
    done_list = []

    if not JIRA_URL or not JIRA_EMAIL or not JIRA_TOKEN or not JIRA_PROJECT_KEY:
        print("⚠️ Error: Jira credentials missing or incomplete in .env!")
        error_group = [
            {
                "version": "⚠️ Error: Jira credentials missing/incomplete in .env!",
                "tickets": []
            }
        ]
        return error_group, []

    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        jql_query = f'project = "{JIRA_PROJECT_KEY}" ORDER BY created DESC'
        issues = jira.search_issues(jql_query, maxResults=100)

        for issue in issues:
            current_status = issue.fields.status.name.upper()

            native_type = issue.fields.issuetype.name
            formatted_type = TYPE_TRANSLATION.get(native_type.lower(), native_type)

            assignee_name = (
                issue.fields.assignee.displayName
                if issue.fields.assignee
                else "Unassigned"
            )

            labels_lower = [lbl.lower() for lbl in (issue.fields.labels or [])]
            cloud_val = "Service" if "service" in labels_lower else "Sales"

            description_adf = issue.raw.get("fields", {}).get("description")

            readable_description = (
                extract_plain_text_adf(description_adf)
                if description_adf
                else "No description"
            )

            prd_pr = extract_prd_pr_adf(description_adf)

            # Inspect ALM Page panel status ('none', 'ready', 'exists')
            alm_state, alm_url = inspect_alm_page_status(description_adf)

            ticket_obj = {
                "snow": "",
                "ticket": issue.key,
                "type": formatted_type,
                "cloud": cloud_val,
                "status": current_status,
                "description": readable_description,
                "description_adf": description_adf,
                "alm_page_state": alm_state,  # 🟢 'none' | 'ready' | 'exists'
                "alm_page_url": alm_url,      # 🟢 Existing Confluence link if any
                "assignee": assignee_name,
                "manual": "No",
                "logs": "No",
                "pr_to_prod": prd_pr,
                "pr_validated": "Yes" if prd_pr else "No",
                "dev_signoff": "Yes" if current_status in ["DEV COMPLETE", "READY FOR TEST", "IN TEST", "DONE"] else "No",
                "qa_signoff": "Yes" if current_status == "DONE" else "No",
                "uat_signoff": "Yes" if current_status == "DONE" else "No",
                "execution_order": "",
                "change_hour": ""
            }

            if current_status == "DONE":
                done_list.append(ticket_obj)
            else:
                if issue.fields.fixVersions:
                    version_name = issue.fields.fixVersions[0].name
                else:
                    version_name = f"{JIRA_PROJECT_KEY} - No Version Defined"

                if version_name not in candidates_groups:
                    candidates_groups[version_name] = []
                candidates_groups[version_name].append(ticket_obj)

        formatted_candidates = [
            {"version": version, "tickets": tickets}
            for version, tickets in candidates_groups.items()
        ]

        if not formatted_candidates:
            formatted_candidates = [
                {
                    "version": f"No pending issues found in project {JIRA_PROJECT_KEY}",
                    "tickets": []
                }
            ]

        return formatted_candidates, done_list

    except Exception as error:
        print(f"⚠️ Error connecting to Jira API: {error}")
        error_group = [
            {
                "version": f"⚠️ Jira connection error: {error}",
                "tickets": []
            }
        ]
        return error_group, []


# =====================================================================
# 🌐 FLASK ROUTING & ACTION ENDPOINTS
# =====================================================================
@app.route("/")
def home():
    candidates_data, done_data = fetch_jira_data()

    return render_template(
        "index.html",
        candidates=candidates_data,
        ready_prod_tickets=READY_FOR_PRODUCTION_TICKETS,
        done_tickets=done_data,
        chart_data=CHART_DATA,
        jira_url=JIRA_URL or "https://atlassian.net"
    )


@app.route("/publish-page/<ticket_id>", methods=["POST"])
def publish_page(ticket_id):
    """
    1. Fetches fresh issue details from Jira.
    2. Verifies ALM PAGE Info Panel:
       - If panel does NOT exist: rejects action.
       - If URL does NOT end with 'CRMB2B-0': page already exists! Returns existing URL.
       - If URL ends with 'CRMB2B-0': publishes to Confluence and updates Jira.
    """
    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        issue = jira.issue(ticket_id)
        description_adf = issue.raw.get("fields", {}).get("description")

        alm_state, existing_url = inspect_alm_page_status(description_adf)

        # Condition 3: No ALM PAGE panel at all
        if alm_state == "none":
            return jsonify({
                "success": False,
                "message": f"Ticket {ticket_id} does not have an 'ALM PAGE' Info Panel in its description yet."
            })

        # Condition 2: Page already exists (URL does not end in CRMB2B-0)
        if alm_state == "exists":
            return jsonify({
                "success": True,
                "already_exists": True,
                "url": existing_url
            })

        # Condition 1: URL ends with CRMB2B-0 -> proceed with creation!
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

        # Update Jira description with the new page link
        update_alm_page_panel_adf(description_adf, generated_url)
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


if __name__ == "__main__":
    app.run(debug=True)
