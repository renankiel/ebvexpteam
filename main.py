import os
import re
from dotenv import load_dotenv
from flask import Flask, render_template
from jira import JIRA

# 🟢 Absolute path finder to ensure .env is correctly loaded
# This guarantees that the .env file is found even if you run the terminal from another folder!
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

# Credentials prepared for future integrations:
CONFLUENCE_URL = os.getenv("CONFLUENCE_URL")
CONFLUENCE_EMAIL = os.getenv("CONFLUENCE_EMAIL")
CONFLUENCE_TOKEN = os.getenv("CONFLUENCE_TOKEN")

SERVICENOW_URL = os.getenv("SERVICENOW_URL")
SERVICENOW_USER = os.getenv("SERVICENOW_USER")
SERVICENOW_PASSWORD = os.getenv("SERVICENOW_PASSWORD")

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

    # If the current node is a 'text' type, return its text value
    if adf_node.get("type") == "text":
        return adf_node.get("text", "")

    # Otherwise, iterate through child elements inside 'content'
    text_pieces = []
    for child in adf_node.get("content", []):
        piece = extract_plain_text_adf(child)
        if piece:
            text_pieces.append(piece)

    # Use a pipeline separator for rows/documents, and spaces for standard text blocks
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
        # 1. Get all table blocks inside the description
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

                # Check if this is a header row (contains tableHeader)
                cell_types = [c.get("type") for c in cells]
                if "tableHeader" in cell_types:
                    for idx, cell in enumerate(cells):
                        header_text = extract_plain_text_adf(cell).upper()
                        if "PRD" in header_text:
                            prd_column_index = idx
                else:
                    # This is a standard data row (tableCell)
                    data_rows.append(cells)

            # 2. If data rows exist, always extract from the very LAST row [-1]
            if data_rows:
                last_row_cells = data_rows[-1]

                # Use PRD index if found; fallback to column 4 [3] or the last cell
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

    # Verify if credentials from .env are fully populated
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
        # Enforce API v3 to native-retrieve description as an ADF JSON dictionary
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        jql_query = f'project = "{JIRA_PROJECT_KEY}" ORDER BY created DESC'
        issues = jira.search_issues(jql_query, maxResults=100)

        for issue in issues:
            # 1. Status Mapping (Normalized to uppercase)
            current_status = issue.fields.status.name.upper()

            # 2. Standardize Issue Types to English (Story, Task, Bug)
            native_type = issue.fields.issuetype.name
            formatted_type = TYPE_TRANSLATION.get(native_type.lower(), native_type)

            # 3. Handle Assignees
            assignee_name = (
                issue.fields.assignee.displayName
                if issue.fields.assignee
                else "Unassigned"
            )

            # 4. Determine Cloud (Scan label array for 'Service', default to 'Sales')
            labels_lower = [lbl.lower() for lbl in (issue.fields.labels or [])]
            cloud_val = "Service" if "service" in labels_lower else "Sales"

            # 5. Extract Native ADF Description from 'raw' API v3 fields
            description_adf = issue.raw.get("fields", {}).get("description")

            # Convert ADF to plain text exclusively for UI description cell
            readable_description = (
                extract_plain_text_adf(description_adf)
                if description_adf
                else "No description"
            )

            # 6. Dynamically extract the last PRD PR from the ADF table structure (Matches corrected name)
            prd_pr = extract_prd_pr_adf(description_adf)

            # 7. Map fields into standard template format
            ticket_obj = {
                "snow": "",                                         # Hard-coded for now
                "ticket": issue.key,                                # From Jira API
                "type": formatted_type,                             # From Jira API (standardized)
                "cloud": cloud_val,                                 # Labels check / Fallback
                "status": current_status,                           # From Jira API
                "description": readable_description,                # Cleaned description text
                "description_adf": description_adf,                 # Raw ADF JSON for future Confluence push
                "alm_page": issue.key,                              # From Jira API
                "assignee": assignee_name,                          # From Jira API
                "manual": "No",                                     # Hard-coded
                "logs": "No",                                       # Hard-coded
                "pr_to_prod": prd_pr,                               # 🟢 Dynamic ADF parsed PR!
                "pr_validated": "Yes" if prd_pr else "No",          # Validated automatically if PRD is found
                "dev_signoff": "Yes" if current_status in ["DEV COMPLETE", "READY FOR TEST", "IN TEST", "DONE"] else "No",
                "qa_signoff": "Yes" if current_status == "DONE" else "No",
                "uat_signoff": "Yes" if current_status == "DONE" else "No",
                "execution_order": "",                              # Hard-coded
                "change_hour": ""                                   # Hard-coded
            }

            # Route 'DONE' tickets to Done tab, all others to Candidates tab
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

        # Structure candidates list as expected by HTML templates
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
# 🌐 FLASK ROUTING
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
        jira_url=JIRA_URL or "https://atlassian.net"  # Safe fallback
    )


if __name__ == "__main__":
    app.run(debug=True)
