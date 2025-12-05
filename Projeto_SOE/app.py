# app.py - Servidor Flask para Monitoramento IoT
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
import requests
import datetime
import os
import threading
import time
from collections import Counter
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configurações
app.config['SECRET_KEY'] = 'chave-secreta-producao' 
ADMIN_PASSWORD = "admin123"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///projeto.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/fotos'

db = SQLAlchemy(app)
DEFAULT_TOKEN = 'SEU_TOKEN_AQUI_OU_NO_DB' 
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# --- Modelos de Dados ---

class Config(db.Model):
    """Armazena configurações dinâmicas (ex: Token Telegram)."""
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)

class Modulo(db.Model):
    """Estado atual de cada módulo IoT."""
    id = db.Column(db.String(50), primary_key=True)
    status = db.Column(db.String(20), nullable=False)
    last_update = db.Column(db.DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    current_light = db.Column(db.Integer, default=0)
    light_threshold = db.Column(db.Integer, default=1000)
    auto_mode = db.Column(db.Boolean, default=True)
    relay_on = db.Column(db.Boolean, default=False)
    photo_requested = db.Column(db.Boolean, default=False)
    last_photo = db.Column(db.String(100), nullable=True)
    requester_id = db.Column(db.String(50), nullable=True) 

    def to_dict(self):
        return {
            'id': self.id, 'status': self.status, 'current_light': self.current_light,
            'light_threshold': self.light_threshold, 'auto_mode': self.auto_mode,
            'relay_on': self.relay_on, 'last_photo': self.last_photo,
            'last_update': self.last_update.isoformat()
        }

class Historico(db.Model):
    """Registro de eventos para análise."""
    id = db.Column(db.Integer, primary_key=True)
    modulo_id = db.Column(db.String(50), nullable=False)
    status_anterior = db.Column(db.String(20), nullable=False)
    novo_status = db.Column(db.String(20), nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))

class User(db.Model):
    """Usuários e permissões."""
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(50), nullable=False)
    chat_id = db.Column(db.String(20), unique=True, nullable=False)
    can_toggle_light = db.Column(db.Boolean, default=False)
    can_request_photo = db.Column(db.Boolean, default=False)

# --- Helpers ---

def get_token():
    try:
        with app.app_context():
            conf = db.session.get(Config, 'telegram_token')
            return conf.value if conf else DEFAULT_TOKEN
    except: return DEFAULT_TOKEN

def _send_msg(chat_id, text):
    try: requests.post(f"https://api.telegram.org/bot{get_token()}/sendMessage", json={'chat_id': chat_id, 'text': text}, timeout=5)
    except: pass

def _send_photo(chat_id, file_path):
    try:
        with open(file_path, 'rb') as f:
            requests.post(f"https://api.telegram.org/bot{get_token()}/sendPhoto", data={'chat_id': chat_id}, files={'photo': f}, timeout=15)
    except: pass

def notify_all(modulo_id, status):
    msg = f"🚨 Alerta {modulo_id}: Status -> {status.upper()}"
    with app.app_context():
        for u in User.query.all(): _send_msg(u.chat_id, msg)

# --- Thread do Bot (Polling) ---
def bot_loop():
    last_id = 0
    while True:
        try:
            token = get_token()
            if not token or token == DEFAULT_TOKEN:
                time.sleep(5); continue
            
            res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates?offset={last_id + 1}&timeout=10", timeout=15)
            data = res.json()
            
            if data.get('ok'):
                for r in data.get('result', []):
                    last_id = r['update_id']
                    if 'message' not in r or 'text' not in r['message']: continue
                    
                    msg = r['message']; chat = str(msg['chat']['id']); cmd = msg['text'].strip().split()
                    
                    with app.app_context():
                        u = User.query.filter_by(chat_id=chat).first()
                        if not u: 
                            _send_msg(chat, "🚫 Não autorizado.")
                            continue
                        
                        op = cmd[0].lower()
                        if op == '/start': _send_msg(chat, f"Olá {u.nome}! Comandos: /status, /foto [id], /luz [id] [on/off/auto]")
                        elif op == '/status':
                            txt = "📊 Status:"
                            for m in Modulo.query.all(): txt += f"\n🔹 {m.id}: {m.status} | Luz: {'ON' if m.relay_on else 'OFF'}"
                            _send_msg(chat, txt)
                        elif op == '/foto' and len(cmd) > 1:
                            if u.can_request_photo:
                                m = db.session.get(Modulo, cmd[1])
                                if m: m.photo_requested=True; m.requester_id=chat; db.session.commit(); _send_msg(chat, "📸 Solicitado.")
                                else: _send_msg(chat, "❌ ID inválido.")
                            else: _send_msg(chat, "🔒 Sem permissão.")
                        elif op == '/luz' and len(cmd) > 2:
                            if u.can_toggle_light:
                                m = db.session.get(Modulo, cmd[1])
                                if m:
                                    act = cmd[2].lower()
                                    if act == 'on': m.relay_on=True; m.auto_mode=False
                                    elif act == 'off': m.relay_on=False; m.auto_mode=False
                                    elif act == 'auto': m.auto_mode=True
                                    db.session.commit(); _send_msg(chat, f"Luz {act}.")
                                else: _send_msg(chat, "❌ ID inválido.")
                            else: _send_msg(chat, "🔒 Sem permissão.")
            time.sleep(1)
        except: time.sleep(5)

# --- Rotas da API ---

@app.route('/api/status', methods=['POST'])
def api_status():
    d = request.json
    mid = d.get('modulo_id')
    if not mid: return jsonify({'e': 'ID inv'}), 400

    m = db.session.get(Modulo, mid)
    if not m: m = Modulo(id=mid, status="init"); db.session.add(m)
    
    new_st = d.get('status')
    if new_st and m.status != new_st:
        db.session.add(Historico(modulo_id=mid, status_anterior=m.status, novo_status=new_st))
        m.status = new_st
        threading.Thread(target=notify_all, args=(mid, new_st)).start()
    
    m.current_light = d.get('light_reading', 0)
    m.last_update = datetime.datetime.now(datetime.timezone.utc)
    db.session.commit()

    return jsonify({
        'auto_mode': m.auto_mode, 'relay_on': m.relay_on,
        'light_threshold': m.light_threshold, 'photo_command': m.photo_requested
    })

@app.route('/api/upload_photo/<string:mid>', methods=['POST'])
def api_upload(mid):
    f = request.files.get('file')
    if f:
        fname = secure_filename(f"{mid}_{int(time.time())}.jpg")
        path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        f.save(path)
        m = db.session.get(Modulo, mid)
        if m:
            m.last_photo = fname; m.photo_requested = False
            if m.requester_id:
                threading.Thread(target=_send_photo, args=(m.requester_id, path)).start()
                m.requester_id = None
            db.session.commit()
        return jsonify({'ok': True})
    return jsonify({'e': 'Err'}), 400

@app.route('/api/modulos', methods=['GET'])
def api_list():
    if 'user_logged_in' not in session and 'admin_logged_in' not in session: return jsonify({'e': '401'}), 401
    return jsonify([m.to_dict() for m in Modulo.query.all()])

# --- Rotas Web ---

@app.route('/')
def index():
    if 'user_logged_in' not in session: return redirect(url_for('login'))
    return render_template('index.html', user=db.session.get(User, session.get('user_id')))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form.get('username')).first()
        if u and u.password == request.form.get('password'):
            session['user_logged_in']=True; session['user_id']=u.id; session['user_name']=u.nome
            return redirect(url_for('index'))
        flash('Erro login.', 'danger')
    return render_template('user_login.html')

@app.route('/logout')
def logout(): session.pop('user_logged_in', None); return redirect(url_for('login'))

# --- Rotas Admin ---

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin_logged_in']=True; return redirect(url_for('admin_dashboard'))
        flash('Senha incorreta.', 'danger')
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin_logged_in' not in session: return redirect(url_for('admin_login'))
    t = db.session.get(Config, 'telegram_token')
    return render_template('admin_dashboard.html', users=User.query.all(), modules=Modulo.query.all(), current_token=t.value if t else "")

@app.route('/admin/analytics')
def analytics():
    if 'admin_logged_in' not in session: return redirect(url_for('admin_login'))
    events = Historico.query.filter_by(novo_status='vazio').all()
    # Analise simplificada
    days = [e.timestamp.strftime('%A') for e in events]
    hours = [f"{e.timestamp.hour}h" for e in events]
    
    crit_day = Counter(days).most_common(1)[0][0] if days else "-"
    crit_hour = Counter(hours).most_common(1)[0][0] if hours else "-"
    
    return render_template('analytics.html', dia_critico=crit_day, hora_critica=crit_hour, total_vazios=len(events), historico=Historico.query.order_by(Historico.id.desc()).limit(50).all())

# Rotas de Ação (Admin/User)
@app.route('/control/toggle/<string:mid>/<action>')
def control_toggle(mid, action):
    # Verifica permissao
    u = db.session.get(User, session.get('user_id'))
    is_admin = 'admin_logged_in' in session
    if not is_admin and (not u or not u.can_toggle_light): return redirect(url_for('index'))
    
    m = db.session.get(Modulo, mid)
    if m:
        if action == 'light': m.relay_on = not m.relay_on; m.auto_mode = False
        elif action == 'auto': m.auto_mode = not m.auto_mode
        db.session.commit()
    
    return redirect(url_for('admin_dashboard' if is_admin else 'index'))

@app.route('/control/photo/<string:mid>')
def control_photo(mid):
    u = db.session.get(User, session.get('user_id'))
    is_admin = 'admin_logged_in' in session
    if not is_admin and (not u or not u.can_request_photo): return redirect(url_for('index'))
    
    m = db.session.get(Modulo, mid)
    if m: m.photo_requested = True; db.session.commit()
    return redirect(url_for('admin_dashboard' if is_admin else 'index'))

# Rotas CRUD Admin
@app.route('/admin/update_token', methods=['POST'])
def update_token():
    v = request.form.get('telegram_token')
    c = db.session.get(Config, 'telegram_token')
    if not c: c = Config(key='telegram_token', value=v); db.session.add(c)
    else: c.value = v
    db.session.commit(); return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_perms', methods=['POST'])
def update_perms():
    u = db.session.get(User, request.form.get('user_id'))
    if u: u.can_toggle_light=(request.form.get('perm_light')=='on'); u.can_request_photo=(request.form.get('perm_photo')=='on'); db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_thresh', methods=['POST'])
def update_thresh():
    m = db.session.get(Modulo, request.form.get('modulo_id'))
    if m: m.light_threshold=int(request.form.get('threshold')); db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/register_user', methods=['POST'])
def register_user():
    try:
        db.session.add(User(nome=request.form.get('nome'), chat_id=request.form.get('chat_id'), username=request.form.get('username'), password=request.form.get('password')))
        db.session.commit()
    except: flash('Erro.', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete/<type>/<id>')
def delete_item(type, id):
    if 'admin_logged_in' not in session: return redirect(url_for('admin_login'))
    obj = db.session.get(User if type=='user' else Modulo, id)
    if obj: db.session.delete(obj); db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/logout')
def admin_logout(): session.pop('admin_logged_in', None); return redirect(url_for('admin_login'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)