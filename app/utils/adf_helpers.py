import re
import copy

def has_technical_activities_expand(adf_node):
    """
    Varre recursivamente o ADF da descrição procurando por um elemento
    'expand' com o título 'Technical Activities'.
    """
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
    """
    Percorre recursivamente a árvore do ADF e substitui as variáveis
    definidas em 'replacements' (ex: {'[ticket]': 'CRMB2B-123'}) em todos os nós de texto.
    A substituição é case-insensitive.
    """
    if not node:
        return node

    if isinstance(node, list):
        return [replace_adf_placeholders(item, replacements) for item in node]

    elif isinstance(node, dict):
        new_node = {}
        for key, val in node.items():
            if key == "text" and isinstance(val, str):
                text_val = val
                for pattern, rep_val in replacements.items():
                    # Substituição case-insensitive usando expressão regular
                    text_val = re.sub(pattern, str(rep_val or ""), text_val, flags=re.IGNORECASE)
                new_node[key] = text_val
            elif isinstance(val, (dict, list)):
                new_node[key] = replace_adf_placeholders(val, replacements)
            else:
                new_node[key] = val
        return new_node

    return node