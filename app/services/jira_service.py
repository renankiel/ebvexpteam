import os
import re
import copy
import requests
from requests.auth import HTTPBasicAuth
from flask import session
from jira import JIRA
from dotenv import load_dotenv

from app.utils.adf_helpers import (
    extract_plain_text_adf,
    extract_prd_pr_adf,
    collect_media_nodes_from_node,
    find_panel_table,
    extract_deployment_results_attachments,
    inspect_alm_page_status,
    inspect_snow_status,
    extract_change_hour_from_snow,
    extract_execution_order_from_snow,
    extract_alm_call_from_alm_page,
    parse_date_for_sorting,
    sort_alm_call_groups,
    find_sprint_field_key,
    extract_sprints,
    extract_prd_status_adf,
    has_technical_activities_expand,
)
from app.services.confluence_service import (
    resolve_confluence_page_id,
    get_existing_confluence_attachments,
    parse_confluence_approvals,
    get_tech_activities_content_from_confluence,
)

load_dotenv()

JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

TYPE_TRANSLATION = {
    "história": "Story",
    "historia": "Story",
    "tarefa": "Task",
    "subtarefa": "Subtask",
    "bug": "Bug",
    "erro": "Bug"
}


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


def check_double_attachments(issue_raw, description_adf, alm_state, alm_url, issue_key):
    """Realiza a checagem dupla dos anexos (Jira <-> Confluence)."""
    table_node = find_panel_table(description_adf, "DEPLOYMENT RESULTS")
    if not table_node:
        return "No"

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

    jira_attachments = extract_deployment_results_attachments(issue_raw, description_adf)

    if len(jira_attachments) < len(media_attrs_list):
        return "No"

    if alm_state != "exists" or not alm_url:
        return "No"

    confluence_page_id = resolve_confluence_page_id(alm_url, issue_key)
    if not confluence_page_id:
        return "No"

    confluence_attachments = get_existing_confluence_attachments(confluence_page_id)

    for att in jira_attachments:
        filename = att.get("filename")
        if not filename or filename not in confluence_attachments:
            return "No"

    return "Yes"


def append_technical_activities_to_jira_issue(ticket_key, github_username=None):
    """
    Busca o conteúdo da página do Confluence, substitui as variáveis [eisid], [ticket], [subject],
    empacota em um nó 'expand' limpo e atualiza o ticket no Jira.
    """
    if not github_username:
        user_dict = session.get("user") or {}
        github_username = (
            user_dict.get("github")
            or user_dict.get("github_username")
            or session.get("github")
            or ""
        )

    github_username = str(github_username).strip().lstrip("@")
    print(f"🎯 [SUCESSO] GitHub ID detectado para [eisid]: '{github_username}'")

    issue_url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}?fields=description,summary"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    get_resp = requests.get(issue_url, auth=auth, headers=headers, timeout=15)
    if get_resp.status_code != 200:
        raise RuntimeError(f"Erro ao buscar issue {ticket_key} no Jira: {get_resp.status_code}")

    issue_data = get_resp.json()
    summary = issue_data.get("fields", {}).get("summary", "")
    current_desc = issue_data.get("fields", {}).get("description")

    confluence_blocks = get_tech_activities_content_from_confluence()

    replacements = {
        r"\[eisid\]": github_username,
        r"\[ticket\]": ticket_key,
        r"\[subject\]": summary
    }

    def _replace_in_text(text):
        if not isinstance(text, str):
            return text
        for pattern, rep_val in replacements.items():
            text = re.sub(pattern, str(rep_val or ""), text, flags=re.IGNORECASE)
        return text

    def _traverse_and_replace(node):
        if not node:
            return node
        if isinstance(node, list):
            return [_traverse_and_replace(item) for item in node]
        elif isinstance(node, dict):
            new_node = {}
            for k, v in node.items():
                if k == "text" and isinstance(v, str):
                    new_node[k] = _replace_in_text(v)
                elif isinstance(v, (dict, list)):
                    new_node[k] = _traverse_and_replace(v)
                else:
                    new_node[k] = v
            return new_node
        return node

    customized_blocks = _traverse_and_replace(copy.deepcopy(confluence_blocks))

    clean_expand_node = {
        "type": "expand",
        "attrs": {
            "title": "Technical Activities"
        },
        "content": customized_blocks
    }

    if not current_desc or not isinstance(current_desc, dict):
        current_desc = {"type": "doc", "version": 1, "content": []}

    clean_content = []
    for c in current_desc.get("content", []):
        if c.get("type") == "expand":
            title = c.get("attrs", {}).get("title", "")
            if "technical activities" in title.lower():
                continue
        clean_content.append(c)

    clean_content.append(clean_expand_node)
    current_desc["content"] = clean_content

    put_payload = {"fields": {"description": current_desc}}
    put_resp = requests.put(issue_url, auth=auth, headers=headers, json=put_payload, timeout=20)
    if put_resp.status_code not in (200, 204):
        raise RuntimeError(f"Erro ao atualizar ticket {ticket_key} no Jira: {put_resp.status_code} - {put_resp.text}")

    return True


def fetch_jira_data():
    candidates_groups = {}
    done_list = []
    ready_prod_list = []
    pending_ta_list = []

    if not JIRA_URL or not JIRA_EMAIL or not JIRA_TOKEN or not JIRA_PROJECT_KEY:
        print("⚠️ Error: Jira credentials missing or incomplete in .env!")
        error_group = [
            {
                "version": f"{JIRA_PROJECT_KEY or 'CRMB2B'} - No Version Defined",
                "tickets": []
            }
        ]
        return error_group, [], [], {"labels": [], "counts": []}, {"labels": [], "counts": []}, []

    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(JIRA_EMAIL, JIRA_TOKEN),
            options={"rest_api_version": "3"}
        )

        jql_query = f'project = "{JIRA_PROJECT_KEY}" ORDER BY created DESC'
        issues = jira.search_issues(jql_query, maxResults=100)

        sprint_field_key = None
        for issue in issues:
            sprint_field_key = find_sprint_field_key(issue.raw.get("fields", {}))
            if sprint_field_key:
                break

        for issue in issues:
            labels = issue.fields.labels or []
            labels_upper = [lbl.upper() for lbl in labels]
            if "BVISTA" not in labels_upper:
                continue

            current_status = issue.fields.status.name.upper()
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
            prd_status = extract_prd_status_adf(description_adf)
            alm_state, alm_url = inspect_alm_page_status(description_adf)
            logs_status = check_double_attachments(issue.raw, description_adf, alm_state, alm_url, issue.key)

            if manual_val == "Yes":
                logs_val = "N/A"
                pr_to_prod_val = "N/A"
                pr_validated_val = "N/A"
            else:
                logs_val = logs_status
                pr_to_prod_val = prd_pr if prd_pr else "—"
                pr_validated_val = prd_status

            snow_state, snow_url, snow_num = inspect_snow_status(description_adf)

            sprint_raw = issue.raw.get("fields", {}).get(sprint_field_key) if sprint_field_key else None
            sprints_list = extract_sprints(sprint_raw)

            sprint_name = f"{JIRA_PROJECT_KEY} - No Version Defined"
            oldest_sprint_id = 999999999

            if sprints_list:
                oldest_sprint = min(sprints_list, key=lambda x: x["id"])
                sprint_name = oldest_sprint["name"]
                oldest_sprint_id = oldest_sprint["id"]

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
                "status_lower_dashed": current_status.lower().replace(" ", "-"),
                "description": readable_description,
                "alm_page_state": alm_state,
                "alm_page_url": alm_url,
                "assignee": assignee_name,
                "assignee_initial": assignee_name[0] if assignee_name else "?",
                "manual": manual_val,
                "manual_lower": manual_val.lower(),
                "logs": logs_val,
                "logs_lower": logs_val.lower().replace("/", "-"),
                "pr_to_prod": pr_to_prod_val,
                "pr_validated": pr_validated_val,
                "pr_validated_lower": pr_validated_val.lower().replace("/", "-"),
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
                has_ta = has_technical_activities_expand(description_adf)
                if not has_ta:
                    pending_ta_list.append(ticket_obj)
                else:
                    if sprint_name not in candidates_groups:
                        candidates_groups[sprint_name] = []
                    candidates_groups[sprint_name].append(ticket_obj)

        sorted_candidate_group_names = sorted(
            candidates_groups.keys(),
            key=lambda name: min(t["sprint_id"] for t in candidates_groups[name])
        )

        formatted_candidates = [
            {"version": name, "tickets": candidates_groups[name]}
            for name in sorted_candidate_group_names
        ]

        ready_prod_groups = {}
        for ticket in ready_prod_list:
            alm_call_val = ticket.get("alm_call", "—")
            group_name = alm_call_val if (alm_call_val and alm_call_val != "—") else "To Be Defined"
            if group_name not in ready_prod_groups:
                ready_prod_groups[group_name] = []
            ready_prod_groups[group_name].append(ticket)

        sorted_ready_group_names = sorted(ready_prod_groups.keys(), key=sort_alm_call_groups)

        formatted_ready_prod = []
        for name in sorted_ready_group_names:
            sorted_tickets_in_group = sorted(
                ready_prod_groups[name],
                key=lambda t: parse_date_for_sorting(t.get("change_hour"))
            )
            formatted_ready_prod.append({
                "version": name,
                "tickets": sorted_tickets_in_group
            })

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

        candidates_status_counts = {}
        for sprint_name, tickets in candidates_groups.items():
            for ticket in tickets:
                status = ticket.get("status", "UNKNOWN")
                candidates_status_counts[status] = candidates_status_counts.get(status, 0) + 1

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

        return formatted_candidates, done_list, formatted_ready_prod, chart_data, candidates_chart_data, pending_ta_list

    except Exception as error:
        print(f"⚠️ Error connecting to Jira API: {error}")
        error_group = [
            {
                "version": f"{JIRA_PROJECT_KEY or 'CRMB2B'} - No Version Defined",
                "tickets": []
            }
        ]
        return error_group, [], [], {"labels": [], "counts": []}, {"labels": [], "counts": []}, []
