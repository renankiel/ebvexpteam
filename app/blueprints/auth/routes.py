import os
import requests
from functools import wraps
from flask import Blueprint, session, redirect, url_for, request, jsonify
from requests.auth import HTTPBasicAuth
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

load_dotenv()

auth_bp = Blueprint("auth", __name__)
oauth = OAuth()

ALLOWED_DOMAINS_STR = os.getenv("ALLOWED_EMAIL_DOMAINS", "amber.com.br,equifax.com,gmail.com")
ALLOWED_DOMAINS = [d.strip().lower() for d in ALLOWED_DOMAINS_STR.split(",")]

JIRA_BASE_URL = os.getenv("JIRA_URL")
JIRA_USER_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_TOKEN")
GITHUB_PROP_KEY = "exp_github_username"


def _resolve_jira_account_id_by_email(email):
    if not (JIRA_BASE_URL and JIRA_USER_EMAIL and JIRA_API_TOKEN and email):
        return None
    try:
        url = f"{JIRA_BASE_URL}/rest/api/3/user/search?query={email}"
        auth = HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
        resp = requests.get(url, auth=auth, headers={"Accept": "application/json"}, timeout=5)
        if resp.status_code == 200:
            users = resp.json()
            if users and isinstance(users, list):
                return users[0].get("accountId")
    except Exception as e:
        print(f"⚠️ Erro ao buscar usuário por e-mail no Jira: {e}")
    return None


def get_github_by_email(email):
    account_id = _resolve_jira_account_id_by_email(email)
    if not account_id:
        return None
    try:
        url = f"{JIRA_BASE_URL}/rest/api/3/user/properties/{GITHUB_PROP_KEY}?accountId={account_id}"
        auth = HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
        resp = requests.get(url, auth=auth, headers={"Accept": "application/json"}, timeout=5)
        if resp.status_code == 200:
            val = resp.json().get("value")
            if isinstance(val, dict):
                return val.get("github")
            return val
    except Exception as e:
        print(f"⚠️ Erro ao ler propriedade do GitHub no Jira: {e}")
    return None


def save_github_by_email(email, github_handle):
    account_id = _resolve_jira_account_id_by_email(email)
    if not account_id:
        return False
    try:
        url = f"{JIRA_BASE_URL}/rest/api/3/user/properties/{GITHUB_PROP_KEY}?accountId={account_id}"
        auth = HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        resp = requests.put(url, auth=auth, headers=headers, json={"github": github_handle}, timeout=5)
        return resp.status_code in (200, 201, 204)
    except Exception as e:
        print(f"⚠️ Erro ao salvar propriedade do GitHub no Jira: {e}")
    return False


def init_auth(app):
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "exp-secret-key-trocar-no-env")
    oauth.init_app(app)
    oauth.register(
        name="google",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"}
    )
    app.register_blueprint(auth_bp)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/") or request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Sessão expirada. Faça login novamente."}), 401
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated_function


@auth_bp.route("/login")
def login():
    if "user" in session:
        return redirect("/")
    redirect_uri = url_for("auth.authorize", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/authorize")
def authorize():
    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get("userinfo")
        if not user_info:
            return "Erro ao obter informações do usuário do Google.", 400

        email = user_info.get("email", "").strip().lower()
        email_domain = email.split("@")[-1] if "@" in email else ""

        if email_domain not in ALLOWED_DOMAINS:
            return f"Acesso negado: O domínio @{email_domain} não possui autorização.", 403

        saved_github = get_github_by_email(email)

        session["user"] = {
            "name": user_info.get("name"),
            "email": email,
            "picture": user_info.get("picture"),
            "github": saved_github if saved_github else ""
        }
        return redirect("/")
    except Exception as e:
        return f"Erro durante a autenticação: {str(e)}", 400


@auth_bp.route("/api/save-github-user", methods=["POST"])
@login_required
def api_save_github_user():
    data = request.get_json() or {}
    github_user = data.get("github", "").strip().lstrip("@")
    if not github_user:
        return jsonify({"success": False, "error": "Informe um usuário válido."}), 400

    email = session["user"].get("email")
    saved_ok = save_github_by_email(email, github_user)
    if not saved_ok:
        return jsonify({"success": False, "error": "Não foi possível salvar no Jira."}), 500

    user_data = dict(session["user"])
    user_data["github"] = github_user
    session["user"] = user_data
    session.modified = True

    return jsonify({"success": True, "github": github_user})


@auth_bp.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("auth.login"))
