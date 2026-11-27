import re
import datetime


def extract_plain_text_adf(adf_node):
    """Recursively traverses any ADF JSON block and merges all text elements."""
    if not isinstance(adf_node, dict):
        return ""

    if adf_node.get("type") == "text":
        return adf_node.get("text", "")

    if adf_node.get("type") == "status":
        return adf_node.get("attrs", {}).get("text", "")

    if adf_node.get("type") == "date":
        timestamp_ms = adf_node.get("attrs", {}).get("timestamp")
        if timestamp_ms:
            try:
                dt = datetime.datetime.utcfromtimestamp(int(timestamp_ms) / 1000.0)
                return dt.strftime("%d/%m")
            except Exception:
                pass

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
    """Recursively searches inside an ADF node for a link (Card or classic text link)."""
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


def node_contains_media(node):
    """Recursively checks if an ADF node contains a media file."""
    if isinstance(node, list):
        return any(node_contains_media(item) for item in node)
    elif isinstance(node, dict):
        node_type = node.get("type", "")
        if node_type in ["mediaInline", "media", "mediaSingle"]:
            return True

        attrs = node.get("attrs", {})
        if attrs.get("type") == "file" and node_type.startswith("media"):
            return True

        for child in node.get("content", []):
            if node_contains_media(child):
                return True

    return False


def collect_media_nodes_from_node(node, collected_list):
    """Recursively collects all 'mediaInline' / 'media' attribute dictionaries inside a given ADF node."""
    if isinstance(node, list):
        for item in node:
            collect_media_nodes_from_node(item, collected_list)
    elif isinstance(node, dict):
        node_type = node.get("type", "")
        if node_type in ["mediaInline", "media"]:
            attrs = node.get("attrs", {})
            if attrs:
                collected_list.append(attrs)

        for child in node.get("content", []):
            collect_media_nodes_from_node(child, collected_list)


def find_panel_table(adf_description, panel_keyword):
    """Finds the Info Panel matching panel_keyword and returns its following table block."""
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


def check_deployment_results_logs(description_adf):
    """Returns 'Yes' if every row contains a media file in 'DEPLOYMENT RESULTS' table, else 'No'."""
    table_node = find_panel_table(description_adf, "DEPLOYMENT RESULTS")
    if not table_node:
        return "No"

    rows = table_node.get("content", [])
    data_rows = []

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        cells = row.get("content", [])
        if not cells:
            continue

        cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
        if "tableHeader" in cell_types:
            continue
        data_rows.append(cells)

    if not data_rows:
        return "No"

    for cells in data_rows:
        file_cell = cells[-1]
        if not node_contains_media(file_cell):
            return "No"

    return "Yes"


def extract_deployment_results_attachments(issue_raw, description_adf):
    """Extracts all media files referenced in the 'DEPLOYMENT RESULTS' table."""
    table_node = find_panel_table(description_adf, "DEPLOYMENT RESULTS")
    if not table_node:
        return []

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
        return []

    issue_attachments = issue_raw.get("fields", {}).get("attachment", []) or []
    attachments_by_name = {}
    for att in issue_attachments:
        fname = att.get("filename")
        if fname:
            attachments_by_name[fname] = {
                "filename": fname,
                "content_url": att.get("content"),
                "mime_type": att.get("mimeType", "application/octet-stream")
            }

    matched_attachments = []
    seen_filenames = set()

    for m_attr in media_attrs_list:
        alt_name = (m_attr.get("alt") or "").strip()
        if alt_name and alt_name in attachments_by_name and alt_name not in seen_filenames:
            matched_attachments.append(attachments_by_name[alt_name])
            seen_filenames.add(alt_name)

    if not matched_attachments and issue_attachments:
        for fname, att_info in attachments_by_name.items():
            if fname not in seen_filenames:
                matched_attachments.append(att_info)
                seen_filenames.add(fname)

    return matched_attachments


def inspect_alm_page_status(description_adf):
    """Inspects 'ALM PAGE' status."""
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
    """Inspects 'SNOW' status."""
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


def extract_change_hour_from_snow(description_adf):
    """Finds the SNOW panel table, extracts the values from the 'DATE' and 'TIME' columns."""
    table_node = find_panel_table(description_adf, "SNOW")
    if not table_node:
        return ""

    rows = table_node.get("content", [])
    date_col_index = None
    time_col_index = None
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
                header_text = extract_plain_text_adf(cell).upper().strip()
                if "DATE" in header_text:
                    date_col_index = idx
                elif "TIME" in header_text:
                    time_col_index = idx
        else:
            data_rows.append(cells)

    if not data_rows:
        return ""

    first_data_row = data_rows[0]
    date_val = ""
    time_val = ""

    if date_col_index is not None and date_col_index < len(first_data_row):
        date_val = extract_plain_text_adf(first_data_row[date_col_index]).strip()

    if time_col_index is not None and time_col_index < len(first_data_row):
        time_val = extract_plain_text_adf(first_data_row[time_col_index]).strip()

    if date_val and time_val:
        return f"{date_val} {time_val}"
    elif date_val:
        return date_val
    elif time_val:
        return time_val

    return ""


def extract_execution_order_from_snow(description_adf):
    """Localiza a tabela abaixo do Info Panel 'SNOW', busca a coluna 'ORDER' e retorna o valor."""
    table_node = find_panel_table(description_adf, "SNOW")
    if not table_node:
        return ""

    rows = table_node.get("content", [])
    order_col_index = None
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
                header_text = extract_plain_text_adf(cell).upper().strip()
                if "ORDER" in header_text:
                    order_col_index = idx
                    break
        else:
            data_rows.append(cells)

    if not data_rows or order_col_index is None:
        return ""

    first_data_row = data_rows[0]
    if order_col_index < len(first_data_row):
        return extract_plain_text_adf(first_data_row[order_col_index]).strip()

    return ""


def extract_alm_call_from_alm_page(description_adf):
    """Localiza a tabela abaixo do Info Panel 'ALM PAGE', busca a coluna 'ALM CALL'."""
    table_node = find_panel_table(description_adf, "ALM PAGE")
    if not table_node:
        return "—"

    rows = table_node.get("content", [])
    alm_call_col_index = None
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
                header_text = extract_plain_text_adf(cell).upper().strip()
                if "ALM CALL" in header_text:
                    alm_call_col_index = idx
                    break
        else:
            data_rows.append(cells)

    if not data_rows or alm_call_col_index is None:
        return "—"

    first_data_row = data_rows[0]
    if alm_call_col_index < len(first_data_row):
        val = extract_plain_text_adf(first_data_row[alm_call_col_index]).strip()
        return val if val else "—"

    return "—"


def parse_date_for_sorting(date_str):
    """Converte strings de datas nos formatos 'DD/MM hh:mi' ou 'DD/MM' para datetime."""
    if not date_str or date_str in ["—", "N/A"]:
        return datetime.datetime(9999, 12, 31, 23, 59)

    current_year = datetime.datetime.now().year

    try:
        return datetime.datetime.strptime(f"{date_str} {current_year}", "%d/%m %H:%M %Y")
    except ValueError:
        pass

    try:
        return datetime.datetime.strptime(f"{date_str} {current_year}", "%d/%m %Y")
    except ValueError:
        pass

    return datetime.datetime(9999, 12, 31, 23, 59)


def sort_alm_call_groups(group_name):
    """Garante que as seções de data fiquem em ordem cronológica e coloca 'To Be Defined' no fim."""
    if group_name == "To Be Defined":
        return datetime.datetime(9999, 12, 31, 23, 59)
    return parse_date_for_sorting(group_name)


def update_panel_table_link_adf(adf_description, panel_keyword, new_url, display_text=None):
    """Updates the LINK cell of the table following the Info Panel."""
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


def find_sprint_field_key(issue_fields):
    """Detecta dinamicamente qual campo customfield_XXXX representa o campo de Sprint do Jira."""
    for key, value in issue_fields.items():
        if key.startswith("customfield_") and isinstance(value, list) and len(value) > 0:
            first_val = value[0]
            if isinstance(first_val, str) and "com.atlassian.greenhopper.service.sprint.Sprint" in first_val:
                return key
            if hasattr(first_val, "id") and hasattr(first_val, "name") and ("sprint" in str(type(first_val)).lower() or "sprint" in str(first_val.__class__).lower()):
                return key
            if isinstance(first_val, dict) and "id" in first_val and "name" in first_val:
                return key
    return None


def extract_sprints(sprint_field):
    """Extrai uma lista de dicionários contendo ID e Nome de todas as Sprints que o ticket possui."""
    if not sprint_field:
        return []

    sprints = []
    for item in sprint_field:
        if isinstance(item, str):
            id_match = re.search(r"id=(\d+)", item)
            name_match = re.search(r"name=([^,\]]+)", item)
            if id_match and name_match:
                try:
                    sprints.append({
                        "id": int(id_match.group(1)),
                        "name": name_match.group(1).strip()
                    })
                except ValueError:
                    pass
        elif hasattr(item, "id") and hasattr(item, "name"):
            try:
                sprints.append({
                    "id": int(item.id),
                    "name": item.name
                })
            except (ValueError, TypeError):
                pass
        elif isinstance(item, dict):
            try:
                sprints.append({
                    "id": int(item.get("id")),
                    "name": item.get("name")
                })
            except (ValueError, TypeError):
                pass
    return sprints


def extract_prd_status_adf(adf_description):
    """Localiza a tabela no ADF da descrição e varre o cabeçalho 'PRD'."""
    if not isinstance(adf_description, dict):
        return "—"

    try:
        tables = [
            block for block in adf_description.get("content", [])
            if isinstance(block, dict) and block.get("type") == "table"
        ]

        for table in tables:
            rows = table.get("content", [])
            for row in rows:
                if row.get("type") != "tableRow":
                    continue

                cells = row.get("content", [])
                if not cells:
                    continue

                cell_types = [c.get("type") for c in cells if isinstance(c, dict)]
                if "tableHeader" in cell_types:
                    for idx, cell in enumerate(cells):
                        header_text = extract_plain_text_adf(cell).upper()
                        if "PRD" in header_text:
                            status_val = "—"

                            def find_status_node(node):
                                nonlocal status_val
                                if isinstance(node, list):
                                    for item in node:
                                        find_status_node(item)
                                elif isinstance(node, dict):
                                    if node.get("type") == "status":
                                        status_val = node.get("attrs", {}).get("text", "").upper().strip()
                                    for child in node.get("content", []):
                                        find_status_node(child)

                            find_status_node(cell)

                            if status_val == "DONE":
                                return "Yes"
                            elif status_val == "ERROR":
                                return "No"
                            else:
                                return "—"

    except Exception as e:
        print(f"⚠️ Error parsing PRD status: {e}")

    return "—"


def has_technical_activities_expand(adf_node):
    """Varre recursivamente o ADF da descrição procurando por um elemento 'expand' com o título 'Technical Activities'."""
    if not adf_node:
        return False

    if isinstance(adf_node, list):
        for item in adf_node:
            if has_technical_activities_expand(item):
                return True
    elif isinstance(adf_node, dict):
        if adf_node.get("type") == "expand":
            title = adf_node.get("attrs", {}).get("title", "")
            if title and "technical activities" in title.strip().lower():
                return True
        for child in adf_node.get("content", []):
            if has_technical_activities_expand(child):
                return True

    return False


def replace_adf_placeholders(node, replacements):
    """Percorre recursivamente a árvore do ADF e substitui as variáveis definidas em 'replacements'."""
    if not node:
        return node

    if isinstance(node, list):
        return [replace_adf_placeholders(item, replacements) for item in node]

    elif isinstance(node, dict):
        new_node = {}
        for key, val in node.items():
            if key == "text" and isinstance(val, str):
                text_val = val
                for placeholder, rep_val in replacements.items():
                    if placeholder in text_val:
                        text_val = text_val.replace(placeholder, str(rep_val or ""))
                new_node[key] = text_val
            elif isinstance(val, (dict, list)):
                new_node[key] = replace_adf_placeholders(val, replacements)
            else:
                new_node[key] = val
        return new_node

    return node


def find_migration_table_in_ta(adf_description):
    """
    Procura especificamente dentro do expand 'Technical Activities'
    pelo Info Panel 'MIGRATION' e retorna a tabela logo abaixo dele.
    """
    if not isinstance(adf_description, dict):
        return None

    def _find_ta_expand(node):
        if isinstance(node, list):
            for item in node:
                res = _find_ta_expand(item)
                if res:
                    return res
        elif isinstance(node, dict):
            if node.get("type") == "expand":
                title = node.get("attrs", {}).get("title", "")
                if "technical activities" in title.strip().lower():
                    return node
            for child in node.get("content", []):
                res = _find_ta_expand(child)
                if res:
                    return res
        return None

    ta_expand = _find_ta_expand(adf_description)
    # Se encontrou o expand Technical Activities, busca o painel MIGRATION lá dentro;
    # caso contrário, faz fallback na descrição inteira
    search_root = ta_expand if ta_expand else adf_description
    return find_panel_table(search_root, "MIGRATION")


def extract_migration_prs_data(adf_description):
    """
    Lê a tabela abaixo do painel 'MIGRATION' (dentro de Technical Activities)
    e retorna os status do cabeçalho e a lista de PRs de cada ambiente:
    DEVINT, QA, UAT, PRD.
    """
    default_data = {
        "DEVINT": {"status": "NOT STARTED", "prs": []},
        "QA": {"status": "NOT STARTED", "prs": []},
        "UAT": {"status": "NOT STARTED", "prs": []},
        "PRD": {"status": "NOT STARTED", "prs": []}
    }

    table_node = find_migration_table_in_ta(adf_description)
    if not table_node:
        return default_data

    rows = table_node.get("content", [])
    col_env_map = {}  # {col_idx: 'DEVINT' | 'QA' | 'UAT' | 'PRD'}
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
                matched_env = None
                for env_key in ["DEVINT", "QA", "UAT", "PRD", "PROD"]:
                    if env_key in header_text:
                        matched_env = "PRD" if env_key == "PROD" else env_key
                        break

                if matched_env:
                    col_env_map[idx] = matched_env
                    # Procura o nó 'status' dentro do tableHeader
                    status_text = "NOT STARTED"
                    def _find_status(n):
                        nonlocal status_text
                        if isinstance(n, list):
                            for i in n:
                                _find_status(i)
                        elif isinstance(n, dict):
                            if n.get("type") == "status":
                                st = n.get("attrs", {}).get("text", "").upper().strip()
                                if st:
                                    status_text = st
                            for ch in n.get("content", []):
                                _find_status(ch)
                    _find_status(cell)
                    default_data[matched_env]["status"] = status_text
        else:
            data_rows.append(cells)

    # Percorre todas as linhas de dados da tabela MIGRATION para extrair os links/números de PR
    for cells in data_rows:
        for col_idx, env_name in col_env_map.items():
            if col_idx < len(cells):
                cell = cells[col_idx]
                url, text = find_url_and_text_in_node(cell)
                plain = extract_plain_text_adf(cell).strip()
                if url or (plain and plain not in ["—", "-", "N/A"]):
                    pr_num = text or plain
                    # Extrai apenas os dígitos se vier como URL ou #20349
                    m = re.search(r"(\d+)$", pr_num.strip())
                    if m:
                        pr_num = m.group(1)
                    default_data[env_name]["prs"].append({
                        "number": pr_num,
                        "url": url or "#"
                    })

    return default_data
