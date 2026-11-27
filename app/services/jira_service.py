import os
import re
import copy
import json
import requests
from requests.auth import HTTPBasicAuth
from flask import session
from jira import JIRA
from dotenv import load_dotenv
from app.utils.adf_helpers import find_panel_table

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
    find_migration_table_in_ta,
    extract_migration_prs_data
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

            migration_prs = extract_migration_prs_data(description_adf)
            # Pega o número da última PR de PRD (se existir na tabela MIGRATION ou do prd_pr antigo)
            prd_prs_list = migration_prs.get("PRD", {}).get("prs", [])
            if prd_prs_list:
                pr_to_prod_num = prd_prs_list[-1].get("number", "—")
                pr_to_prod_url = prd_prs_list[-1].get("url", "#")
            else:
                m_pr = re.search(r"(\d+)$", (prd_pr or "").strip())
                pr_to_prod_num = m_pr.group(1) if m_pr else (prd_pr if prd_pr else "—")
                pr_to_prod_url = prd_pr if (prd_pr and prd_pr.startswith("http")) else "#"

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
                "pr_to_prod_num": "N/A" if manual_val == "Yes" else pr_to_prod_num,
                "pr_to_prod_url": pr_to_prod_url,
                "migration_prs": migration_prs,
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


def sync_ticket_prs_from_github(ticket_key):
    """
    1. Consulta as Pull Requests atreladas ao ticket na Dev Status API do Jira.
    2. Identifica o ambiente pela branch de destino (DEVINT, QA, UAT, PRD/PROD).
    3. Localiza a tabela abaixo do Info Panel 'MIGRATION' dentro do Expand 'Technical Activities'.
    4. Atualiza o elemento Status no cabeçalho de cada ambiente:
       - Quando ainda não existia PR no Jira e a PR está OPEN -> VALIDATING
       - Quando a PR está CLOSED ou MERGED -> DEPLOYING
    5. Grava os números/links das PRs nas linhas da tabela (criando novas linhas quando houver
       mais de uma PR para o mesmo ambiente).
    """
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # 1. Busca id interno + description ADF atual do ticket
    issue_url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}?fields=id,description"
    resp = requests.get(issue_url, auth=auth, headers=headers, timeout=15)
    if resp.status_code != 200:
        return {"success": False, "message": f"Erro ao buscar ticket {ticket_key} no Jira (Status {resp.status_code})."}

    issue_data = resp.json()
    issue_id = issue_data.get("id")
    description_adf = issue_data.get("fields", {}).get("description")

    if not description_adf:
        return {"success": False, "message": f"O ticket {ticket_key} não possui descrição ADF."}

    table_node = find_migration_table_in_ta(description_adf)
    if not table_node:
        return {
            "success": False,
            "message": f"Tabela 'MIGRATION' dentro de 'Technical Activities' não encontrada no ticket {ticket_key}."
        }

    # 2. Consulta Dev Status API do Jira para buscar as PRs do GitHub
    dev_status_url = f"{JIRA_URL}/rest/dev-status/latest/issue/detail"
    params = {
        "issueId": issue_id,
        "applicationType": "oAuth-com.github.integration.production",
        "dataType": "pullrequest"
    }
    dev_resp = requests.get(dev_status_url, params=params, auth=auth, headers=headers, timeout=15)
    if dev_resp.status_code != 200:
        return {"success": False, "message": f"Erro na Dev Status API (Status {dev_resp.status_code})."}

    pull_requests = []
    for app_detail in dev_resp.json().get("detail", []):
        pull_requests.extend(app_detail.get("pullRequests", []) or app_detail.get("pullrequests", []))

    # 3. Agrupa PRs por ambiente de destino (DEVINT, QA, UAT, PRD)
    env_prs = {"DEVINT": [], "QA": [], "UAT": [], "PRD": []}

    for pr in pull_requests:
        pr_id_raw = str(pr.get("id", "")).strip().lstrip("#")
        pr_url = pr.get("url", "").strip()
        pr_status = str(pr.get("status", "")).upper().strip()  # OPEN, MERGED, DECLINED, CLOSED
        pr_name = str(pr.get("name", "")).upper()

        dest_branch = (
            pr.get("destinationBranch")
            or pr.get("targetBranch")
            or pr.get("toBranch")
            or (pr.get("destination") or {}).get("branch")
            or (pr.get("target") or {}).get("branch")
            or ""
        )
        if isinstance(dest_branch, dict):
            dest_branch = dest_branch.get("name", "")
        dest_upper = str(dest_branch).upper()

        # Identifica qual dos 4 ambientes é o destino (pela branch ou pelo sufixo do título caso branch venha vazia)
        target_env = None
        for candidate in ["DEVINT", "QA", "UAT", "PRD", "PROD"]:
            if candidate in dest_upper or pr_name.endswith(f"-{candidate}") or f"-{candidate}-" in pr_name:
                target_env = "PRD" if candidate == "PROD" else candidate
                break

        if target_env:
            env_prs[target_env].append({
                "number": pr_id_raw or "PR",
                "url": pr_url,
                "status": pr_status
            })

    # 4. Mapeia colunas da tabela MIGRATION e verifica quais PRs já existiam antes no Jira
    existing_migration = extract_migration_prs_data(description_adf)

    rows = table_node.get("content", [])
    header_row = None
    col_env_map = {}  # {col_idx: 'DEVINT' | 'QA' | 'UAT' | 'PRD'}
    num_cols = 4

    for row in rows:
        if isinstance(row, dict) and row.get("type") == "tableRow":
            cells = row.get("content", [])
            cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
            if "tableHeader" in cell_types:
                header_row = row
                num_cols = len(cells)
                for idx, cell in enumerate(cells):
                    h_text = extract_plain_text_adf(cell).upper()
                    for cand in ["DEVINT", "QA", "UAT", "PRD", "PROD"]:
                        if cand in h_text:
                            col_env_map[idx] = "PRD" if cand == "PROD" else cand
                            break
                break

    if not header_row or not col_env_map:
        return {"success": False, "message": "Cabeçalho da tabela MIGRATION não possui as colunas DEVINT, QA, UAT, PRD."}

    # Helper para atualizar o elemento Status dentro do tableHeader da coluna
    STATUS_COLORS = {
        "NOT STARTED": "neutral",
        "VALIDATING": "yellow",
        "STAND BY": "purple",
        "APPROVING": "blue",
        "DEPLOYING": "blue",
        "DONE": "green",
        "ERROR": "red"
    }

    def _set_header_status(header_cell, env_label, new_status_text):
        color = STATUS_COLORS.get(new_status_text.upper(), "neutral")
        # Tenta substituir in-place se já existir um nó status no header_cell
        updated_in_place = False

        def _replace_status(node):
            nonlocal updated_in_place
            if isinstance(node, list):
                for item in node:
                    _replace_status(item)
            elif isinstance(node, dict):
                if node.get("type") == "status":
                    node["attrs"] = {
                        "text": new_status_text,
                        "color": color,
                        "localId": node.get("attrs", {}).get("localId", f"status-{env_label.lower()}"),
                        "style": ""
                    }
                    updated_in_place = True
                for child in node.get("content", []):
                    _replace_status(child)

        _replace_status(header_cell)

        if not updated_in_place:
            header_cell["content"] = [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": f"{env_label} ", "marks": [{"type": "strong"}]},
                        {
                            "type": "status",
                            "attrs": {
                                "text": new_status_text,
                                "color": color,
                                "localId": f"status-{env_label.lower()}",
                                "style": ""
                            }
                        }
                    ]
                }
            ]

    # 5. Atualiza o status no cabeçalho de cada coluna conforme as regras:
    # - Se não existia PR no Jira e veio PR OPEN -> muda para VALIDATING
    # - Se a PR veio CLOSED / MERGED -> muda para DEPLOYING
    header_cells = header_row.get("content", [])
    for col_idx, env_key in col_env_map.items():
        prs_for_env = env_prs.get(env_key, [])
        if not prs_for_env:
            continue

        had_pr_before = len(existing_migration.get(env_key, {}).get("prs", [])) > 0
        current_header_status = existing_migration.get(env_key, {}).get("status", "NOT STARTED").upper()

        # Verifica estados das PRs desse ambiente
        any_closed_or_merged = any(p["status"] in ["MERGED", "CLOSED", "DECLINED"] for p in prs_for_env)
        any_open = any(p["status"] == "OPEN" for p in prs_for_env)

        new_status = None
        if any_closed_or_merged:
            # Quando estiver CLOSED/MERGED muda para DEPLOYING (a menos que já esteja DONE)
            if current_header_status != "DONE":
                new_status = "DEPLOYING"
        elif any_open and (not had_pr_before or current_header_status == "NOT STARTED"):
            # Quando no Jira ainda não existir a PR e ela estiver OPEN -> VALIDATING
            new_status = "VALIDATING"

        if new_status and col_idx < len(header_cells):
            _set_header_status(header_cells[col_idx], env_key, new_status)

    # 6. Reconstrói as linhas de dados da tabela MIGRATION (criando novas linhas se houver >1 PR para o mesmo ambiente)
    max_rows_needed = max([len(env_prs[e]) for e in ["DEVINT", "QA", "UAT", "PRD"]] + [1])

    new_data_rows = []
    for row_idx in range(max_rows_needed):
        row_cells = []
        for col_idx in range(num_cols):
            env_key = col_env_map.get(col_idx)
            prs_list = env_prs.get(env_key, []) if env_key else []

            if row_idx < len(prs_list):
                pr_item = prs_list[row_idx]
                cell_content = [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": str(pr_item["number"]),
                                "marks": [
                                    {
                                        "type": "link",
                                        "attrs": {"href": pr_item["url"]}
                                    }
                                ]
                            }
                        ]
                    }
                ]
            else:
                cell_content = [{"type": "paragraph", "content": []}]

            row_cells.append({
                "type": "tableCell",
                "attrs": {},
                "content": cell_content
            })

        new_data_rows.append({
            "type": "tableRow",
            "content": row_cells
        })

    table_node["content"] = [header_row] + new_data_rows

    # 7. Salva o ADF atualizado no Jira
    ok, msg = update_jira_description_adf(ticket_key, description_adf)
    if not ok:
        return {"success": False, "message": msg}

    updated_migration_prs = extract_migration_prs_data(description_adf)
    prd_list = updated_migration_prs.get("PRD", {}).get("prs", [])
    pr_to_prod_num = prd_list[-1]["number"] if prd_list else "—"
    pr_to_prod_url = prd_list[-1]["url"] if prd_list else "#"

    return {
        "success": True,
        "message": f"PRs sincronizadas com sucesso para {ticket_key}!",
        "migration_prs": updated_migration_prs,
        "pr_to_prod_num": pr_to_prod_num,
        "pr_to_prod_url": pr_to_prod_url
    }
