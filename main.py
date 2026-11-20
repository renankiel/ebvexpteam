import os
import re
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify
from jira import JIRA
from confluence_publisher import publish_to_confluence

# Absolute path finder to ensure .env is correctly loaded
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
env_path = os.path.join(BASE_DIR, ".env")
env_path_txt = os.path.join(BASE_DIR, ".env.txt")

# Double guard loading mechanism
if os.path.exists(env_path):
    print(f"✅ Found .env file at: {env_path}")
    load_dotenv(env_path)
elif os.path.exists(env_path_txt):
    print(f"ℹ️ Found .env.txt file at: {env_path_txt}. Loading...")
    load_dotenv(env_path_txt)
else:
    print(f"❌ WARNING: No .env or .env.txt found in: {BASE_DIR}")

app = Flask(__name__)

# =====================================================================
# 🔑 CREDENTIALS LOADED DYNAMICALLY FROM YOUR .ENV FILE
# =====================================================================
JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

# Statuses defined in your Jira workflow for the Candidates tab
STATUS_CANDIDATES = [
    "DEV BACKLOG",
    "ON HOLD",
    "IN DEV",
    "DEV COMPLETE",
    "READY FOR TEST",
    "IN TEST"
]

# Type standardization (in case Jira returns values in Portuguese)
TYPE_TRANSLATION = {
    "história": "Story",
    "historia": "Story",
    "tarefa": "Task",
    "subtarefa": "Subtask",
    "bug": "Bug",
    "erro": "Bug"
}

# =====================================================================
# 📌 STATIC DATA (For tabs/charts not yet connected to live APIs)
# =====================================================================
READY_FOR_PRODUCTION_TICKETS = [
    {
        "snow": "CHG1326437",
        "ticket": "CRMB2B-31284",
        "type": "Story",
        "cloud": "Sales",
        "status": "Ready For Production",
        "alm_page": "CRMB2B-31284",
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

# Horizontal Bar Chart Data
CHART_DATA = {
    "labels": ["Today", "Tomorrow", "Day After", "In 3 Days", "In 4 Days"],
    "counts": [2, 3, 4, 1, 5]
}


# =====================================================================
# 🧩 ADF PARSING & READING FUNCTIONS (NO REGEX!)
# =====================================================================
def extract_plain_text_adf(adf_node):
    """
    Recursively traverses any ADF JSON block and merges all text elements.
    Used both to read cell values and to generate clean summaries for the UI.
    """
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
    """
    Navigates the ADF JSON tree searching for a 'table' node.
    Identifies the 'PRD' column header and returns the value of the LAST data row.
    """
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


# =====================================================================
# 🔄 JIRA REST API v3 INTEGRATION (NATIVE ADF JSON)
# =====================================================================
def fetch_jira_data():
    """
    Connects to Jira REST API v3 to receive descriptions in native ADF JSON format,
    extracting structured table fields while preserving raw ADF for Confluence.
    """
    candidates_groups = {}
    done_list = []

    if not JIRA_URL or not JIRA_EMAIL or not JIRA_TOKEN or not JIRA_PROJECT_KEY:
        print("⚠️ Error: Jira credentials are missing or incomplete in your .env file!")
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

            ticket_obj = {
                "snow": "",
                "ticket": issue.key,
                "type": formatted_type,
                "cloud": cloud_val,
                "status": current_status,
                "description": readable_description,
                "description_adf": description_adf,
                "alm_page": issue.key,
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
    Endpoint triggered via AJAX to fetch fresh ticket details from Jira
    and publish them automatically to Confluence using the template JSON.
    """
    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )
        
        # 1. Fetch live issue details
        issue = jira.issue(ticket_id)
        description_adf = issue.raw.get("fields", {}).get("description")
        
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
        
        # Build dataset for publisher
        ticket_data = {
            "ticket": ticket_id,
            "assignee": assignee_name,
            "description": readable_description,
            "pr_to_prod": prd_pr,
            "pr_devint": "",  # Placeholders ready for expansion
            "pr_qa": "",
            "pr_uat": ""
        }
        
        # 2. Trigger Publisher
        result = publish_to_confluence(ticket_data)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


if __name__ == "__main__":
    app.run(debug=True)
