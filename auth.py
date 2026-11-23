import os
from functools import wraps
from flask import Blueprint, session, redirect, url_for, request, jsonify
from authlib.integrations.flask_client import OAuth

# Criamos o Blueprint para gerenciar as rotas de autenticação
auth_bp = Blueprint('auth', __name__)

oauth = OAuth()
ALLOWED_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "amber.com.br")

def init_auth(app):
    """
    Inicializa o OAuth com a chave secreta da aplicação Flask.
    Registra o Blueprint de rotas de login no app principal.
    """
    # Define a chave que assina os cookies criptografados da sessão
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "exp-secret-key-trocar-no-env")
    
    # Inicializa o objeto oauth com o app Flask
    oauth.init_app(app)
    
    # Registra o provedor do Google
    oauth.register(
        name='google',
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )
    
    # Registra o blueprint no app para liberar as rotas /login, /authorize e /logout
    app.register_blueprint(auth_bp)


def login_required(f):
    """
    Decorador para proteger rotas.
    Redireciona para o login do Google se o usuário não estiver autenticado.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            # Se for uma requisição de API ou AJAX (ex: refresh), retorna 401 JSON
            if request.path.startswith('/api/') or request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"error": "Sessão expirada. Faça login novamente."}), 401
            # Caso contrário, redireciona o navegador direto para a rota de login
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


# --- ROTAS DE AUTENTICAÇÃO ---

@auth_bp.route('/login')
def login():
    if 'user' in session:
        return redirect('/')
    
    # Gera a URL de callback para a qual o Google redirecionará após o login
    redirect_uri = url_for('auth.authorize', _external=True)
    
    # O parâmetro 'hd' força o Google a focar apenas nas contas do domínio corporativo
    return oauth.google.authorize_redirect(redirect_uri, hd=ALLOWED_DOMAIN)


@auth_bp.route('/authorize')
def authorize():
    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get('userinfo')
        
        # Garante de forma estrita que o e-mail pertence ao domínio da empresa
        if not user_info or not user_info.get('email', '').endswith(f"@{ALLOWED_DOMAIN}"):
            return f"Acesso negado: Apenas e-mails @{ALLOWED_DOMAIN} são permitidos.", 403

        # Salva as informações básicas do perfil direto na sessão criptografada (Stateless)
        session['user'] = {
            'name': user_info.get('name'),
            'email': user_info.get('email'),
            'picture': user_info.get('picture')
        }
        
        # Redireciona o usuário direto para a página principal (main)
        return redirect('/')
        
    except Exception as e:
        return f"Erro durante a autenticação: {str(e)}", 400


@auth_bp.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('auth.login'))
