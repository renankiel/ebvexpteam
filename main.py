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
from servicenow_publisher import create_servicenow_change

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
        "snow_state": "exists",
        "snow_url": "https://dev489662.service-now.com/nav_to.do?uri=change_request.do?sysparm_query=number=CHG1326437",
        "snow_number": "CHG1326437",
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
# 🧩 ADF PARSING, TABLE INSPECTION & UPDATING FUNCTIONS
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
    """
    Recursively searches inside an ADF node for a link:
    1. 'inlineCard' -> returns (attrs.url, last_url_segment)
    2. 'text' with 'link' mark -> returns (mark.attrs.href, node.text)
    3. Raw 'text' starting with http:// or https:// -> returns (raw_text, raw_text)
    """
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


def find_panel_table(adf_description, panel_keyword):
    """
    Finds the Info Panel matching 'panel_keyword' (e.g., 'ALM PAGE' or 'SNOW')
    and returns the sibling 'table' block that immediately follows it in the ADF document.
    """
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


def inspect_alm_page_status(description_adf):
    """
    Checks if both the 'ALM PAGE' Info Panel and its sibling table exist.
    Returns: ('none', '') | ('ready', '') | ('exists', url)
    """
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
    """
    Checks if both the 'SNOW' Info Panel and its sibling table exist.
    Returns: ('none', '', '') | ('ready', '', '') | ('exists', url, display_text)
    """
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


def update_panel_table_link_adf(adf_description, panel_keyword, new_url, display_text=None):
    """Updates the 'LINK' cell of the table following the Info Panel."""
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

            # 🟢 MANUAL? Label Evaluation
            has_pull_request = "pull-request" in labels_lower
            has_manually = "manually" in labels_lower

            if has_pull_request:
                # If pull-request exists (even if manually also exists) -> 'No'
                manual_val = "No"
            elif has_manually:
                # If only manually exists -> 'Yes'
                manual_val = "Yes"
            else:
                # If neither exists -> Dash
                manual_val = "—"

            description_adf = issue.raw.get("fields", {}).get("description")

            readable_description = (
                extract_plain_text_adf(description_adf)
                if description_adf
                else "No description"
            )

            prd_pr = extract_prd_pr_adf(description_adf)

            # 🟢 Apply 'N/A' overrides when MANUAL? is 'Yes'
            if manual_val == "Yes":
                logs_val = "N/A"
                pr_to_prod_val = "N/A"
                pr_validated_val = "N/A"
            else:
                logs_val = "No"
                pr_to_prod_val = prd_pr
                pr_validated_val = "Yes" if prd_pr else "No"

            # Inspect ALM PAGE and SNOW panels + sibling tables
            alm_state, alm_url = inspect_alm_page_status(description_adf)
            snow_state, snow_url, snow_num = inspect_snow_status(description_adf)

            ticket_obj = {
                "snow_state": snow_state,
                "snow_url": snow_url,
                "snow_number": snow_num or "View SNOW",
                "ticket": issue.key,
                "type": formatted_type,
                "cloud": cloud_val,
                "status": current_status,
                "description": readable_description,
                "description_adf": description_adf,
                "alm_page_state": alm_state,
                "alm_page_url": alm_url,
                "assignee": assignee_name,
                "manual": manual_val,
                "logs": logs_val,
                "pr_to_prod": pr_to_prod_val,
                "pr_validated": pr_validated_val,
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


@app.route("/create-snow/<ticket_id>", methods=["POST"])
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
