import os
from flask import Blueprint, jsonify, request, session
from jira import JIRA
from app.blueprints.auth.routes import login_required
from app.utils.adf_helpers import (
    extract_plain_text_adf,
    extract_prd_pr_adf,
    extract_deployment_results_attachments,
    inspect_alm_page_status,
    inspect_snow_status,
    update_panel_table_link_adf,
)
from app.services.jira_service import (
    update_jira_description_adf,
    append_technical_activities_to_jira_issue,
    sync_ticket_prs_from_github
)
from app.services.confluence_service import (
    publish_to_confluence,
    sync_attachments_to_confluence,
)
from app.services.servicenow_service import create_servicenow_change

actions_bp = Blueprint("actions", __name__)

JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")


@actions_bp.route("/publish-page/<ticket_id>", methods=["POST"])
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


@actions_bp.route("/update-page/<ticket_id>", methods=["POST"])
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


@actions_bp.route("/create-snow/<ticket_id>", methods=["POST"])
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


@actions_bp.route("/api/add-technical-activities", methods=["POST"])
@login_required
def api_add_technical_activities():
    data = request.get_json() or {}
    ticket_key = data.get("ticket")

    if not ticket_key:
        return jsonify({"success": False, "error": "Ticket não informado."}), 400

    try:
        user_dict = session.get("user") or {}
        gh_user = (
            user_dict.get("github")
            or session.get("github_username")
            or session.get("github_user")
            or ""
        )
        append_technical_activities_to_jira_issue(ticket_key, github_username=gh_user)
        return jsonify({"success": True, "message": f"Technical Activities adicionado ao ticket {ticket_key}!"})
    except Exception as e:
        print(f"⚠️ Erro ao adicionar Technical Activities: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@actions_bp.route("/api/sync-prs/<ticket_id>", methods=["POST"])
@login_required
def api_sync_prs(ticket_id):
    result = sync_ticket_prs_from_github(ticket_id)
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code