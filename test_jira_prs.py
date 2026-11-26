import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = (os.getenv("JIRA_URL") or "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")

# Defina a chave do seu ticket de teste real
TICKET_KEY = "CRMB2B-1"  


def get_issue_id(ticket_key):
    """Busca o ID numérico interno da issue no Jira."""
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}?fields=id"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    response = requests.get(url, auth=auth, headers=headers, timeout=10)
    if response.status_code != 200:
        print(f"❌ Erro ao buscar ID do ticket {ticket_key}: Status {response.status_code}")
        return None

    return response.json().get("id")


def get_issue_pull_requests(ticket_key):
    if not (JIRA_URL and JIRA_EMAIL and JIRA_TOKEN):
        print("❌ Credenciais do Jira ausentes no arquivo .env.")
        return

    print(f"🔍 [1/3] Buscando ID interno da issue para '{ticket_key}'...")
    issue_id = get_issue_id(ticket_key)

    if not issue_id:
        print("⚠️ Não foi possível encontrar o ID correspondente no Jira.")
        return

    print(f"✅ ID da issue encontrado: {issue_id}")
    
    # 1. Faz uma busca prévia no summary para listar os aplicativos e tipos reais integrados
    print(f"🔍 [2/3] Verificando resumo de ferramentas no painel de desenvolvimento...")
    summary_url = f"{JIRA_URL}/rest/dev-status/latest/issue/summary"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}
    
    summary_resp = requests.get(summary_url, params={"issueId": issue_id}, auth=auth, headers=headers, timeout=15)
    
    # Definimos os candidatos de tipo de aplicação para varrer
    # oAuth-com.github.integration.production é o novo padrão para o App oficial do GitHub!
    application_candidates = ["oAuth-com.github.integration.production", "GitHub", "github", "githube"]
    
    if summary_resp.status_code == 200:
        summary_data = summary_resp.json()
        print("📊 Resumo do Painel de Desenvolvimento obtido com sucesso.")
        
        # Tenta descobrir o applicationType dinamicamente a partir do resumo
        by_instance = summary_data.get("summary", {}).get("pullrequest", {}).get("byInstanceType", {})
        if by_instance:
            discovered_types = list(by_instance.keys())
            print(f"💡 Instâncias de PR detectadas no resumo: {discovered_types}")
            for d_type in discovered_types:
                if d_type not in application_candidates:
                    application_candidates.insert(0, d_type)

    print(f"🔍 [3/3] Consultando detalhes de desenvolvimento (dataType=pullrequest)...")
    
    # Vamos testar o endpoint /detail e o alternativo /details se necessário
    endpoints = ["/rest/dev-status/latest/issue/detail", "/rest/dev-status/latest/issue/details"]
    pull_requests = []
    success = False

    for endpoint_path in endpoints:
        if success:
            break
            
        dev_status_url = f"{JIRA_URL}{endpoint_path}"
        
        for app_type in application_candidates:
            print(f"   ⚡ Tentando endpoint '{endpoint_path}' com tipo '{app_type}'...")
            
            params = {
                "issueId": issue_id,
                "applicationType": app_type,
                "dataType": "pullrequest"
            }
            
            try:
                response = requests.get(dev_status_url, params=params, auth=auth, headers=headers, timeout=15)
                
                if response.status_code == 200:
                    data = response.json()
                    detail = data.get("detail", [])
                    
                    if detail:
                        for app in detail:
                            prs = app.get("pullRequests", []) or app.get("pullrequests", [])
                            if prs:
                                pull_requests.extend(prs)
                        
                        if pull_requests:
                            success = True
                            break
            except Exception as e:
                print(f"   ⚠️ Exceção na tentativa: {e}")

    if not success:
        print(f"\n⚠️ Nenhuma Pull Request ativa ou vinculada retornada pelas APIs para o ticket {ticket_key}.")
        print("💡 Dica: Certifique-se de que o título da PR ou o nome da branch no GitHub contenham a chave do ticket (ex: 'CRMB2B-1')!")
        return []

    print("\n" + "=" * 60)
    print(f"🎯 PULL REQUESTS ENCONTRADAS PARA O TICKET: {ticket_key}")
    print("=" * 60)

    for idx, pr in enumerate(pull_requests, start=1):
        pr_id = pr.get("id")
        pr_name = pr.get("name")
        pr_url = pr.get("url")
        pr_status = pr.get("status")

        # Mapeia dinamicamente as diferentes chaves que o Jira usa para branch
        dest_branch = (
            pr.get("destinationBranch")
            or pr.get("targetBranch")
            or pr.get("toBranch")
            or (pr.get("destination") or {}).get("branch")
            or (pr.get("target") or {}).get("branch")
            or "—"
        )
        
        source_branch = (
            pr.get("sourceBranch")
            or (pr.get("source") or {}).get("branch")
            or pr.get("fromBranch")
            or "—"
        )

        print(f"\n[{idx}] 📌 Título: {pr_name}")
        print(f"    🆔 ID: {pr_id}")
        print(f"    🚦 Status: {pr_status}")
        print(f"    🌿 Branches: {source_branch} ➔ {dest_branch}")
        print(f"    🔗 URL: {pr_url}")

        # Se ainda vier como "—", descomente a linha abaixo para ver todas as chaves do objeto PR:
        # print("    🔍 Chaves disponíveis no objeto PR:", list(pr.keys()))

    print("\n" + "=" * 60)

    return pull_requests


if __name__ == "__main__":
    get_issue_pull_requests(TICKET_KEY)
