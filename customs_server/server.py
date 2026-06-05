"""
本地报关文件生成服务
端口：5008
Chrome 扩展通过 POST http://localhost:5008/generate 调用
"""
import sys
import os

# PyInstaller 打包后资源路径兼容
if getattr(sys, 'frozen', False):
    _BASE = sys._MEIPASS
    # 优先从 exe 同目录加载 .py 文件（方便热更新，无需重新打包）
    _EXE_DIR = os.path.dirname(sys.executable)
    sys.path.insert(0, _EXE_DIR)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
    _EXE_DIR = _BASE

_DATA_BASE = _EXE_DIR if getattr(sys, 'frozen', False) else _BASE

sys.path.insert(0, _BASE)

from flask import Flask, request, jsonify, render_template_string, send_from_directory, send_file
from flask_cors import CORS
import traceback
import json
import sqlite3
import hashlib
import secrets
import hmac
import html
import subprocess
import time
import shutil
import zipfile

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from excel_gen import generate_all, process_tax_pdf
from ai_identify import (
    identify_and_save, identify_from_jdy,
    get_config_for_ui, save_config, get_existing_codes,
    preview_from_license, write_confirmed_license,
)
import jdy_api
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from flask import session, redirect, url_for
from werkzeug.security import check_password_hash

app = Flask(__name__)
APP_BUILD = '20260603-reorder-mvp'

_AUTH_USER = os.environ.get('QIHANG_LEGACY_USER', 'qh0001')
_AUTH_PASS = os.environ.get('QIHANG_LEGACY_PASS', 'CHANGE_ME_LEGACY_PASS')
_AUTH_SECRET_FILE = os.path.join(_DATA_BASE, 'server_secret.key')
_AUTH_DB_FILE = os.path.join(_DATA_BASE, 'auth_users.sqlite3')
_ADMIN_USER = os.environ.get('QIHANG_ADMIN_USER', 'admin')
_ADMIN_PASS = os.environ.get('QIHANG_ADMIN_PASS', 'CHANGE_ME_ADMIN_PASS')


def _load_server_secret():
    try:
        if os.path.exists(_AUTH_SECRET_FILE):
            with open(_AUTH_SECRET_FILE, 'r', encoding='utf-8') as f:
                secret = f.read().strip()
                if secret:
                    return secret
        os.makedirs(os.path.dirname(_AUTH_SECRET_FILE), exist_ok=True)
        secret = secrets.token_urlsafe(48)
        with open(_AUTH_SECRET_FILE, 'w', encoding='utf-8') as f:
            f.write(secret)
        return secret
    except Exception:
        return 'qihang-local-fallback-secret'


app.secret_key = _load_server_secret()
app.permanent_session_lifetime = timedelta(days=365)
app.config.update(
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True,
)


def _hash_password(password, salt=''):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', (password or '').encode('utf-8'), salt.encode('utf-8'), 120000)
    return f'pbkdf2_sha256${salt}${digest.hex()}'


def _verify_password(password, stored):
    try:
        stored = str(stored or '')
        parts = str(stored or '').split('$')
        if len(parts) == 3 and parts[0] == 'pbkdf2_sha256':
            expected = _hash_password(password, parts[1])
            return hmac.compare_digest(expected, stored)
        return check_password_hash(stored, password or '')
    except Exception:
        return False


def _auth_conn():
    os.makedirs(os.path.dirname(_AUTH_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(_AUTH_DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    admin = conn.execute('SELECT id FROM users WHERE username = ?', (_ADMIN_USER,)).fetchone()
    admin_hash = _hash_password(_ADMIN_PASS)
    if admin:
        conn.execute(
            '''UPDATE users
               SET password_hash = COALESCE(NULLIF(password_hash, ''), ?), name = ?, role = 'admin', status = 'active', approved_at = COALESCE(NULLIF(approved_at, ''), ?)
               WHERE username = ?''',
            (admin_hash, '管理员', now, _ADMIN_USER),
        )
    else:
        conn.execute(
            '''INSERT INTO users (username, password_hash, name, role, status, created_at, approved_at)
               VALUES (?, ?, ?, 'admin', 'active', ?, ?)''',
            (_ADMIN_USER, admin_hash, '管理员', now, now),
        )
    conn.commit()
    return conn


def _row_to_user(row):
    if not row:
        return None
    return {
        'id': row['id'],
        'username': row['username'],
        'name': row['name'],
        'role': row['role'],
        'status': row['status'],
        'created_at': row['created_at'],
        'approved_at': row['approved_at'],
    }


def _current_user():
    if not _is_logged_in():
        return None
    return {
        'username': session.get('auth_user') or '',
        'name': session.get('auth_name') or '',
        'role': session.get('auth_role') or 'user',
        'is_admin': session.get('auth_role') == 'admin',
    }


def _is_admin():
    return _is_logged_in() and session.get('auth_role') == 'admin'


def _admin_path_required():
    admin_exact = {
        '/ai-config', '/browse-folder', '/browse-file',
        '/jdy-config', '/jdy-refresh-auth', '/jdy-test',
        '/system-update/upload', '/system-update/backups', '/system-update/rollback',
        '/system-logs', '/sales-sync-config', '/sales-sync/status', '/sales-sync-run',
        '/jdy-webhook/status',
        '/sales-cache/status', '/sales-cache-refresh', '/sales-cache-cleanup',
        '/clear-supplier-cache', '/admin/users',
    }
    if request.path in admin_exact:
        return True
    return request.path.startswith('/admin/users/')

_LOG_DIR = os.path.join(_DATA_BASE, 'logs')
_APP_LOG_FILE = os.path.join(_LOG_DIR, 'server.log')
_GENERATED_DOWNLOAD_DIR = os.path.join(_DATA_BASE, '_generated_downloads')
_GENERATED_WORK_DIR = os.path.join(_DATA_BASE, '_generated_work')


def _log_event(tag, message):
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(_APP_LOG_FILE, 'a', encoding='utf-8', errors='replace') as f:
            f.write(f'[{ts}] [{tag}] {message}\n')
    except Exception:
        pass


def _tail_text_file(path, max_bytes=131072):
    if not path or not os.path.exists(path):
        return ''
    max_bytes = max(1024, min(int(max_bytes or 131072), 1024 * 1024))
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes), os.SEEK_SET)
        data = f.read()
    return data.decode('utf-8', errors='replace')


def _system_log_files():
    def latest_update_log():
        update_dir = os.path.join(_DATA_BASE, '_updates')
        names = ['apply_update.log']
        try:
            names.extend(
                name for name in os.listdir(update_dir)
                if name.lower().startswith('update_') and name.lower().endswith('.log')
            )
        except Exception:
            pass
        paths = [os.path.join(update_dir, name) for name in names]
        paths = [path for path in paths if os.path.exists(path)]
        if not paths:
            return os.path.join(update_dir, 'apply_update.log')
        return max(paths, key=lambda path: os.path.getmtime(path))

    candidates = {
        'server': ('业务日志 server.log', os.path.join(_LOG_DIR, 'server.log')),
        'watchdog': ('守护进程 watchdog.log', os.path.join(_LOG_DIR, 'watchdog.log')),
        'stdout': ('服务输出 server_stdout.log', os.path.join(_LOG_DIR, 'server_stdout.log')),
        'stderr': ('服务错误 server_stderr.log', os.path.join(_LOG_DIR, 'server_stderr.log')),
        'update': ('在线更新日志', latest_update_log()),
        'runtime_out': ('本地运行输出 server_runtime.out.log', os.path.join(_DATA_BASE, 'server_runtime.out.log')),
        'runtime_err': ('本地运行错误 server_runtime.err.log', os.path.join(_DATA_BASE, 'server_runtime.err.log')),
        'sales_sync_out': ('销售同步输出 sales_quantity_sync.out.log', os.path.join(_DATA_BASE, 'sales_quantity_sync.out.log')),
        'sales_sync_err': ('销售同步错误 sales_quantity_sync.err.log', os.path.join(_DATA_BASE, 'sales_quantity_sync.err.log')),
    }
    items = []
    for key, (label, path) in candidates.items():
        exists = os.path.exists(path)
        try:
            stat = os.stat(path) if exists else None
            size = stat.st_size if stat else 0
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S') if stat else ''
        except Exception:
            size = 0
            mtime = ''
        items.append({'key': key, 'label': label, 'exists': exists, 'size': size, 'mtime': mtime})
    return items, {key: path for key, (_, path) in candidates.items()}


def _is_logged_in():
    if session.get('auth_ok') is not True:
        return False
    username = session.get('auth_user') or ''
    if not username:
        session.clear()
        return False
    try:
        with _auth_conn() as conn:
            row = conn.execute('SELECT username, name, role, status FROM users WHERE username = ?', (username,)).fetchone()
        if not row or row['status'] != 'active':
            session.clear()
            return False
        session['auth_name'] = row['name']
        session['auth_role'] = row['role']
        return True
    except Exception:
        return False


def _wants_json():
    accept = request.headers.get('Accept', '')
    return (
        request.path.startswith('/api/')
        or request.is_json
        or 'application/json' in accept
        or request.path not in ('/', '/jdy', '/login')
    )


@app.before_request
def require_login():
    if request.method == 'OPTIONS':
        return None
    public_paths = {'/health', '/login', '/register', '/favicon.ico', '/jdy-webhook'}
    if request.path in public_paths:
        return None
    if _is_logged_in():
        if _admin_path_required() and not _is_admin():
            if _wants_json():
                return jsonify({'success': False, 'error': '只有管理员可以进入设置'}), 403
            return redirect('/jdy')
        return None
    if _wants_json():
        return jsonify({'success': False, 'error': '请先登录'}), 401
    return redirect(url_for('login_page', next=request.full_path or '/jdy'))


@app.route('/')
def home():
    return redirect('/jdy')


def _legacy_login_page_unused():
    error = ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        remember = (request.form.get('remember') or '1') in ('1', 'on', 'true', 'yes')
        if hmac.compare_digest(username, _AUTH_USER) and hmac.compare_digest(password, _AUTH_PASS):
            session.clear()
            session.permanent = remember
            session['auth_ok'] = True
            session['auth_user'] = username
            nxt = request.args.get('next') or '/jdy'
            if not nxt.startswith('/'):
                nxt = '/jdy'
            return redirect(nxt)
        error = '账号或密码错误'
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>祺航本地项目登录</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#eef3f7;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1f2d3d}}
.box{{width:min(92vw,380px);background:#fff;border:1px solid #dde6ee;border-radius:14px;box-shadow:0 18px 45px rgba(30,55,90,.14);padding:28px}}
h1{{font-size:24px;margin:0 0 8px;text-align:center}}p{{margin:0 0 22px;text-align:center;color:#788797;font-size:14px}}
label{{display:block;font-size:13px;color:#5f6f7f;margin:14px 0 6px}}input{{width:100%;height:44px;border:1px solid #cfd9e3;border-radius:8px;padding:0 12px;font-size:16px;outline:none}}input:focus{{border-color:#0cbfbd;box-shadow:0 0 0 3px rgba(12,191,189,.14)}}
.password-wrap{{position:relative}}.password-wrap input{{padding-right:48px}}.eye-btn{{position:absolute;right:6px;top:6px;width:34px;height:32px;margin:0;border:0;border-radius:6px;background:#eef3f7;color:#465565;font-size:15px;cursor:pointer}}
.remember{{display:flex;align-items:center;gap:8px;margin-top:14px;color:#5f6f7f;font-size:14px}}.remember input{{width:18px;height:18px;padding:0}}
button{{width:100%;height:44px;margin-top:20px;border:0;border-radius:8px;background:#0cbfbd;color:#fff;font-size:16px;font-weight:700;cursor:pointer}}
.err{{min-height:20px;margin-top:12px;color:#d93025;text-align:center;font-size:13px}}
</style>
</head>
<body>
  <form class="box" method="post">
    <h1>祺航本地项目</h1>
    <p>请输入账号密码后继续访问</p>
    <label>账号</label>
    <input name="username" autocomplete="username" autofocus>
    <label>密码</label>
    <div class="password-wrap">
      <input id="password" name="password" type="password" autocomplete="current-password">
      <button class="eye-btn" type="button" onclick="const p=document.getElementById('password');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'👁':'🙈';">👁</button>
    </div>
    <label class="remember"><input name="remember" type="checkbox" value="1" checked> 保持登录</label>
    <button type="submit">登录</button>
    <div class="err">{error}</div>
  </form>
</body>
</html>'''


@app.route('/logout', methods=['POST', 'GET'])
def logout_page():
    session.clear()
    return redirect('/login')


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    error = ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        remember = (request.form.get('remember') or '1') in ('1', 'on', 'true', 'yes')
        with _auth_conn() as conn:
            row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if row and _verify_password(password, row['password_hash']):
            if row['status'] != 'active':
                error = '账号已提交注册申请，请等待管理员通过'
            else:
                session.clear()
                session.permanent = remember
                session['auth_ok'] = True
                session['auth_user'] = row['username']
                session['auth_name'] = row['name']
                session['auth_role'] = row['role']
                nxt = request.args.get('next') or '/jdy'
                if not nxt.startswith('/'):
                    nxt = '/jdy'
                return redirect(nxt)
        else:
            if hmac.compare_digest(username, _AUTH_USER) and hmac.compare_digest(password, _AUTH_PASS):
                session.clear()
                session.permanent = remember
                session['auth_ok'] = True
                session['auth_user'] = username
                session['auth_name'] = username
                session['auth_role'] = 'admin' if username == _ADMIN_USER else 'user'
                nxt = request.args.get('next') or '/jdy'
                if not nxt.startswith('/'):
                    nxt = '/jdy'
                return redirect(nxt)
            error = '账号或密码错误'
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>祺航本地项目登录</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#eef3f7;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1f2d3d}}
.box{{width:min(92vw,380px);background:#fff;border:1px solid #dde6ee;border-radius:14px;box-shadow:0 18px 45px rgba(30,55,90,.14);padding:28px}}
h1{{font-size:24px;margin:0 0 8px;text-align:center}}p{{margin:0 0 22px;text-align:center;color:#788797;font-size:14px}}
label{{display:block;font-size:13px;color:#5f6f7f;margin:14px 0 6px}}input{{width:100%;height:44px;border:1px solid #cfd9e3;border-radius:8px;padding:0 12px;font-size:16px;outline:none}}input:focus{{border-color:#0cbfbd;box-shadow:0 0 0 3px rgba(12,191,189,.14)}}
.password-wrap{{position:relative}}.password-wrap input{{padding-right:52px}}.eye-btn{{position:absolute;right:6px;top:6px;width:42px;height:32px;margin:0;border:0;border-radius:6px;background:#eef3f7;color:#465565;font-size:13px;cursor:pointer}}
.remember{{display:flex;align-items:center;gap:8px;margin-top:14px;color:#5f6f7f;font-size:14px}}.remember input{{width:18px;height:18px;padding:0}}
button{{width:100%;height:44px;margin-top:20px;border:0;border-radius:8px;background:#0cbfbd;color:#fff;font-size:16px;font-weight:700;cursor:pointer}}
.err{{min-height:20px;margin-top:12px;color:#d93025;text-align:center;font-size:13px}}.link{{display:block;text-align:center;margin-top:14px;color:#0b6fbd;text-decoration:none;font-size:14px}}
</style>
</head>
<body>
  <form class="box" method="post">
    <h1>祺航本地项目</h1>
    <p>请输入账号密码后继续访问</p>
    <label>账号</label>
    <input name="username" autocomplete="username" autofocus>
    <label>密码</label>
    <div class="password-wrap">
      <input id="password" name="password" type="password" autocomplete="current-password">
      <button class="eye-btn" type="button" onclick="const p=document.getElementById('password');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'显示':'隐藏';">显示</button>
    </div>
    <label class="remember"><input name="remember" type="checkbox" value="1" checked> 保持登录</label>
    <button type="submit">登录</button>
    <div class="err">{error}</div>
    <a class="link" href="/register">申请注册账号</a>
  </form>
</body>
</html>'''

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    error = ''
    ok = ''
    username = ''
    name = ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        name = (request.form.get('name') or '').strip()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        if not username or not name or not password:
            error = '请完整填写登录账户、姓名和密码'
        elif password != confirm:
            error = '两次输入的密码不一致'
        elif len(password) < 6:
            error = '密码至少需要 6 位'
        else:
            try:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with _auth_conn() as conn:
                    conn.execute(
                        '''INSERT INTO users (username, password_hash, name, role, status, created_at, approved_at)
                           VALUES (?, ?, ?, 'user', 'pending', ?, '')''',
                        (username, _hash_password(password), name, now),
                    )
                    conn.commit()
                ok = '注册申请已提交，请等待管理员在设置页审核通过'
                username = ''
                name = ''
            except sqlite3.IntegrityError:
                error = '这个登录账户已经存在'
            except Exception as e:
                error = f'注册失败：{e}'
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>申请注册账号</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#eef3f7;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1f2d3d}}
.box{{width:min(92vw,420px);background:#fff;border:1px solid #dde6ee;border-radius:14px;box-shadow:0 18px 45px rgba(30,55,90,.14);padding:28px}}
h1{{font-size:24px;margin:0 0 8px;text-align:center}}p{{margin:0 0 22px;text-align:center;color:#788797;font-size:14px}}
label{{display:block;font-size:13px;color:#5f6f7f;margin:14px 0 6px}}input{{width:100%;height:44px;border:1px solid #cfd9e3;border-radius:8px;padding:0 12px;font-size:16px;outline:none}}input:focus{{border-color:#0cbfbd;box-shadow:0 0 0 3px rgba(12,191,189,.14)}}
button{{width:100%;height:44px;margin-top:20px;border:0;border-radius:8px;background:#0cbfbd;color:#fff;font-size:16px;font-weight:700;cursor:pointer}}
.err{{min-height:20px;margin-top:12px;color:#d93025;text-align:center;font-size:13px}}.ok{{min-height:20px;margin-top:12px;color:#19a453;text-align:center;font-size:13px}}.link{{display:block;text-align:center;margin-top:14px;color:#0b6fbd;text-decoration:none;font-size:14px}}
</style>
</head>
<body>
  <form class="box" method="post">
    <h1>申请注册账号</h1>
    <p>提交后需要管理员在设置页审核通过</p>
    <label>登录账户</label>
    <input name="username" value="{username}" autocomplete="username" autofocus>
    <label>姓名</label>
    <input name="name" value="{name}" autocomplete="name">
    <label>密码</label>
    <input name="password" type="password" autocomplete="new-password">
    <label>确认密码</label>
    <input name="confirm" type="password" autocomplete="new-password">
    <button type="submit">提交注册申请</button>
    <div class="err">{error}</div>
    <div class="ok">{ok}</div>
    <a class="link" href="/login">返回登录</a>
  </form>
</body>
</html>'''

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API 结果缓存（按 code，24小时过期）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _register_page_v2():
    error = ''
    ok = ''
    username = ''
    name = ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        name = (request.form.get('name') or '').strip()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        if not username or not name or not password:
            error = '请完整填写登录账户、密码、确认密码和姓名'
        elif password != confirm:
            error = '两次输入的密码不一致'
        elif len(password) < 6:
            error = '密码至少需要 6 位'
        else:
            try:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with _auth_conn() as conn:
                    conn.execute(
                        '''INSERT INTO users (username, password_hash, name, role, status, created_at, approved_at)
                           VALUES (?, ?, ?, 'user', 'pending', ?, '')''',
                        (username, _hash_password(password), name, now),
                    )
                    conn.commit()
                ok = '申请已提交，等待管理员审核。审核通过后就可以登录使用。'
                username = ''
                name = ''
            except sqlite3.IntegrityError:
                error = '这个登录账户已经存在，请换一个账户名'
            except Exception as e:
                error = f'注册失败：{e}'
    success_block = f'<div class="ok">{html.escape(ok)}</div>' if ok else ''
    error_block = f'<div class="err">{html.escape(error)}</div>' if error else ''
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>申请注册账号</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#eef3f7;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1f2d3d}}
.box{{width:min(92vw,420px);background:#fff;border:1px solid #dde6ee;border-radius:14px;box-shadow:0 18px 45px rgba(30,55,90,.14);padding:28px}}
h1{{font-size:24px;margin:0 0 8px;text-align:center}}p{{margin:0 0 22px;text-align:center;color:#788797;font-size:14px}}
label{{display:block;font-size:13px;color:#5f6f7f;margin:14px 0 6px}}input{{width:100%;height:44px;border:1px solid #cfd9e3;border-radius:8px;padding:0 12px;font-size:16px;outline:none}}input:focus{{border-color:#0cbfbd;box-shadow:0 0 0 3px rgba(12,191,189,.14)}}
button{{width:100%;height:44px;margin-top:20px;border:0;border-radius:8px;background:#0cbfbd;color:#fff;font-size:16px;font-weight:700;cursor:pointer}}
.err{{margin-top:12px;color:#d93025;text-align:center;font-size:14px}}.ok{{margin-top:14px;padding:12px;border-radius:8px;background:#e9f8ef;color:#16823c;text-align:center;font-size:14px;line-height:1.6}}.link{{display:block;text-align:center;margin-top:14px;color:#0b6fbd;text-decoration:none;font-size:14px}}
</style>
</head>
<body>
  <form class="box" method="post">
    <h1>申请注册账号</h1>
    <p>提交后需要管理员在设置页审核通过</p>
    <label>登录账户</label>
    <input name="username" value="{html.escape(username)}" autocomplete="username" autofocus>
    <label>姓名</label>
    <input name="name" value="{html.escape(name)}" autocomplete="name">
    <label>密码</label>
    <input name="password" type="password" autocomplete="new-password">
    <label>确认密码</label>
    <input name="confirm" type="password" autocomplete="new-password">
    <button type="submit">提交注册申请</button>
    {error_block}
    {success_block}
    <a class="link" href="/login">返回登录</a>
  </form>
</body>
</html>'''


app.view_functions['register_page'] = _register_page_v2


@app.route('/auth/me', methods=['GET'])
def auth_me():
    user = _current_user()
    return jsonify({'success': True, 'user': user, 'is_admin': bool(user and user.get('is_admin'))})


@app.route('/admin/users', methods=['GET'])
def admin_users():
    with _auth_conn() as conn:
        rows = conn.execute(
            '''SELECT id, username, name, role, status, created_at, approved_at
               FROM users
               ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, id DESC'''
        ).fetchall()
    return jsonify({'success': True, 'list': [_row_to_user(row) for row in rows]})


@app.route('/admin/users/<int:user_id>/approve', methods=['POST'])
def admin_user_approve(user_id):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _auth_conn() as conn:
        row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '用户不存在'}), 404
        if row['role'] == 'admin':
            return jsonify({'success': False, 'error': '管理员账号不需要审核'}), 400
        conn.execute(
            "UPDATE users SET status = 'active', approved_at = ? WHERE id = ?",
            (now, user_id),
        )
        conn.commit()
    return jsonify({'success': True})


@app.route('/admin/users/<int:user_id>/reject', methods=['POST'])
def admin_user_reject(user_id):
    with _auth_conn() as conn:
        row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '用户不存在'}), 404
        if row['role'] == 'admin':
            return jsonify({'success': False, 'error': '不能拒绝管理员账号'}), 400
        conn.execute("UPDATE users SET status = 'rejected' WHERE id = ?", (user_id,))
        conn.commit()
    return jsonify({'success': True})


_CACHE_FILE  = os.path.join(_DATA_BASE, '_api_cache', 'code_cache.json')
_CACHE_TTL_H = 24
_cache_lock   = threading.Lock()
_history_lock = threading.Lock()   # generation_history.json 写锁

def _load_code_cache():
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_code_cache(cache):
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    with _cache_lock:
        with open(_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

def _cache_valid(entry):
    """检查缓存条目是否在 TTL 内有效"""
    ts = entry.get('fetched_at', '')
    if not ts:
        return False
    try:
        return datetime.now() - datetime.strptime(ts, '%Y-%m-%d %H:%M:%S') < timedelta(hours=_CACHE_TTL_H)
    except Exception:
        return False
CORS(app, origins='*')   # 允许 Chrome 扩展跨域调用

@app.after_request
def allow_private_network(response):
    # Chrome Private Network Access：允许公网页面访问本地服务
    response.headers['Access-Control-Allow-Private-Network'] = 'true'
    return response

def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, '')).strip() or default)
    except Exception:
        return default


def _workers_disabled():
    return str(os.environ.get('QIHANG_DISABLE_WORKERS', '')).strip().lower() in ('1', 'true', 'yes', 'on')


PORT = _env_int('QIHANG_PORT', 5008)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'port': PORT, 'build': APP_BUILD})


def _fetch_safe_exchange_rate():
    """
    从国家外汇管理局获取本月第一个工作日的美元汇率中间价。
    返回 (rate: float, date_str: str) 或 (None, None)。
    """
    from datetime import date, timedelta
    import urllib.request as _req
    from urllib.parse import urlencode
    import ssl as _ssl
    import re as _re

    today = date.today()
    d = date(today.year, today.month, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    end_d = min(d + timedelta(days=13), today)
    payload = urlencode({
        'startDate': d.strftime('%Y-%m-%d'),
        'endDate':   end_d.strftime('%Y-%m-%d'),
        'queryYN':   'true',
    }).encode('utf-8')

    _ctx = _ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode    = _ssl.CERT_NONE

    req = _req.Request(
        'https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do',
        data=payload,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent':   'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer':      'https://www.safe.gov.cn/safe/rmbhlzjj/index.html',
        }
    )
    with _req.urlopen(req, timeout=12, context=_ctx) as r:
        html = r.read().decode('utf-8', errors='replace')

    rows = _re.findall(r'<tr[^>]*>(.*?)</tr>', html, _re.DOTALL | _re.IGNORECASE)
    for row in rows:
        cells = _re.findall(r'<td[^>]*>(.*?)</td>', row, _re.DOTALL | _re.IGNORECASE)
        cells = [_re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if len(cells) < 2:
            continue
        date_cell = cells[0]
        usd_cell  = cells[1]
        if not _re.match(r'\d{4}[-/]\d{2}[-/]\d{2}', date_cell):
            continue
        try:
            usd_val  = float(usd_cell)
            rate     = round(usd_val / 100, 4)
            row_date = _re.sub(r'[-/]', '-', date_cell[:10])
            return rate, row_date
        except ValueError:
            continue
    return None, None


@app.route('/get-exchange-rate', methods=['GET'])
def get_exchange_rate():
    """
    从国家外汇管理局获取本月第一个工作日（Mon-Fri）的美元汇率中间价。
    接口：POST https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do
    返回 HTML 表格，美元列值为"100美元兑人民币"，除以 100 得实际汇率。
    """
    try:
        rate, row_date = _fetch_safe_exchange_rate()
        if rate:
            print(f'[RATE] SAFE 汇率 {row_date}: {rate}')
            return jsonify({'success': True, 'rate': rate,
                            'date': row_date, 'source': 'SAFE外汇局'})
        from datetime import date
        d = date.today().replace(day=1)
        return jsonify({'success': False,
                        'error': f'外汇局未找到 {d} 起的汇率数据，请手动填写'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/browse-folder', methods=['GET'])
def browse_folder():
    """调用系统原生文件夹选择对话框（macOS 用 osascript，Windows 用 tkinter）"""
    import platform
    system = platform.system()

    if system == 'Darwin':  # macOS：用 AppleScript，不依赖 tkinter
        try:
            import subprocess
            script = 'POSIX path of (choose folder with prompt "选择报关文件保存文件夹")'
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=120
            )
            folder = result.stdout.strip().rstrip('/')
            if result.returncode == 0 and folder:
                return jsonify({'success': True, 'path': folder})
            return jsonify({'success': False, 'path': ''})
        except subprocess.TimeoutExpired:
            return jsonify({'success': False, 'path': ''})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    else:  # Windows：用 tkinter
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            folder = filedialog.askdirectory(title='选择报关文件保存文件夹')
            root.destroy()
            if folder:
                return jsonify({'success': True, 'path': folder})
            return jsonify({'success': False, 'path': ''})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})


@app.route('/browse-file', methods=['GET'])
def browse_file():
    """调用系统原生文件选择对话框（macOS 用 osascript，Windows 用 tkinter）"""
    import platform
    system = platform.system()

    if system == 'Darwin':
        try:
            import subprocess
            script = 'POSIX path of (choose file with prompt "选择退税联 PDF 文件" of type {"pdf"})'
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=120
            )
            path = result.stdout.strip()
            if result.returncode == 0 and path:
                return jsonify({'success': True, 'path': path})
            return jsonify({'success': False, 'path': ''})
        except subprocess.TimeoutExpired:
            return jsonify({'success': False, 'path': ''})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    else:  # Windows
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askopenfilename(
                title='选择退税联 PDF 文件',
                filetypes=[('PDF 文件', '*.pdf'), ('所有文件', '*.*')]
            )
            root.destroy()
            if path:
                return jsonify({'success': True, 'path': path})
            return jsonify({'success': False, 'path': ''})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})


def _cleanup_old_generated_downloads(max_age_hours=24):
    now = time.time()
    for folder in (_GENERATED_DOWNLOAD_DIR, _GENERATED_WORK_DIR):
        try:
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                try:
                    if now - os.path.getmtime(path) < max_age_hours * 3600:
                        continue
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass


def _zip_generated_folder(folder, invoice_no=''):
    os.makedirs(_GENERATED_DOWNLOAD_DIR, exist_ok=True)
    _cleanup_old_generated_downloads()
    safe_invoice = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in str(invoice_no or 'generated'))[:60]
    filename = f'报关单_{safe_invoice}_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{secrets.token_hex(4)}.zip'
    zip_path = os.path.join(_GENERATED_DOWNLOAD_DIR, filename)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(folder):
            for file in files:
                full = os.path.join(root, file)
                rel = os.path.relpath(full, folder)
                zf.write(full, rel)
    return filename, zip_path


@app.route('/generated-download/<path:filename>', methods=['GET'])
def generated_download(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(_GENERATED_DOWNLOAD_DIR, safe_name)
    if not os.path.isfile(path):
        return jsonify({'success': False, 'error': '下载文件不存在或已过期，请重新生成'}), 404
    return send_file(path, as_attachment=True, download_name=safe_name, mimetype='application/zip')


@app.route('/generate', methods=['POST'])
def generate():
    """
    请求体 JSON：
    {
        "items": [
            {"code": "T.26431T-24", "unit": "PAC", "qty": 1.0, "rmb_price": 89.0},
            ...
        ],
        "exchange_rate": 7.2,
        "invoice_no": "LK00022",
        "target_total_usd": null,      // 可选，目标总金额
        "output_path": "C:\\Users\\xxx\\Desktop\\export"
    }
    """
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({'success': False, 'error': '无效的请求体'}), 400

        required = ['items', 'exchange_rate']
        for field in required:
            if field not in data:
                return jsonify({'success': False, 'error': f'缺少字段：{field}'}), 400

        output_path = str(data.get('output_path') or '').strip()
        download_mode = bool(data.get('download_mode')) or not output_path
        if download_mode:
            os.makedirs(_GENERATED_WORK_DIR, exist_ok=True)
            work_name = f'gen_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{secrets.token_hex(4)}'
            output_path = os.path.join(_GENERATED_WORK_DIR, work_name)
            data['output_path'] = output_path

        # 调试日志：记录前端传来的原始数据
        items = data.get('items', [])
        if items:
            print(f'[DEBUG] 收到 {len(items)} 个商品，前3个: ')
            for it in items[:3]:
                print(f'  code={it.get("code")}, qty={it.get("qty")}, unit={it.get("unit")}, rmb_price={it.get("rmb_price")}, boxes={it.get("boxes")}')
            # 找 F.3153F-296
            for it in items:
                if '3153F' in str(it.get('code', '')):
                    print(f'  [TARGET] code={it.get("code")}, qty={it.get("qty")}, unit={it.get("unit")}, boxes={it.get("boxes")}')

        # 提取 order_refs 信息（单号/备注/账套，用于子文件夹命名 + 限定查询账套）
        order_refs = data.get('order_refs') or []
        first_ref = order_refs[0] if order_refs else {}
        data['order_no']     = (first_ref.get('number') or first_ref.get('id') or '').strip()
        data['order_remark'] = (first_ref.get('remark') or '').strip()

        # 根据调拨单账套决定只查对应的 JDY 账套，不允许跨账套查任何信息
        order_account = (first_ref.get('account') or '').strip()
        _cfg2 = _load_jdy_config2()
        if order_account and order_account == _cfg2.get('name'):
            preferred_cli_fn = _ensure_jdy_client2
        elif order_account:
            preferred_cli_fn = _ensure_jdy_client
        else:
            preferred_cli_fn = None  # 未知账套，仍查两个
        _cli_label = 'cli2' if preferred_cli_fn is _ensure_jdy_client2 else ('cli1' if preferred_cli_fn else '两个')
        print(f'[SUPPLIER] 调拨单账套={order_account!r}, 使用客户端={_cli_label}')

        # 多账套混选时，每个 item 携带自己的 account，按 code 建立精确账套映射
        code_account_map = {}
        for it in items:
            _code = (it.get('code') or '').strip()
            _acct = (it.get('account') or '').strip()
            if _code and _acct:
                if _acct == _cfg2.get('name'):
                    code_account_map[_code] = _ensure_jdy_client2
                else:
                    code_account_map[_code] = _ensure_jdy_client
        if code_account_map:
            _accts = set(fn.__name__ for fn in code_account_map.values())
            print(f'[SUPPLIER] per-code 账套映射: {len(code_account_map)} 个商品, 账套: {_accts}')

        # ── 手工填写的 proLicense（格式：品名*材料*分类简称，来自前端弹窗）──
        manual_licenses = data.get('manual_licenses') or []
        if manual_licenses:
            from ai_identify import fill_from_license, _load_config, _find_excel
            from jdy_api import parse_dimensions as _parse_dims
            _ai_cfg     = _load_ai_config()
            _excel_path = _find_excel(_ai_cfg)
            for ml in manual_licenses:
                _code    = (ml.get('code') or '').strip()
                _raw     = (ml.get('pro_license') or '').strip()   # 格式：品名*材料*分类简称
                if not (_code and _raw):
                    continue
                # 拆分：至少需要 品名 和 材料
                _parts   = [p.strip() for p in _raw.split('*')]
                _name    = _parts[0] if len(_parts) > 0 else ''
                _mat     = _parts[1] if len(_parts) > 1 else ''
                _cat_str = _parts[2] if len(_parts) > 2 else ''
                if not (_name and _mat):
                    print(f'[WARN] manual_license {_code}: 格式错误 "{_raw}"，需要 品名*材料')
                    continue
                # 从 JDY 取 product id / dimensions 等额外字段，并回写 proLicense
                _extra = {}
                _prod_id = None
                try:
                    _cli_fn = (code_account_map or {}).get(_code) or preferred_cli_fn
                    _cli = _cli_fn() if _cli_fn else (_ensure_jdy_client() or _ensure_jdy_client2())
                    if _cli:
                        _prod = _cli.get_product_by_code(_code)
                        if _prod:
                            _prod_id = _prod.get('id')
                            _cfg2  = _load_jdy_config2()
                            _acct  = _cfg2['name'] if _cli is _ensure_jdy_client2() else _load_jdy_config()['name']
                            _extra = {
                                'category':   _cat_str or _prod.get('categoryName') or '',
                                'dimensions': _parse_dims(_prod.get('registrationNo') or ''),
                                'account':    _acct,
                            }
                            # 回写 proLicense 到精斗云
                            try:
                                _cli.update_product_pro_license(_prod_id, _raw)
                                print(f'[LICENSE MANUAL] {_code} proLicense 已回写 JDY: {_raw!r}')
                            except Exception as _we:
                                print(f'[WARN] {_code} 回写 JDY 失败: {_we}')
                except Exception as _e:
                    print(f'[WARN] manual_license extra fields {_code}: {_e}')
                if _excel_path:
                    try:
                        fill_from_license(_excel_path, _code, _name, _mat,
                                          extra_fields=_extra, cat_str=_cat_str or None)
                        print(f'[LICENSE MANUAL] {_code} 手工补全完成: {_name}/{_mat}' +
                              (f', 分类简称: {_cat_str}' if _cat_str else ''))
                    except Exception as _e:
                        print(f'[WARN] manual_license fill {_code}: {_e}')

        # ── 生成前自动档案补全：缺失商品先用 proLicense 填表（限定同账套）──
        auto_filled, no_license_items = _auto_license_fill(
            items, preferred_cli_fn=preferred_cli_fn, code_account_map=code_account_map)
        if auto_filled:
            print(f'[LICENSE AUTO] 自动补全 {len(auto_filled)} 个缺失商品: {auto_filled}')

        # proLicense 为空的商品 → 让前端弹窗让用户手工填写，再次提交
        if no_license_items and not manual_licenses:
            return jsonify({
                'success': True,
                'needs_manual_license': True,
                'no_license_items': no_license_items,
            })

        # 查询供应商映射（用于生成采购合同）
        supplier_overrides = data.get('supplier_overrides') or {}
        try:
            supplier_map, needs_select = _lookup_suppliers(
                items,
                preferred_cli_fn=preferred_cli_fn,
                supplier_overrides=supplier_overrides,
                code_account_map=code_account_map,
            )
            data['supplier_map'] = supplier_map
            print(f'[SUPPLIER] 查到 {len(supplier_map)} 个商品的供应商，待选 {len(needs_select)} 个')
        except Exception as e:
            print(f'[WARN] 供应商查询失败: {e}')
            data['supplier_map'] = {}
            needs_select = {}

        # 若有商品供应商待选，先返回给前端弹窗确认
        if needs_select:
            return jsonify({
                'success': True,
                'needs_supplier_select': True,
                'select_items': needs_select,
            })

        # 批量查 API 尺寸（registrationNo），缓存命中直接跳过
        try:
            cache = _load_code_cache()
            all_codes = list({(it.get('code') or '').strip() for it in items if it.get('code')})

            # 区分：缓存有效 vs 需要重新拉取
            api_dims_map = {}
            for c in all_codes:
                if _cache_valid(cache.get(c, {})) and 'dims' in cache.get(c, {}):
                    api_dims_map[c] = cache[c]['dims']   # 直接用缓存

            codes_need_dims = [c for c in all_codes if c not in api_dims_map]

            if codes_need_dims:
                _dim_fn_to_codes = {}
                _dim_no_acct = []
                for _code in codes_need_dims:
                    _fn = code_account_map.get(_code) or preferred_cli_fn
                    if _fn:
                        _dim_fn_to_codes.setdefault(_fn, []).append(_code)
                    else:
                        _dim_no_acct.append(_code)
                for _fn2 in [_ensure_jdy_client, _ensure_jdy_client2]:
                    _dim_fn_to_codes.setdefault(_fn2, []).extend(_dim_no_acct)

                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                for _fn, _codes in _dim_fn_to_codes.items():
                    if not _codes:
                        continue
                    try:
                        cli = _fn()
                        if not cli:
                            continue
                        for i in range(0, len(_codes), 30):
                            batch = cli.get_products_by_codes(_codes[i:i+30])
                            for prod in (batch or []):
                                c   = prod.get('number') or prod.get('code', '')
                                reg = prod.get('registrationNo', '')
                                if c and reg:
                                    d = jdy_api.parse_dimensions(reg)
                                    if d:
                                        api_dims_map[c] = d
                                        # 写入磁盘缓存
                                        if c not in cache:
                                            cache[c] = {}
                                        cache[c]['dims'] = d
                                        cache[c].setdefault('fetched_at', now_str)
                    except Exception as e:
                        print(f'[WARN] api_dims_map {_fn.__name__}: {e}')
                _save_code_cache(cache)

            data['api_dims_map'] = api_dims_map
            print(f'[DIMS] API 尺寸 {len(api_dims_map)} 个（其中 {len(all_codes)-len(codes_need_dims)} 个命中缓存）')
        except Exception as e:
            print(f'[WARN] api_dims 失败: {e}')
            data['api_dims_map'] = {}

        # total_cbm：前端可传入，默认满柜 68CBM
        total_cbm = data.get('total_cbm')
        if total_cbm is None:
            total_cbm = 68.0
        data['total_cbm'] = float(total_cbm)

        result = generate_all(data)
        result['auto_filled'] = auto_filled   # 返回给前端提示
        # 记录哪些调拨单被用于生成报关单
        order_refs = data.get('order_refs') or []
        if order_refs:
            _record_gen_history(order_refs)
        if download_mode:
            zip_name, zip_path = _zip_generated_folder(output_path, data.get('invoice_no') or '')
            result['download_url'] = url_for('generated_download', filename=zip_name)
            result['download_name'] = zip_name
            result['download_size'] = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
        return jsonify({'success': True, **result})

    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


@app.route('/ai-config', methods=['GET'])
def ai_config_get():
    try:
        return jsonify({'success': True, **get_config_for_ui()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/ai-config', methods=['POST'])
def ai_config_set():
    try:
        data = request.get_json(force=True) or {}
        save_config(data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/ai-codes', methods=['GET'])
def ai_codes():
    """返回 Excel B 列所有已识别的编码集合，供前端批量预判断"""
    try:
        from ai_identify import _load_config, _find_excel
        config = _load_config()
        excel_path = _find_excel(config)
        if not excel_path:
            return jsonify({'success': True, 'codes': []})
        codes = get_existing_codes(excel_path)
        return jsonify({'success': True, 'codes': codes})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/ai-identify', methods=['POST'])
def ai_identify():
    """
    请求体 JSON：
    {
        "code":      "T.F240T-308",
        "name":      "荔枝纹斜挎包",    // 可选
        "category":  "Lady Bag",        // 可选
        "image_url": "https://...",     // 与 image_b64 二选一
        "image_b64": "..."
    }
    """
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({'success': False, 'error': '无效请求体'}), 400
        result = identify_and_save(data)
        return jsonify({'success': True, **result})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# 精斗云 (JDY) 相关端点
# ──────────────────────────────────────────────────────────────────────────────

def _runtime_config_candidates():
    candidates = []
    env_path = os.environ.get('QIHANG_CONFIG_PATH') or os.environ.get('QIHANG_CONFIG_FILE')
    if env_path:
        candidates.append(env_path)
    candidates.extend([
        os.path.join(os.getcwd(), 'ai_config.json'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_config.json'),
        os.path.join(_DATA_BASE, 'ai_config.json'),
        os.path.join(_EXE_DIR, 'ai_config.json'),
    ])
    try:
        from ai_identify import _CONFIG_FILE
        candidates.append(_CONFIG_FILE)
    except Exception:
        pass
    candidates.append(os.path.join(os.path.dirname(_DATA_BASE), 'ai_config.json'))
    result = []
    seen = set()
    for path in candidates:
        if not path:
            continue
        full = os.path.abspath(path)
        key = os.path.normcase(full)
        if key not in seen:
            seen.add(key)
            result.append(full)
    return result


def _runtime_config_file():
    for path in _runtime_config_candidates():
        if os.path.exists(path):
            return path
    return os.path.join(_DATA_BASE, 'ai_config.json')


_RUNTIME_CONFIG_LOAD_ERROR = ''


def _load_runtime_config():
    global _RUNTIME_CONFIG_LOAD_ERROR
    _RUNTIME_CONFIG_LOAD_ERROR = ''
    path = _runtime_config_file()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
        except Exception as exc:
            _RUNTIME_CONFIG_LOAD_ERROR = f'{type(exc).__name__}: {exc}'
    return {}


def _runtime_config_debug():
    path = _runtime_config_file()
    cfg = _load_runtime_config()
    keys = {
        'jdy_app_key': bool(cfg.get('jdy_app_key')),
        'jdy_client_id': bool(cfg.get('jdy_client_id')),
        'jdy_db_id': bool(cfg.get('jdy_db_id')),
        'jdy_domain': bool(cfg.get('jdy_domain')),
        'jdy_app_secret': bool(cfg.get('jdy_app_secret')),
        'jdy_client_secret': bool(cfg.get('jdy_client_secret')),
        'jdy2_app_key': bool(cfg.get('jdy2_app_key')),
        'jdy2_client_id': bool(cfg.get('jdy2_client_id')),
        'jdy2_db_id': bool(cfg.get('jdy2_db_id')),
        'jdy2_domain': bool(cfg.get('jdy2_domain')),
        'jdy2_app_secret': bool(cfg.get('jdy2_app_secret')),
        'jdy2_client_secret': bool(cfg.get('jdy2_client_secret')),
    }
    return {
        'path': path,
        'exists': os.path.exists(path),
        'cwd': os.getcwd(),
        'has_keys': keys,
        'load_error': _RUNTIME_CONFIG_LOAD_ERROR,
    }


def _save_runtime_config(cfg):
    path = _runtime_config_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _load_jdy_config():
    """从 ai_config.json 读取账套1 JDY 配置"""
    cfg = _load_runtime_config()
    return {
        'name':          cfg.get('jdy_name', '饰品'),
        'client_id':     cfg.get('jdy_client_id', ''),
        'app_key':       cfg.get('jdy_app_key', ''),
        'app_secret':    cfg.get('jdy_app_secret', ''),
        'db_id':         cfg.get('jdy_db_id', ''),
        'domain':        cfg.get('jdy_domain', ''),
        'app_signature':  cfg.get('jdy_app_signature', ''),
        'client_secret':  cfg.get('jdy_client_secret', ''),
    }


def _load_jdy_config2():
    """从 ai_config.json 读取账套2 JDY 配置"""
    cfg = _load_runtime_config()
    return {
        'name':          cfg.get('jdy2_name', '箱包'),
        'client_id':     cfg.get('jdy2_client_id', ''),
        'app_key':       cfg.get('jdy2_app_key', ''),
        'app_secret':    cfg.get('jdy2_app_secret', ''),
        'db_id':         cfg.get('jdy2_db_id', ''),
        'domain':        cfg.get('jdy2_domain', ''),
        'app_signature':  cfg.get('jdy2_app_signature', ''),
        'client_secret':  cfg.get('jdy2_client_secret', ''),
    }


def _save_jdy_config(updates):
    """将 JDY 配置合并写回 ai_config.json"""
    cfg = _load_runtime_config()
    secret_keys = {
        'jdy_app_secret', 'jdy_client_secret',
        'jdy2_app_secret', 'jdy2_client_secret',
    }
    for k, v in updates.items():
        if k in secret_keys and isinstance(v, str) and not v.strip():
            continue
        if v is not None:
            cfg[k] = v
    _save_runtime_config(cfg)


def _ensure_jdy_client():
    """返回已初始化的账套1 JDYClient，或根据配置新建"""
    cli = jdy_api.get_client()
    if cli:
        return cli
    cfg = _load_jdy_config()
    if not all([cfg['client_id'], cfg['app_key'], cfg['db_id']]):
        return None
    if not cfg['app_secret'] and not cfg['client_secret']:
        return None
    if not cfg['app_secret']:
        cfg['app_secret'] = cfg['client_secret']
    return jdy_api.init_client(
        cfg['client_id'], cfg['app_key'], cfg['app_secret'],
        cfg['db_id'], cfg.get('domain', ''), cfg.get('app_signature', ''),
        cfg.get('client_secret', '')
    )


def _ensure_jdy_client2():
    """返回已初始化的账套2 JDYClient，或根据配置新建（未配置返回 None）"""
    cli = jdy_api.get_client_2()
    if cli:
        return cli
    cfg = _load_jdy_config2()
    # app_secret 可以用 client_secret 代替（正式版无轮换密钥时）
    if not all([cfg['client_id'], cfg['app_key'], cfg['db_id']]):
        return None
    if not cfg['app_secret'] and not cfg['client_secret']:
        return None
    # 若 app_secret 为空，用 client_secret 填充
    if not cfg['app_secret']:
        cfg['app_secret'] = cfg['client_secret']
    return jdy_api.init_client_2(
        cfg['client_id'], cfg['app_key'], cfg['app_secret'],
        cfg['db_id'], cfg.get('domain', ''), cfg.get('app_signature', ''),
        cfg.get('client_secret', '')
    )


def _jdy_domain_candidates(primary=''):
    candidates = [
        primary,
        'https://vip2-hz.jdy.com',
        'https://vip1-gd.jdy.com',
        'https://vip2-gd.jdy.com',
        'https://vip1-hz.jdy.com',
    ]
    result = []
    for item in candidates:
        val = str(item or '').strip().rstrip('/')
        if val and val not in result:
            result.append(val)
    return result


def _probe_jdy_business_route(cli):
    """Token success is not enough; probe a tiny business API to validate IDC routing."""
    try:
        result = cli._request(
            'POST',
            '/jdyscm/purchaseOrder/list',
            body={'filter': {'page': 1, 'pageSize': 1}},
            query=cli._api_query(),
            timeout=30,
        )
        code = result.get('errcode') or result.get('code')
        if code in (3000002000, 5001, 5003):
            return False, result.get('msg') or result.get('description_cn') or result.get('description') or str(code)
        return True, ''
    except Exception as e:
        return False, _short_sync_error(e) if '_short_sync_error' in globals() else str(e)[:200]


def _init_jdy_client_with_working_domain(idx, client_id, app_key, app_secret, db_id, domain, client_secret):
    init_fn = jdy_api.init_client_2 if int(idx) == 2 else jdy_api.init_client
    last_error = ''
    for candidate in _jdy_domain_candidates(domain):
        cli = init_fn(client_id, app_key, app_secret, db_id, candidate, '', client_secret)
        test = cli.test_connection()
        if not test.get('ok'):
            last_error = test.get('msg') or 'token test failed'
            continue
        ok, probe_error = _probe_jdy_business_route(cli)
        if ok:
            return cli, candidate, test.get('msg', '')
        last_error = probe_error
    return None, '', last_error or 'no working JDY business route'


def _refresh_jdy_auth_for_idx(idx):
    from ai_identify import _load_config, _CONFIG_FILE
    cfg_all = _load_config()
    pfx = '' if int(idx) == 1 else '2'
    client_id = cfg_all.get(f'jdy{pfx}_client_id', '')
    client_secret = cfg_all.get(f'jdy{pfx}_client_secret', '')
    outer_id = cfg_all.get(f'jdy{pfx}_outer_instance_id', '') or cfg_all.get(f'jdy{pfx}_db_id', '')
    if not all([client_id, client_secret, outer_id]):
        return {'success': False, 'error': f'账套{idx}缺少 client_id / client_secret / outer_instance_id'}
    result = jdy_api.push_app_authorize(client_id, client_secret, outer_id)
    items = result.get('data') or []
    if not items:
        return {'success': False, 'error': f'账套{idx}无授权数据'}
    item = items[0]
    app_key = item.get('appKey', '')
    app_secret = item.get('appSecret', '')
    domain = item.get('domain', '')
    account_id = str(item.get('accountId', ''))
    if not app_secret:
        return {'success': False, 'error': f'账套{idx}返回中无 appSecret'}
    updates = {
        f'jdy{pfx}_app_key': app_key,
        f'jdy{pfx}_app_secret': app_secret,
    }
    if domain:
        updates[f'jdy{pfx}_domain'] = domain
    if account_id:
        updates[f'jdy{pfx}_db_id'] = account_id
    cfg_all.update(updates)
    with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg_all, f, ensure_ascii=False, indent=2)
    cli, working_domain, msg = _init_jdy_client_with_working_domain(
        idx,
        client_id, app_key, app_secret,
        account_id or cfg_all.get(f'jdy{pfx}_db_id', ''),
        domain or cfg_all.get(f'jdy{pfx}_domain', ''),
        client_secret,
    )
    if not cli:
        return {'success': False, 'error': f'账套{idx}业务接口路由不可用：{msg}'}
    if working_domain and working_domain != cfg_all.get(f'jdy{pfx}_domain', ''):
        cfg_all[f'jdy{pfx}_domain'] = working_domain
        with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg_all, f, ensure_ascii=False, indent=2)
    return {'success': True, 'client': cli, 'message': msg, 'domain': working_domain}


def _refresh_jdy_auth_for_account_name(account_name):
    cfg1 = _load_jdy_config()
    cfg2 = _load_jdy_config2()
    if account_name == cfg2.get('name'):
        return _refresh_jdy_auth_for_idx(2)
    return _refresh_jdy_auth_for_idx(1)


def _client_for_sync_account(account_name, cli_fn):
    """同步任务默认使用现有授权，只有本地凭证不完整时才尝试刷新授权。"""
    cli = cli_fn()
    if cli:
        return cli
    auth = _refresh_jdy_auth_for_account_name(account_name)
    if auth.get('success'):
        return auth.get('client') or cli_fn()
    raise RuntimeError(auth.get('error') or 'JDY API not configured')


def _mask(s):
    s = s or ''
    return ('*' * max(len(s) - 4, 0) + s[-4:]) if len(s) >= 4 else '*' * len(s)


def _do_license_fill_one(code, account='', preferred_cli_fn=None):
    """
    查询单个商品的 proLicense，解析后写入 Excel。
    preferred_cli_fn：若提供，严格只查该账套，不跨账套兜底。
    返回 dict（含 status）或 None（跳过）。
    """
    from ai_identify import parse_prolicense, fill_from_license, _load_config, _find_excel
    from jdy_api import parse_dimensions

    cfg2 = _load_jdy_config2()
    if preferred_cli_fn:
        # 调拨单账套已知 → 严格限制，不跨账套
        cli = preferred_cli_fn()
    else:
        cli = _ensure_jdy_client2() if account == cfg2.get('name') else _ensure_jdy_client()
        if not cli:
            cli = _ensure_jdy_client() or _ensure_jdy_client2()
    if not cli:
        return {'code': code, 'status': 'no_client'}

    product = cli.get_product_by_code(code)
    if not product and not preferred_cli_fn:
        # 未指定账套时才允许跨账套兜底
        other = _ensure_jdy_client2() if cli is _ensure_jdy_client() else _ensure_jdy_client()
        if other:
            product = other.get_product_by_code(code)
            if product:
                cli = other
    if not product:
        return {'code': code, 'status': 'not_found'}

    pro_license = product.get('proLicense') or ''
    name_part, material_part = parse_prolicense(pro_license)
    if not name_part or not material_part:
        return {'code': code, 'status': 'no_license', 'proLicense': pro_license,
                'cn_name': (product.get('name') or '').strip()}

    cfg1   = _load_jdy_config()
    actual = cfg2.get('name') if cli is _ensure_jdy_client2() else cfg1.get('name')
    remark = product.get('remark') or ''
    try:
        conversion = float(remark.strip()) if remark.strip() else None
    except ValueError:
        conversion = None

    ai_cfg     = _load_ai_config()
    excel_path = _find_excel(ai_cfg)
    if not excel_path:
        return {'code': code, 'status': 'no_excel'}

    r = fill_from_license(
        excel_path, code, name_part, material_part,
        extra_fields={
            'category':   product.get('categoryName') or '',
            'dimensions': parse_dimensions(product.get('registrationNo') or ''),
            'conversion': conversion,
            'account':    actual,
        },
    )
    return {'status': 'ok', **r}


def _auto_license_fill(items, preferred_cli_fn=None, code_account_map=None):
    """
    生成前检查哪些商品不在基础资料里，自动用 proLicense 补全。
    code_account_map：{code: cli_fn}，优先级高于 preferred_cli_fn。
    返回 (filled_codes, no_license_items)
      filled_codes:    成功补全的 code 列表
      no_license_items: proLicense 为空、需要手工填写的 [{code, cn_name}]
    """
    try:
        from excel_gen import load_base_data
        base   = load_base_data()
        codes  = [it['code'] for it in items if it.get('code')]
        missing = [c for c in codes if c not in base]
        filled, no_license_items = [], []
        for code in missing:
            code_cli_fn = (code_account_map or {}).get(code) or preferred_cli_fn
            r = _do_license_fill_one(code, preferred_cli_fn=code_cli_fn)
            if r and r.get('status') == 'ok':
                filled.append(code)
            elif r and r.get('status') == 'no_license':
                no_license_items.append({'code': code, 'cn_name': r.get('cn_name', '')})
        return filled, no_license_items
    except Exception as e:
        print(f'[WARN] _auto_license_fill: {e}')
        return [], []


# 模块级缓存：服务器运行期间，同一商品编码不重复查询
# code -> {'supplierName', 'supplierNumber', 'origin_city'}  或  None（表示已查过但无结果）
_supplier_lookup_cache = {}
_city_lookup_cache     = {}  # supplierNumber -> origin_city


def _lookup_suppliers(items, preferred_cli_fn=None, supplier_overrides=None, code_account_map=None):
    """
    查询供应商信息。缓存（_api_cache/code_cache.json）命中直接返回，
    未命中的码用 5 线程并发调取 JDY API，结果写入磁盘缓存（24h 有效）。
    返回 (result_dict, needs_select_dict)
    """
    _SKIP_KW = ('国内展厅', '国内采购', '盘存')
    now_str   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    supplier_overrides = supplier_overrides or {}
    cache = _load_code_cache()

    # 去重
    seen, codes = set(), []
    for it in items:
        c = (it.get('code') or '').strip()
        if c and c not in seen:
            seen.add(c); codes.append(c)

    # 哪些码需要调 API（未缓存 或 已过期）
    def _po_cached(code):
        e = cache.get(code, {})
        return _cache_valid(e) and 'po_result' in e

    to_fetch = [c for c in codes if c not in supplier_overrides and not _po_cached(c)]

    # ── 并发抓取 ──────────────────────────────────────────────────────────
    def _fetch_one(code):
        import time as _t
        _code_cli = (code_account_map or {}).get(code) or preferred_cli_fn
        _cli_fns  = [_code_cli] if _code_cli else [_ensure_jdy_client, _ensure_jdy_client2]

        po_result   = None   # None=未找到, dict=找到, {'needs_select':[...]}=多供应商
        found_cli   = None
        found_snum  = None

        for cli_fn in _cli_fns:
            po = None
            for attempt in range(3):
                try:
                    cli = cli_fn()
                    if not cli:
                        break
                    po = cli.get_purchase_orders_by_product(code, page_size=10)
                    break
                except Exception as e:
                    err = str(e)
                    if any(k in err for k in ('Connection reset', 'Errno 54', 'Errno 104',
                                              'RemoteDisconnected', 'Connection aborted')) \
                            and attempt < 2:
                        _t.sleep(0.5)
                        continue
                    po = None
                    break

            if po is None:
                continue

            orders = [o for o in ((po or {}).get('list') or [])
                      if not any(kw in (o.get('supplierName') or '') for kw in _SKIP_KW)]
            if not orders:
                continue

            # 收集所有唯一编号（保序，名称去重前），供后续城市遍历用
            all_snums_ordered = []
            _seen_snum = set()
            for o in orders:
                snum = (o.get('supplierNumber') or '').strip()
                if snum and snum not in _seen_snum:
                    _seen_snum.add(snum)
                    all_snums_ordered.append(snum)

            unique = {}
            for o in orders:
                snum = (o.get('supplierNumber') or '').strip()
                sname = (o.get('supplierName') or '').strip()
                if snum and snum not in unique:
                    unique[snum] = sname

            # 按名称再去重：同名供应商只保留第一条（不同 supplierNumber 也算同一家）
            seen_names = set()
            deduped = {}
            for snum, sname in unique.items():
                if sname not in seen_names:
                    seen_names.add(sname)
                    deduped[snum] = sname
            unique = deduped

            if len(unique) > 1:
                # 多供应商时先过滤掉禁用的（status=0查不到 → 禁用）
                enabled_unique = {}
                for _k, _v in unique.items():
                    try:
                        if cli.get_supplier_by_number(_k, status=0):
                            enabled_unique[_k] = _v
                        else:
                            print(f'[SUPPLIER] {_k}({_v}) 已禁用，从候选列表移除')
                    except Exception:
                        enabled_unique[_k] = _v   # 查询异常时保留，不误删
                unique = enabled_unique

            if len(unique) > 1:
                po_result = {'needs_select': [{'supplierNumber': k, 'supplierName': v}
                                              for k, v in unique.items()]}
            elif len(unique) == 1:
                # 过滤后只剩一个，直接用，不用弹窗
                found_snum = list(unique.keys())[0]
                # 从 orders 里找这个 snum 对应的最新一条
                _matched = next((o for o in orders
                                 if (o.get('supplierNumber') or '').strip() == found_snum), orders[0])
                po_result  = {'supplierName':   (_matched.get('supplierName') or '').strip(),
                              'supplierNumber': found_snum}
                found_cli  = cli
            else:
                first = orders[0]
                po_result  = {'supplierName':   (first.get('supplierName') or '').strip(),
                              'supplierNumber': (first.get('supplierNumber') or '').strip()}
                found_cli  = cli
                found_snum = po_result['supplierNumber']

                # 若首选供应商已禁用（status=0查不到），从同名的其他编号里找启用的替换
                try:
                    _sup_check = found_cli.get_supplier_by_number(found_snum, status=0)
                    if _sup_check is None:   # status=0查不到 → 禁用
                        print(f'[SUPPLIER] {found_snum} 已禁用，尝试其他编号')
                        for _snum2 in all_snums_ordered:
                            if _snum2 == found_snum:
                                continue
                            _sup2 = found_cli.get_supplier_by_number(_snum2, status=0)
                            if _sup2:   # status=0能查到 → 启用
                                found_snum = _snum2
                                po_result['supplierNumber'] = _snum2
                                po_result['supplierName']   = (_sup2.get('name') or po_result['supplierName']).strip()
                                print(f'[SUPPLIER] 改用启用编号 {_snum2}')
                                break
                except Exception:
                    pass
            break

        # 查城市：遍历所有供应商编号，status=0只查启用的，找到有城市信息的为止
        origin_city = ''
        if found_cli and all_snums_ordered:
            for _snum_try in all_snums_ordered:
                try:
                    sup = found_cli.get_supplier_by_number(_snum_try, status=0)
                    if not sup:   # 禁用或不存在，跳过
                        continue
                    for c in (sup.get('contacts') or []):
                        city   = (c.get('city') or '').strip()
                        county = (c.get('county') or '').strip()
                        # 优先用县级市（更精确），fallback 到地级市
                        if county: origin_city = county; break
                        if city:   origin_city = city;   break
                    if origin_city:
                        break
                except Exception:
                    pass

        # 注意：不再 fallback '义乌市'，空串时让 process_items 用基础资料 H列的 origin
        return code, {
            'fetched_at':   now_str,
            'po_result':    po_result,
            'origin_city':  origin_city,
        }

    if to_fetch:
        print(f'[CACHE] {len(to_fetch)} 个码调取 API，'
              f'{len(codes) - len(to_fetch)} 个命中缓存，5线程并发')
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = {pool.submit(_fetch_one, c): c for c in to_fetch}
            for fut in as_completed(futs):
                try:
                    code, entry = fut.result()
                    cache[code] = entry
                except Exception as e:
                    print(f'[CACHE] fetch error: {e}')
        _save_code_cache(cache)
    else:
        print(f'[CACHE] 全部 {len(codes)} 个码命中缓存')

    # ── 构建返回值 ─────────────────────────────────────────────────────────
    supplier_order, counter = {}, [1]
    def _get_idx(name):
        if name not in supplier_order:
            supplier_order[name] = counter[0]; counter[0] += 1
        return supplier_order[name]

    result, needs_select = {}, {}
    for code in codes:
        # 用户已选定
        if code in supplier_overrides:
            ov = supplier_overrides[code]
            sname  = (ov.get('supplierName') or '未知供应商').strip()
            snumber = (ov.get('supplierNumber') or '').strip()
            # 城市：优先缓存
            city_entry = cache.get(code, {})
            origin_city = city_entry.get('origin_city', '义乌市') if _po_cached(code) else '义乌市'
            result[code] = {'supplierName': sname, 'supplierNumber': snumber,
                            'origin_city': origin_city, 'supplierIdx': _get_idx(sname)}
            continue

        entry = cache.get(code, {})
        po    = entry.get('po_result')

        if po is None:
            needs_select[code] = []
            print(f'[SUPPLIER] code={code} 未找到采购单')
            continue

        if 'needs_select' in po:
            needs_select[code] = po['needs_select']
            print(f'[SUPPLIER] code={code} 多供应商，需选择')
            continue

        sname = po.get('supplierName') or '未知供应商'
        result[code] = {
            'supplierName':   sname,
            'supplierNumber': po.get('supplierNumber', ''),
            'origin_city':    entry.get('origin_city', '义乌市'),
            'supplierIdx':    _get_idx(sname),
        }

    return result, needs_select


def _load_ai_config():
    return _load_runtime_config()


def _extract_supplier_address(supplier):
    """
    从供应商首要联系人中提取地址信息，返回 (district, address_detail)。
      district      → H 列（contacts[].county，即区）
      address_detail → R 列（contacts[].address，即详细地址）

    实测 JDY supplier/list 联系人字段：
      province / city / county（区）/ address（详细地址）/ isPrimary
    """
    contacts = supplier.get('contacts') or []
    if not contacts:
        return '', ''

    # 找首要联系人（isPrimary=true），否则取第一个
    primary = next((c for c in contacts if c.get('isPrimary')), contacts[0])

    district = (primary.get('county') or '').strip()
    detail   = (primary.get('address') or '').strip()
    return district, detail


# ── 生成历史记录（generation_history.json 与 ai_config.json 同目录）────────
_GEN_HISTORY_FILE = None

def _gh_file():
    global _GEN_HISTORY_FILE
    if _GEN_HISTORY_FILE is None:
        from ai_identify import _CONFIG_FILE
        _GEN_HISTORY_FILE = os.path.join(
            os.path.dirname(os.path.abspath(_CONFIG_FILE)), 'generation_history.json')
    return _GEN_HISTORY_FILE

def _load_gen_history():
    try:
        p = _gh_file()
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as fp:
                return json.load(fp)
    except Exception:
        pass
    return {}

def _record_gen_history(order_refs):
    """order_refs: [{id, account}, ...]  —  生成成功后调用"""
    if not order_refs:
        return
    import datetime
    with _history_lock:
        h = _load_gen_history()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for ref in order_refs:
            oid = str(ref.get('id', '')).strip()
            acc = str(ref.get('account', '')).strip()
            if not oid:
                continue
            key = f'{acc}::{oid}'
            if key not in h:
                h[key] = {'id': oid, 'account': acc, 'count': 0, 'last_at': ''}
            h[key]['count'] += 1
            h[key]['last_at'] = now
        with open(_gh_file(), 'w', encoding='utf-8') as fp:
            json.dump(h, fp, ensure_ascii=False, indent=2)
    print(f'[GEN] 已记录 {len(order_refs)} 张单的生成历史')


@app.route('/jdy-config', methods=['GET'])
def jdy_config_get():
    """返回两套 JDY 配置（敏感字段打码）"""
    try:
        cfg  = _load_jdy_config()
        cfg2 = _load_jdy_config2()
        return jsonify({
            'success': True,
            # 账套 1
            'name':       cfg.get('name', '饰品'),
            'client_id':  cfg.get('client_id', ''),
            'app_key':    cfg.get('app_key', ''),
            'db_id':      cfg.get('db_id', ''),
            'domain':     cfg.get('domain', ''),
            'app_secret_masked':    _mask(cfg.get('app_secret', '')),
            'client_secret_masked': _mask(cfg.get('client_secret', '')),
            'has_config': bool(cfg.get('client_id') and cfg.get('app_key')
                               and cfg.get('app_secret') and cfg.get('db_id')),
            # 账套 2
            'name2':       cfg2.get('name', '箱包'),
            'client_id2':  cfg2.get('client_id', ''),
            'app_key2':    cfg2.get('app_key', ''),
            'db_id2':      cfg2.get('db_id', ''),
            'domain2':     cfg2.get('domain', ''),
            'app_secret_masked2':    _mask(cfg2.get('app_secret', '')),
            'client_secret_masked2': _mask(cfg2.get('client_secret', '')),
            'has_config2': bool(cfg2.get('client_id') and cfg2.get('app_key')
                                and cfg2.get('app_secret') and cfg2.get('db_id')),
            'config_debug': _runtime_config_debug(),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/jdy-config', methods=['POST'])
def jdy_config_set():
    """保存 JDY 配置（idx=1 或 idx=2 区分账套）"""
    try:
        data = request.get_json(force=True) or {}
        idx  = int(data.get('idx', 1))   # 1=饰品, 2=箱包
        pfx  = '' if idx == 1 else '2'   # json key 前缀
        critical_keys = ('client_id', 'app_key', 'app_secret', 'client_secret', 'db_id', 'domain')
        if not any(str(data.get(k) or '').strip() for k in critical_keys):
            return jsonify({'success': False, 'error': '配置未加载，禁止保存空配置'}), 400
        updates = {}
        name = str(data.get('name') or '').strip()
        client_id = str(data.get('client_id') or '').strip()
        app_key = str(data.get('app_key') or '').strip()
        db_id = str(data.get('db_id') or '').strip()
        domain = str(data.get('domain') or '').strip()
        if name:
            updates[f'jdy{pfx}_name']          = name
        if client_id:
            updates[f'jdy{pfx}_client_id']     = client_id
        if app_key:
            updates[f'jdy{pfx}_app_key']       = app_key
        app_secret = str(data.get('app_secret') or '').strip()
        client_secret = str(data.get('client_secret') or '').strip()
        if app_secret:
            updates[f'jdy{pfx}_app_secret']    = app_secret
        if client_secret:
            updates[f'jdy{pfx}_client_secret'] = client_secret
        if db_id:
            updates[f'jdy{pfx}_db_id']         = db_id
            updates[f'jdy{pfx}_outer_instance_id'] = db_id
        if domain:
            updates[f'jdy{pfx}_domain']        = domain
        _save_jdy_config(updates)
        # 重新初始化对应客户端
        if idx == 1:
            cfg = _load_jdy_config()
            if cfg['client_id'] and cfg['app_key'] and cfg['app_secret'] and cfg['db_id']:
                jdy_api.init_client(
                    cfg['client_id'], cfg['app_key'], cfg['app_secret'],
                    cfg['db_id'], cfg.get('domain', ''), cfg.get('app_signature', ''),
                    cfg.get('client_secret', '')
                )
        else:
            cfg2 = _load_jdy_config2()
            if cfg2['client_id'] and cfg2['app_key'] and cfg2['app_secret'] and cfg2['db_id']:
                jdy_api.init_client_2(
                    cfg2['client_id'], cfg2['app_key'], cfg2['app_secret'],
                    cfg2['db_id'], cfg2.get('domain', ''), cfg2.get('app_signature', ''),
                    cfg2.get('client_secret', '')
                )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/jdy-webhook', methods=['GET', 'POST'])
def jdy_webhook():
    """
    接收精斗云实时授权推送（appKey / appSecret / domain / accountId 自动更新）
    配置地址：open.jdy.com → 应用详情 → 沙箱调试/企业授权 → 消息订阅
    请求格式参见官方文档：bizType=app_authorize
    """
    try:
        if request.method == 'GET':
            return jsonify({'success': True, 'message': 'JDY webhook endpoint is ready'})

        biz_type = (request.args.get('bizType') or '').strip()
        data = request.get_json(force=True) or {}
        if not biz_type:
            biz_type = data.get('bizType', '')

        print(f'[JDY Webhook] bizType={biz_type}, body={json.dumps(data, ensure_ascii=False)[:300]}')

        # app_authorize updates credentials; business messages are queued for slow local sync.
        if biz_type != 'app_authorize':
            queued = _enqueue_jdy_webhook_events(biz_type, data)
            return jsonify({'errcode': '0', 'description': 'ok',
                            'data': {'status': '0', 'msg': f'queued {queued}', 'type': biz_type}})

        items = data.get('data') or []
        if not items:
            return jsonify({'errcode': '0', 'description': 'ok',
                            'data': {'status': '0', 'msg': 'empty data', 'type': biz_type}})

        cfg1 = _load_jdy_config()
        cfg2 = _load_jdy_config2()

        for item in items:
            incoming_key = str(item.get('appKey', ''))
            account_id   = str(item.get('accountId', ''))

            # 按 appKey 精确匹配账套（最可靠），其次按 accountId
            if   incoming_key and incoming_key == cfg2.get('app_key', ''):
                pfx = '2'
            elif incoming_key and incoming_key == cfg1.get('app_key', ''):
                pfx = ''
            elif account_id and account_id == str(cfg2.get('db_id', '')):
                pfx = '2'
            elif account_id and account_id == str(cfg1.get('db_id', '')):
                pfx = ''
            else:
                # 无法匹配任何已知账套 → 跳过，避免覆盖错误账套
                print(f'[JDY Webhook] 跳过无法匹配的推送: appKey={incoming_key} accountId={account_id}')
                continue

            # 只更新会轮换的凭证（appKey / appSecret），不动 domain 和 dbId
            updates = {}
            if item.get('appKey'):    updates[f'jdy{pfx}_app_key']    = item['appKey']
            if item.get('appSecret'): updates[f'jdy{pfx}_app_secret'] = item['appSecret']

            if updates:
                _save_jdy_config(updates)
                print(f'[JDY Webhook] 账套{pfx or "1"}密钥更新: appKey={incoming_key[:8]}… accountId={account_id}')

        # 重新初始化两个客户端
        jdy_api._client_instance   = None
        jdy_api._client_instance_2 = None
        _ensure_jdy_client()
        _ensure_jdy_client2()

        return jsonify({'errcode': '0', 'description': 'success',
                        'data': {'status': '0', 'msg': 'success', 'type': biz_type}})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] jdy_webhook: {tb}')
        return jsonify({'errcode': '1', 'description': str(e),
                        'data': {'status': '1', 'msg': str(e), 'type': ''}}), 200


def _write_update_apply_script(script_path):
    ps = r'''
param(
  [string]$ZipPath,
  [string]$AppDir
)
$ErrorActionPreference = "Stop"
$port = 5008
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$updateDir = Join-Path $AppDir "_updates"
$log = Join-Path $updateDir "apply_update.log"
New-Item -ItemType Directory -Force -Path $updateDir | Out-Null
function Log([string]$msg) {
  ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) | Out-File -FilePath $log -Append -Encoding UTF8
}
function Stop-Watchdog() {
  try { schtasks /End /TN "QiHangLocalProjectWatchdog" /F | Out-Null } catch {}
  try { schtasks /Delete /TN "QiHangLocalProjectWatchdog" /F | Out-Null } catch {}
  try {
    $watchers = Get-CimInstance Win32_Process | Where-Object {
      ($_.Name -match "cmd|powershell") -and ($_.CommandLine -like "*--watch*")
    }
    foreach ($p in $watchers) {
      Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
      Log "Stopped watchdog PID $($p.ProcessId)"
    }
  } catch {
    Log "Stop watchdog scan failed: $($_.Exception.Message)"
  }
}
function Stop-Port([int]$p) {
  $conns = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Where-Object { $_.State -eq "Listen" }
  foreach ($c in $conns) {
    try {
      Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
      Log "Stopped PID $($c.OwningProcess) on port $p"
    } catch {
      Log "Failed to stop PID $($c.OwningProcess): $($_.Exception.Message)"
    }
  }
}
try {
  "`n--- update $timestamp ---" | Out-File -FilePath $log -Append -Encoding UTF8
  Log "Update started. zip=$ZipPath appDir=$AppDir"
  Start-Sleep -Seconds 2
  Stop-Watchdog
  Stop-Port $port
  Start-Sleep -Seconds 2

  $tmp = Join-Path $updateDir ("extract_" + $timestamp)
  if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  Expand-Archive -LiteralPath $ZipPath -DestinationPath $tmp -Force
  Log "Archive extracted to $tmp"

  $serverExeInZip = Get-ChildItem -LiteralPath $tmp -Filter "server.exe" -File -Recurse |
    Sort-Object { $_.FullName.Length } |
    Select-Object -First 1
  if ($serverExeInZip) {
    $src = $serverExeInZip.DirectoryName
  } else {
    throw "server.exe not found in update archive"
  }
  Log "Using update source: $src"

  $backupRoot = Join-Path $updateDir "backups"
  $backup = Join-Path $backupRoot $timestamp
  New-Item -ItemType Directory -Force -Path $backup | Out-Null
  $skip = @("ai_config.json", "server_secret.key", "_sales_cache", "_api_cache", "_items_store", "_updates", "logs", "generation_history.json")

  foreach ($item in Get-ChildItem -LiteralPath $AppDir -Force) {
    if ($skip -contains $item.Name) { continue }
    try {
      Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $backup $item.Name) -Recurse -Force
    } catch {
      Log "Backup skip $($item.Name): $($_.Exception.Message)"
    }
  }
  Log "Backup created: $backup"

  foreach ($item in Get-ChildItem -LiteralPath $src -Force) {
    if ($skip -contains $item.Name) {
      Log "Skip runtime item from update: $($item.Name)"
      continue
    }
    $dst = Join-Path $AppDir $item.Name
    $copied = $false
    for ($i = 1; $i -le 5; $i++) {
      try {
        Copy-Item -LiteralPath $item.FullName -Destination $dst -Recurse -Force
        $copied = $true
        break
      } catch {
        Log "Copy retry $i for $($item.Name): $($_.Exception.Message)"
        Stop-Watchdog
        Stop-Port $port
        Start-Sleep -Seconds 2
      }
    }
    if (-not $copied) { throw "Failed to update $($item.Name)" }
    Log "Updated $($item.Name)"
  }

  $watchBat = $null
  foreach ($bat in Get-ChildItem -LiteralPath $AppDir -Filter "*.bat" -File -ErrorAction SilentlyContinue) {
    if (Select-String -LiteralPath $bat.FullName -Pattern "--watch","QiHangLocalProjectWatchdog" -Quiet -ErrorAction SilentlyContinue) {
      $watchBat = $bat.FullName
      break
    }
  }
  $exe = Join-Path $AppDir "server.exe"
  if (Test-Path $watchBat) {
    Start-Process -FilePath $watchBat -WorkingDirectory $AppDir
    Log "Started watchdog bat"
  } elseif (Test-Path $exe) {
    Start-Process -FilePath $exe -WorkingDirectory $AppDir
    Log "Started server.exe"
  } else {
    throw "server.exe not found after update"
  }
  Log "Update completed"
} catch {
  Log ("Update failed: " + $_.Exception.Message)
  $exe = Join-Path $AppDir "server.exe"
  if (Test-Path $exe) { Start-Process -FilePath $exe -WorkingDirectory $AppDir }
  exit 1
}
'''
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(ps.strip() + '\n')


def _list_update_backups():
    backup_root = os.path.join(_DATA_BASE, '_updates', 'backups')
    rows = []
    if not os.path.isdir(backup_root):
        return rows
    for name in os.listdir(backup_root):
        path = os.path.join(backup_root, name)
        if not os.path.isdir(path):
            continue
        try:
            stat = os.stat(path)
            rows.append({
                'name': name,
                'path': path,
                'mtime': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            })
        except Exception:
            rows.append({'name': name, 'path': path, 'mtime': ''})
    rows.sort(key=lambda x: x.get('name', ''), reverse=True)
    return rows


def _write_update_rollback_script(script_path):
    ps = r'''
param(
  [string]$BackupDir,
  [string]$AppDir
)
$ErrorActionPreference = "Stop"
$port = 5008
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$updateDir = Join-Path $AppDir "_updates"
$log = Join-Path $updateDir "apply_update.log"
New-Item -ItemType Directory -Force -Path $updateDir | Out-Null
function Log([string]$msg) {
  ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) | Out-File -FilePath $log -Append -Encoding UTF8
}
function Stop-Watchdog() {
  try { schtasks /End /TN "QiHangLocalProjectWatchdog" /F | Out-Null } catch {}
  try { schtasks /Delete /TN "QiHangLocalProjectWatchdog" /F | Out-Null } catch {}
  try {
    $watchers = Get-CimInstance Win32_Process | Where-Object {
      ($_.Name -match "cmd|powershell") -and ($_.CommandLine -like "*--watch*")
    }
    foreach ($p in $watchers) {
      Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
      Log "Stopped watchdog PID $($p.ProcessId)"
    }
  } catch {
    Log "Stop watchdog scan failed: $($_.Exception.Message)"
  }
}
function Stop-Port([int]$p) {
  $conns = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Where-Object { $_.State -eq "Listen" }
  foreach ($c in $conns) {
    try {
      Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
      Log "Stopped PID $($c.OwningProcess) on port $p"
    } catch {
      Log "Failed to stop PID $($c.OwningProcess): $($_.Exception.Message)"
    }
  }
}
try {
  "`n--- rollback $timestamp ---" | Out-File -FilePath $log -Append -Encoding UTF8
  Log "Rollback started. backup=$BackupDir appDir=$AppDir"
  if (-not (Test-Path $BackupDir)) { throw "backup not found: $BackupDir" }
  Start-Sleep -Seconds 2
  Stop-Watchdog
  Stop-Port $port
  Start-Sleep -Seconds 2

  foreach ($item in Get-ChildItem -LiteralPath $BackupDir -Force) {
    $dst = Join-Path $AppDir $item.Name
    $copied = $false
    for ($i = 1; $i -le 5; $i++) {
      try {
        Copy-Item -LiteralPath $item.FullName -Destination $dst -Recurse -Force
        $copied = $true
        break
      } catch {
        Log "Rollback copy retry $i for $($item.Name): $($_.Exception.Message)"
        Stop-Watchdog
        Stop-Port $port
        Start-Sleep -Seconds 2
      }
    }
    if (-not $copied) { throw "Failed to restore $($item.Name)" }
    Log "Restored $($item.Name)"
  }

  $watchBat = $null
  foreach ($bat in Get-ChildItem -LiteralPath $AppDir -Filter "*.bat" -File -ErrorAction SilentlyContinue) {
    if (Select-String -LiteralPath $bat.FullName -Pattern "--watch","QiHangLocalProjectWatchdog" -Quiet -ErrorAction SilentlyContinue) {
      $watchBat = $bat.FullName
      break
    }
  }
  $exe = Join-Path $AppDir "server.exe"
  if (Test-Path $watchBat) {
    Start-Process -FilePath $watchBat -WorkingDirectory $AppDir
    Log "Started watchdog bat"
  } elseif (Test-Path $exe) {
    Start-Process -FilePath $exe -WorkingDirectory $AppDir
    Log "Started server.exe"
  } else {
    throw "server.exe not found after rollback"
  }
  Log "Rollback completed"
} catch {
  Log ("Rollback failed: " + $_.Exception.Message)
  $exe = Join-Path $AppDir "server.exe"
  if (Test-Path $exe) { Start-Process -FilePath $exe -WorkingDirectory $AppDir }
  exit 1
}
'''
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(ps.strip() + '\n')


@app.route('/system-update/upload', methods=['POST'])
def system_update_upload():
    """
    上传新版 zip 到服务器并触发离线覆盖更新。
    zip 可包含顶层目录，内部有“服务端”目录时优先使用该目录。
    """
    try:
        f = request.files.get('file')
        if not f or not f.filename:
            return jsonify({'success': False, 'error': '请选择 zip 更新包'}), 400
        if not f.filename.lower().endswith('.zip'):
            return jsonify({'success': False, 'error': '只支持 zip 更新包'}), 400

        update_dir = os.path.join(_DATA_BASE, '_updates')
        os.makedirs(update_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_path = os.path.join(update_dir, f'pending_update_{ts}.zip')
        f.save(zip_path)

        script_path = os.path.join(update_dir, 'apply_update.ps1')
        _write_update_apply_script(script_path)

        powershell = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                  r'System32\WindowsPowerShell\v1.0\powershell.exe')
        if not os.path.exists(powershell):
            powershell = 'powershell.exe'

        subprocess.Popen(
            [powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', script_path, zip_path, _EXE_DIR],
            cwd=_EXE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        return jsonify({
            'success': True,
            'message': '更新包已上传，服务器将在几秒后自动重启并应用更新',
            'zip_path': zip_path,
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] system_update_upload: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/system-update/backups', methods=['GET'])
def system_update_backups():
    try:
        return jsonify({'success': True, 'backups': _list_update_backups()})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] system_update_backups: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/system-update/rollback', methods=['POST'])
def system_update_rollback():
    try:
        data = request.get_json(force=True) or {}
        name = str(data.get('backup') or '').strip()
        if not name or any(ch in name for ch in ('/', '\\', ':', '..')):
            return jsonify({'success': False, 'error': '请选择有效备份'}), 400
        backup_root = os.path.join(_DATA_BASE, '_updates', 'backups')
        backup_dir = os.path.abspath(os.path.join(backup_root, name))
        if not backup_dir.startswith(os.path.abspath(backup_root)) or not os.path.isdir(backup_dir):
            return jsonify({'success': False, 'error': '备份不存在'}), 404

        update_dir = os.path.join(_DATA_BASE, '_updates')
        os.makedirs(update_dir, exist_ok=True)
        script_path = os.path.join(update_dir, 'rollback_update.ps1')
        _write_update_rollback_script(script_path)
        powershell = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                  r'System32\WindowsPowerShell\v1.0\powershell.exe')
        if not os.path.exists(powershell):
            powershell = 'powershell.exe'
        subprocess.Popen(
            [powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', script_path, backup_dir, _EXE_DIR],
            cwd=_EXE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        return jsonify({'success': True, 'message': '回滚已开始，服务器将在几秒后重启', 'backup': name})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] system_update_rollback: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/system-logs', methods=['GET'])
def system_logs():
    try:
        items, paths = _system_log_files()
        key = request.args.get('file') or 'server'
        if key not in paths:
            key = 'server'
        try:
            max_kb = int(request.args.get('max_kb') or 128)
        except Exception:
            max_kb = 128
        content = _tail_text_file(paths.get(key), max_bytes=max_kb * 1024)
        return jsonify({
            'success': True,
            'files': items,
            'current': key,
            'content': content,
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] system_logs: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/jdy-refresh-auth', methods=['POST'])
def jdy_refresh_auth():
    """
    主动调 push_app_authorize 获取正式版 appKey/appSecret/domain
    body: {idx: 1 或 2}
    成功后自动写入 ai_config.json 并重新初始化 client
    """
    try:
        from ai_identify import _load_config, _CONFIG_FILE
        data = request.get_json(force=True) or {}
        idx  = int(data.get('idx', 1))
        pfx  = '' if idx == 1 else '2'

        cfg_all = _load_config()
        client_id     = cfg_all.get(f'jdy{pfx}_client_id', '')
        client_secret = cfg_all.get(f'jdy{pfx}_client_secret', '')
        outer_id      = cfg_all.get(f'jdy{pfx}_outer_instance_id', '') or cfg_all.get(f'jdy{pfx}_db_id', '')

        if not all([client_id, client_secret, outer_id]):
            return jsonify({'success': False,
                            'error': f'账套{idx}缺少 client_id / client_secret / outer_instance_id'})

        auth = _refresh_jdy_auth_for_idx(idx)
        if not auth.get('success'):
            return jsonify({'success': False, 'error': auth.get('error') or '刷新授权失败'})
        cfg_all = _load_config()
        app_key = cfg_all.get(f'jdy{pfx}_app_key', '')
        app_secret = cfg_all.get(f'jdy{pfx}_app_secret', '')
        domain = cfg_all.get(f'jdy{pfx}_domain', '')
        account_id = cfg_all.get(f'jdy{pfx}_db_id', '')

        return jsonify({'success': True,
                        'app_key': app_key,
                        'app_secret_masked': app_secret[:6] + '…',
                        'domain': domain,
                        'account_id': account_id})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] jdy_refresh_auth: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/jdy-test', methods=['POST'])
def jdy_test():
    """测试 JDY API 连通性（idx=1 或 2 指定账套）"""
    try:
        data = request.get_json(force=True) or {}
        idx  = int(data.get('idx', 1))
        cli  = _ensure_jdy_client2() if idx == 2 else _ensure_jdy_client()
        if not cli:
            return jsonify({'success': False, 'error': f'请先填写账套{idx} JDY API 配置'})
        result = cli.test_connection()
        if result['ok']:
            return jsonify({'success': True, 'message': result['msg']})
        else:
            return jsonify({'success': False, 'error': result['msg']})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/purchase-orders', methods=['GET'])
def purchase_orders_list():
    """
    合并两账套购货入库单列表（并行查询）
    ?search=xxx&begin_date=2026-04-01&end_date=2026-05-15
    每条记录附加 _account 字段
    """
    from concurrent.futures import ThreadPoolExecutor
    try:
        search     = request.args.get('search', '')
        begin_date = request.args.get('begin_date', '')
        end_date   = request.args.get('end_date', '')

        cfg1 = _load_jdy_config();  name1 = cfg1.get('name', '饰品')
        cfg2 = _load_jdy_config2(); name2 = cfg2.get('name', '箱包')
        cli1 = _ensure_jdy_client()
        cli2 = _ensure_jdy_client2()

        if not cli1 and not cli2:
            return jsonify({'success': False, 'error': '请先配置 JDY API'})

        def _fetch(cli, name):
            r     = cli.get_purchase_orders(page=1, page_size=200,
                                            search=search,
                                            begin_date=begin_date, end_date=end_date)
            items = r.get('list') or []
            for item in items:
                item['_account'] = name
                # 整理 entries：只保留前端需要的字段
                item['_product_codes'] = [
                    e.get('productNumber', '') for e in (item.get('entries') or [])
                    if e.get('productNumber')
                ]
            print(f'[{name}] 购货单共 {len(items)} 条')
            return items

        all_items, errors = [], []
        with ThreadPoolExecutor(max_workers=2) as exe:
            tasks = {}
            if cli1: tasks[exe.submit(_fetch, cli1, name1)] = name1
            if cli2: tasks[exe.submit(_fetch, cli2, name2)] = name2
            for fut, name in tasks.items():
                try:
                    all_items.extend(fut.result())
                except Exception as e:
                    errors.append(f'{name}: {e}')

        all_items.sort(key=lambda x: x.get('date') or '', reverse=True)
        return jsonify({'success': True, 'list': all_items,
                        'total': len(all_items), 'errors': errors})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/transfer-orders', methods=['GET'])
def transfer_orders():
    """
    合并两个账套的调拨单列表（两账套并行查询）
    ?page=1&page_size=20&search=xxx&begin_date=2026-04-01&end_date=2026-05-15
    每条记录附加 _account 字段（账套名称）
    """
    from concurrent.futures import ThreadPoolExecutor
    try:
        page       = int(request.args.get('page', 1))
        page_size  = int(request.args.get('page_size', 20))
        search     = request.args.get('search', '')
        begin_date = request.args.get('begin_date', '')
        end_date   = request.args.get('end_date', '')
        source     = request.args.get('source', 'cache')

        cfg1 = _load_jdy_config()
        cfg2 = _load_jdy_config2()
        name1 = cfg1.get('name', '饰品')
        name2 = cfg2.get('name', '箱包')

        if source != 'live':
            all_items = _read_cached_transfer_orders(begin_date, end_date, 'all', search.strip().lower())
            return jsonify({
                'success': True,
                'list': all_items,
                'total': len(all_items),
                'account_names': [name1, name2],
                'errors': [],
                'cache': True,
                'cache_stats': _transfer_cache_stats(),
            })

        cli1 = _ensure_jdy_client()
        cli2 = _ensure_jdy_client2()

        if not cli1 and not cli2:
            return jsonify({'success': False, 'error': '请先配置 JDY API'})

        def _fetch(cli, name):
            r     = cli.get_transfer_orders(page=1, page_size=200, search=search,
                                            begin_date=begin_date, end_date=end_date)
            items = r.get('list') or []
            all_out_locs = set()
            items = [_normalize_transfer_order(item, name) for item in items]
            for item in items:
                all_out_locs.update(item.get('_outLocNames') or [])
            print(f'[{name}] 共 {len(items)} 条，全部调出库位: {all_out_locs}')
            return items

        all_items = []
        errors    = []
        tasks     = {}
        with ThreadPoolExecutor(max_workers=2) as exe:
            if cli1: tasks[exe.submit(_fetch, cli1, name1)] = name1
            if cli2: tasks[exe.submit(_fetch, cli2, name2)] = name2
            for fut, name in tasks.items():
                try:
                    all_items.extend(fut.result())
                except Exception as e:
                    errors.append(f'{name}: {e}')
                    print(f'[ERROR] {name} transfer_orders: {e}')

        # 按日期降序排列
        all_items.sort(key=lambda x: x.get('date') or x.get('createTime') or '', reverse=True)

        # 不做服务端分页，全量返回给前端（前端在过滤后再分页）
        return jsonify({'success': True, 'list': all_items, 'total': len(all_items),
                        'account_names': [name1, name2], 'errors': errors, 'cache': False})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


def _fetch_live_transfer_orders_all(cli, account_name, begin_date='', end_date='', search=''):
    page = 1
    page_size = 20
    rows = []
    total = None
    while True:
        result = cli.get_transfer_orders(
            page=page,
            page_size=page_size,
            search=search,
            begin_date=begin_date,
            end_date=end_date,
        )
        batch = result.get('list') or []
        rows.extend(_normalize_transfer_order(row, account_name) for row in batch)
        total = result.get('total') or total
        if not batch or len(batch) < page_size:
            break
        if total and len(rows) >= int(total):
            break
        page += 1
        if page > 80:
            break
        time.sleep(0.8)
    return rows


def _fetch_live_transfer_orders_by_day(cli, account_name, begin_date='', end_date='', search=''):
    if not begin_date or not end_date:
        return _fetch_live_transfer_orders_all(cli, account_name, begin_date, end_date, search)
    rows = []
    seen_numbers = set()
    for day in _iter_date_strings(begin_date, end_date):
        day_rows = _fetch_live_transfer_orders_all(cli, account_name, day, day, search)
        _log_event('TRANSFER_SYNC', f'{account_name}: {day} fetched {len(day_rows)} rows')
        for row in day_rows:
            key = str(row.get('number') or row.get('billNo') or row.get('id') or '')
            if key and key in seen_numbers:
                continue
            if key:
                seen_numbers.add(key)
            rows.append(row)
    return rows


def _refresh_transfer_cache(begin_date='', end_date='', account='all', search=''):
    started_at = datetime.now()
    seen = 0
    new_count = 0
    changed_count = 0
    unchanged_count = 0
    errors = []
    _log_event('TRANSFER_SYNC', f'start account={account} begin={begin_date} end={end_date} search={search}')
    for cli_fn, name in _sales_sources_for_account(account):
        try:
            cli = _client_for_sync_account(name, cli_fn)
            if not cli:
                errors.append(f'{name}: JDY API not configured')
                _log_event('TRANSFER_SYNC', f'{name}: JDY API not configured')
                continue
            rows = _fetch_live_transfer_orders_by_day(cli, name, begin_date, end_date, search)
            _log_event('TRANSFER_SYNC', f'{name}: fetched {len(rows)} rows from JDY')
            for attempt in range(3):
                try:
                    with _sales_cache_conn() as conn:
                        for row in rows:
                            number = row.get('number') or row.get('billNo') or row.get('id') or ''
                            new_hash = _transfer_order_signature(row)
                            old_hash = _cached_transfer_hash(conn, name, number)
                            if old_hash is None:
                                new_count += 1
                            elif old_hash != new_hash:
                                changed_count += 1
                            else:
                                unchanged_count += 1
                            _cache_upsert_transfer_order(conn, row)
                        conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if 'locked' not in str(e).lower() or attempt >= 2:
                        raise
                    _log_event('TRANSFER_SYNC', f'{name}: database locked, retry {attempt + 1}/3')
                    time.sleep(1.5 * (attempt + 1))
            seen += len(rows)
            msg = f'{name}: cached={len(rows)} window={begin_date}~{end_date}'
            print(f'[TRANSFER SYNC] {msg}')
            _log_event('TRANSFER_SYNC', msg)
        except Exception as e:
            err = f'{name}: {_short_sync_error(e)}'
            errors.append(err)
            print(f'[TRANSFER SYNC ERROR] {err}')
            _log_event('TRANSFER_SYNC_ERROR', err)
    result = {
        'seen': seen,
        'new_count': new_count,
        'changed_count': changed_count,
        'unchanged_count': unchanged_count,
        'errors': errors,
        'begin_date': begin_date,
        'end_date': end_date,
        'duration_seconds': round((datetime.now() - started_at).total_seconds(), 2),
        'stats': _transfer_cache_stats(),
    }
    _log_event('TRANSFER_SYNC', f'finish seen={seen} new={new_count} changed={changed_count} unchanged={unchanged_count} errors={errors}')
    return result


@app.route('/transfer-cache-refresh', methods=['POST'])
def transfer_cache_refresh():
    try:
        body = request.get_json(silent=True) or {}
        begin_date = request.args.get('begin_date', '') or body.get('begin_date', '')
        end_date = request.args.get('end_date', '') or body.get('end_date', '')
        account = request.args.get('account', 'all') or body.get('account', 'all')
        search = request.args.get('search', '') or body.get('search', '')
        result = _refresh_transfer_cache(begin_date, end_date, account, search)
        return jsonify({'success': not bool(result.get('errors')), **result})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] transfer_cache_refresh: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/transfer-order/<order_id>', methods=['GET'])
def transfer_order_detail(order_id):
    """获取调拨单详情，?account=账套名 路由到对应 client"""
    try:
        account = request.args.get('account', '')
        source = request.args.get('source', 'cache')
        if source != 'live':
            cached = _read_cached_transfer_detail(order_id, account)
            if cached:
                return jsonify({'success': True, 'data': cached, 'cache': True})
            return jsonify({'success': False, 'error': '本地没有这张调拨单，请先同步调拨单缓存'}), 404

        cfg1 = _load_jdy_config()
        cfg2 = _load_jdy_config2()
        if account == cfg2.get('name', '箱包'):
            cli = _ensure_jdy_client2()
        else:
            cli = _ensure_jdy_client()
        if not cli:
            return jsonify({'success': False, 'error': '请先配置 JDY API'})
        detail = cli.get_transfer_order_detail(order_id)
        return jsonify({'success': True, 'data': _normalize_transfer_order(detail, account), 'cache': False})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/jdy-auto-fill', methods=['POST'])
def jdy_auto_fill():
    """
    自动补全单个商品基础资料

    请求体:
    {
        "code":            "T.F240T-307",
        "account":         "祺航饰品",          // 可选，指定账套
        "supplier_number": "SUP001",            // 可选，用户选定的供应商编号
        "confirmed":       null 或 {...}        // 用户批量确认后传回
    }

    返回状态:
        needs_supplier_select=True  → 有多条购货单，前端弹窗选供应商
        needs_confirm=True          → AI 与历史冲突，前端加入批量确认队列
        否则                        → 直接写入 Excel
    """
    try:
        data            = request.get_json(force=True) or {}
        code            = (data.get('code') or '').strip()
        account         = (data.get('account') or '').strip()
        supplier_number = (data.get('supplier_number') or '').strip()
        confirmed       = data.get('confirmed')

        if not code:
            return jsonify({'success': False, 'error': '缺少 code 参数'})

        # 按账套选 client
        cfg2 = _load_jdy_config2()
        cli  = _ensure_jdy_client2() if account == cfg2.get('name') else _ensure_jdy_client()
        if not cli:
            # 尝试另一个
            cli = _ensure_jdy_client() or _ensure_jdy_client2()
        if not cli:
            return jsonify({'success': False, 'error': '请先配置 JDY API'})

        # ① 查商品详情
        product = cli.get_product_by_code(code)
        if not product:
            # 未在当前账套找到，换另一个账套试试
            other = _ensure_jdy_client2() if cli is _ensure_jdy_client() else _ensure_jdy_client()
            if other:
                product = other.get_product_by_code(code)
                if product:
                    cli = other
        if not product:
            return jsonify({'success': False, 'error': f'精斗云中未找到商品: {code}'})

        # ② 从商品档案中取字段
        from jdy_api import parse_dimensions
        category   = product.get('categoryName') or ''
        reg_no     = product.get('registrationNo') or ''
        remark     = product.get('remark') or ''
        name       = product.get('productName') or ''
        dimensions = parse_dimensions(reg_no)
        try:
            conversion = float(remark.strip()) if remark.strip() else None
        except ValueError:
            conversion = None

        # ③ 取商品第一张图
        imgs      = product.get('multiImg') or []
        image_url = imgs[0].get('url') if isinstance(imgs, list) and imgs else ''
        if not image_url:
            for sku in (product.get('invSku') or []):
                image_url = sku.get('skuImg') or ''
                if image_url:
                    break

        # ④ 购货入库单 → 供应商编号
        #    若调用方已传入 supplier_number（用户选了），直接用
        #    否则查购货单：单条自动取；多条返回列表让前端选
        origin = ''
        if not supplier_number:
            po_result = cli.get_purchase_orders_by_product(code, page_size=20)
            po_list   = po_result.get('list', [])
            if len(po_list) == 1:
                supplier_number = po_list[0].get('supplierNumber', '')
            elif len(po_list) > 1:
                # 多条购货单，前端需要弹窗让用户选
                return jsonify({
                    'success': True,
                    'needs_supplier_select': True,
                    'code': code,
                    'purchase_orders': [
                        {
                            'number':         r.get('number', ''),
                            'date':           r.get('date', ''),
                            'supplierNumber': r.get('supplierNumber', ''),
                            'supplierName':   r.get('supplierName', ''),
                            'totalQty':       r.get('totalQty', 0),
                        }
                        for r in po_list
                    ],
                })
            # len == 0：无购货单，origin 留空

        address_detail = ''
        if supplier_number:
            supplier = cli.get_supplier_by_number(supplier_number)
            if supplier:
                origin, address_detail = _extract_supplier_address(supplier)

        # ⑤ 调 AI 识别 + 写 Excel
        payload = {
            'code':      code,
            'name':      name,
            'category':  category,
            'image_url': image_url,
        }
        # 记录实际使用的账套名（cli 可能被换成另一账套）
        cfg1 = _load_jdy_config()
        actual_account = cfg2.get('name') if cli is _ensure_jdy_client2() else cfg1.get('name')

        extra_fields = {
            'category':        category,
            'origin':          origin,
            'address_detail':  address_detail,
            'dimensions':      dimensions,
            'conversion':      conversion,
            'supplier_number': supplier_number,
            'account':         actual_account,
        }

        result = identify_from_jdy(payload, extra_fields=extra_fields, confirmed=confirmed)
        return jsonify({'success': True, **result})

    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


@app.route('/purchase-orders-by-product', methods=['GET'])
def purchase_orders_by_product():
    """
    查询某商品的购货入库单列表（供选择供应商用）
    ?code=商品编号&account=账套名
    返回: [{number, date, supplierNumber, supplierName, totalQty, totalAmount}, ...]
    """
    try:
        code    = request.args.get('code', '').strip()
        account = request.args.get('account', '').strip()
        if not code:
            return jsonify({'success': False, 'error': '缺少 code 参数'})

        cfg2 = _load_jdy_config2()
        cli  = _ensure_jdy_client2() if account == cfg2.get('name') else _ensure_jdy_client()
        if not cli:
            return jsonify({'success': False, 'error': '请先配置 JDY API'})

        result = cli.get_purchase_orders_by_product(code, page_size=20)
        # 精简返回字段（只前端需要的）
        items = [
            {
                'number':         r.get('number', ''),
                'date':           r.get('date', ''),
                'supplierNumber': r.get('supplierNumber', ''),
                'supplierName':   r.get('supplierName', ''),
                'totalQty':       r.get('totalQty', 0),
                'totalAmount':    r.get('totalAmount', 0),
                'checkStatus':    r.get('checkStatus', False),
            }
            for r in result.get('list', [])
        ]
        return jsonify({'success': True, 'list': items, 'total': result.get('total', 0)})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] purchase_orders_by_product: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/compare-products-batch', methods=['POST'])
def compare_products_batch():
    """
    批量对比：Excel 已有商品 vs JDY 当前档案，找出需要处理的商品
    请求体: { "codes": ["T.xxx", ...] }   // 前端分批传，每次最多30个
    返回每个商品的状态:
        status: "ok"        — 数据完整，无需处理
                "missing"   — Excel 中不存在
                "incomplete"— 存在但有列为空(E/F/H/I/J/T)
                "error"     — JDY 查询失败
    """
    try:
        from ai_identify import _load_config, _find_excel, _EXCEL_LOCK
        import openpyxl

        data  = request.get_json(force=True) or {}
        codes = [c.strip() for c in (data.get('codes') or []) if c.strip()]
        if not codes:
            return jsonify({'success': False, 'error': '缺少 codes 参数'})

        config     = _load_config()
        excel_path = _find_excel(config)
        if not excel_path:
            return jsonify({'success': False, 'error': '未找到基础资料 Excel'})

        # 读 Excel 中已有数据（B,E,F,H,I,J,T 列）
        excel_data = {}   # code → {e,f,h,i,j,t}
        with _EXCEL_LOCK:
            wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
            ws = wb.active
            try:
                for row in ws.iter_rows(values_only=True):
                    if len(row) < 2 or row[1] is None:
                        continue
                    b = str(row[1]).strip()
                    excel_data[b] = {
                        'e': row[4]  if len(row) > 4  else None,   # E 海关编码
                        'f': row[5]  if len(row) > 5  else None,   # F 商品名称
                        'h': row[7]  if len(row) > 7  else None,   # H 货源地
                        'i': row[8]  if len(row) > 8  else None,   # I 材料
                        'j': row[9]  if len(row) > 9  else None,   # J 用途
                        't': row[19] if len(row) > 19 else None,   # T 供应商编号
                    }
            finally:
                wb.close()

        # 用两个账套批量查 JDY（每账套最多30个）
        jdy_map = {}   # code → product dict
        cli1, cli2 = _ensure_jdy_client(), _ensure_jdy_client2()
        for cli in [c for c in [cli1, cli2] if c]:
            try:
                prods = cli.get_products_by_codes(codes)
                for p in prods:
                    pn = str(p.get('productNumber', '')).strip()
                    if pn and pn not in jdy_map:
                        jdy_map[pn] = p
            except Exception as ex:
                print(f'[WARN] compare_products_batch JDY error: {ex}')

        results = []
        for code in codes:
            ex_row = excel_data.get(code)
            jdy_p  = jdy_map.get(code)

            if ex_row is None:
                results.append({'code': code, 'status': 'missing',
                                 'jdy_name': jdy_p.get('productName', '') if jdy_p else ''})
                continue

            # 检查关键列是否有空
            empty_cols = [col for col, val in [
                ('E海关编码', ex_row['e']), ('F商品名称', ex_row['f']),
                ('H货源地',   ex_row['h']), ('I材料',     ex_row['i']),
                ('J用途',     ex_row['j']), ('T供应商',   ex_row['t']),
            ] if not val]

            if empty_cols:
                results.append({'code': code, 'status': 'incomplete',
                                 'empty_cols': empty_cols,
                                 'jdy_name': jdy_p.get('productName', '') if jdy_p else ''})
            else:
                results.append({'code': code, 'status': 'ok'})

        summary = {s: sum(1 for r in results if r['status'] == s)
                   for s in ('ok', 'missing', 'incomplete', 'error')}
        return jsonify({'success': True, 'results': results, 'summary': summary})

    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] compare_products_batch: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/license-fill', methods=['POST'])
def license_fill():
    """
    档案补全：用商品档案生产许可证 (proLicense) 字段补全 Excel。
    body: {"codes": ["T.xxx", ...], "account": "饰品"}
    或单个: {"code": "T.xxx", "account": "饰品"}
    """
    try:
        data    = request.get_json(force=True) or {}
        codes   = data.get('codes') or ([data['code']] if data.get('code') else [])
        account = (data.get('account') or '').strip()
        if not codes:
            return jsonify({'success': False, 'error': '缺少 codes 参数'})

        results = []
        for code in codes:
            r = _do_license_fill_one(code, account)
            results.append(r or {'code': code, 'status': 'skipped'})

        ok    = [r for r in results if r.get('status') == 'ok']
        other = [r for r in results if r.get('status') != 'ok']
        return jsonify({'success': True, 'results': results,
                        'ok_count': len(ok), 'skip': other})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] license_fill: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/clear-supplier-cache', methods=['POST'])
def clear_supplier_cache():
    """清空指定商品或全部商品的供应商查询缓存。"""
    try:
        body = request.get_json(silent=True) or {}
        codes = [str(c).strip() for c in (body.get('codes') or []) if str(c).strip()]
        cache = _load_code_cache()
        total_before = len(cache)
        if codes:
            cleared = 0
            for code in codes:
                if code in cache:
                    del cache[code]
                    cleared += 1
        else:
            cleared = total_before
            cache = {}
        _save_code_cache(cache)
        _supplier_lookup_cache.clear()
        _city_lookup_cache.clear()
        return jsonify({'success': True, 'cleared': cleared, 'total_before': total_before})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _do_license_preview_one(code, account, existing_codes, cli1, cli2, cfg1_name, cfg2_name):
    """
    查询单个商品 proLicense，返回预览数据（不写 Excel）。
    existing_codes: 预先读取的 Excel 编码 set，避免重复 IO。
    cli1/cli2: 预先初始化好的 JDY 客户端，避免并发重复认证。
    """
    from ai_identify import parse_prolicense
    from jdy_api import parse_dimensions

    cli = cli2 if account == cfg2_name else cli1
    if not cli:
        cli = cli1 or cli2
    if not cli:
        return {'code': code, 'status': 'no_client'}

    product = cli.get_product_by_code(code)
    if not product:
        other = cli2 if cli is cli1 else cli1
        if other:
            product = other.get_product_by_code(code)
            if product:
                cli = other
    if not product:
        return {'code': code, 'status': 'not_found'}

    in_excel    = code in existing_codes
    pro_license = product.get('proLicense') or ''
    name_part, material_part = parse_prolicense(pro_license)
    if not name_part or not material_part:
        return {'code': code, 'status': 'no_license', 'in_excel': in_excel, 'proLicense': pro_license}

    actual = cfg2_name if cli is cli2 else cfg1_name
    remark = product.get('remark') or ''
    try:
        conversion = float(remark.strip()) if remark.strip() else None
    except ValueError:
        conversion = None

    multi_img = product.get('multiImg') or []
    img_url   = multi_img[0].get('url', '') if multi_img else ''

    preview = preview_from_license(code, name_part, material_part, extra_fields={
        'category':   product.get('categoryName') or '',
        'dimensions': parse_dimensions(product.get('registrationNo') or ''),
        'conversion': conversion,
        'account':    actual,
    })
    return {'status': 'ok', 'in_excel': in_excel, 'img_url': img_url, **preview}


@app.route('/license-preview', methods=['POST'])
def license_preview():
    """
    档案补全预览：返回各商品的预览数据，不写入 Excel。
    body: {"codes": [...], "account": "饰品"}
    """
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ai_identify import _find_excel, get_existing_codes

        data    = request.get_json(force=True) or {}
        codes   = data.get('codes') or ([data['code']] if data.get('code') else [])
        account = (data.get('account') or '').strip()
        if not codes:
            return jsonify({'success': False, 'error': '缺少 codes 参数'})

        # 预加载：只读一次 Excel + 只初始化一次 JDY 客户端
        ai_cfg        = _load_ai_config()
        excel_path    = _find_excel(ai_cfg)
        existing_codes = set()
        if excel_path:
            try:
                existing_codes = get_existing_codes(excel_path)
            except Exception:
                pass

        cli1      = _ensure_jdy_client()
        cli2      = _ensure_jdy_client2()
        cfg1_name = _load_jdy_config().get('name', '')
        cfg2_name = _load_jdy_config2().get('name', '')

        # 并发查询（IO 密集，线程池加速）
        results_map = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(_do_license_preview_one,
                            code, account, existing_codes,
                            cli1, cli2, cfg1_name, cfg2_name): code
                for code in codes
            }
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    results_map[code] = fut.result()
                except Exception as ex:
                    results_map[code] = {'code': code, 'status': 'error', 'error': str(ex)}

        # 保持原始顺序
        results = [results_map.get(c, {'code': c, 'status': 'skipped'}) for c in codes]
        ok    = [r for r in results if r.get('status') == 'ok']
        other = [r for r in results if r.get('status') != 'ok']
        return jsonify({'success': True, 'results': results,
                        'ok_count': len(ok), 'skip': other})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] license_preview: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/license-confirm-write', methods=['POST'])
def license_confirm_write():
    """
    档案补全写入：将用户在确认弹窗中确认（可能修改过）的数据写入 Excel。
    body: {"items": [{"code","product_name","material","hs_code","usage","account","category",...}]}
    """
    try:
        data  = request.get_json(force=True) or {}
        items = data.get('items') or []
        if not items:
            return jsonify({'success': False, 'error': '缺少 items 参数'})

        from ai_identify import _load_config, _find_excel
        ai_cfg     = _load_ai_config()
        excel_path = _find_excel(ai_cfg)
        if not excel_path:
            return jsonify({'success': False, 'error': '未找到 Excel 文件，请先在设置中配置'})

        results = []
        for item in items:
            code = item.get('code', '')
            try:
                r = write_confirmed_license(
                    excel_path,
                    code,
                    product_name=item.get('product_name') or '',
                    material=item.get('material') or '',
                    hs_code=item.get('hs_code') or '',
                    usage=item.get('usage') or '',
                    extra_fields={
                        'account':   item.get('account') or '',
                        'category':  item.get('category') or '',
                    },
                )
                results.append({'code': code, 'status': 'ok', 'row': r.get('row')})
            except Exception as ex:
                results.append({'code': code, 'status': 'error', 'error': str(ex)})

        ok   = sum(1 for r in results if r['status'] == 'ok')
        fail = len(results) - ok
        return jsonify({'success': True, 'results': results, 'ok': ok, 'fail': fail})
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] license_confirm_write: {tb}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/jdy-product', methods=['GET'])
def jdy_product():
    """根据编号查询精斗云商品原始数据（调试用），自动尝试两个账套"""
    try:
        code = request.args.get('code', '').strip()
        if not code:
            return jsonify({'success': False, 'error': '缺少 code 参数'})
        # 先查账套1，没找到再查账套2
        cli1 = _ensure_jdy_client()
        product = cli1.get_product_by_code(code) if cli1 else None
        account = _load_jdy_config().get('name', '饰品')
        if not product:
            cli2 = _ensure_jdy_client2()
            if cli2:
                product = cli2.get_product_by_code(code)
                account = _load_jdy_config2().get('name', '箱包')
        return jsonify({'success': True, 'data': product, 'account': account})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── PyWebView 桌面 UI ─────────────────────────────────────────────────────────

@app.route('/generation-history', methods=['GET'])
def generation_history():
    """返回所有调拨单的生成次数历史"""
    return jsonify({'success': True, 'data': _load_gen_history()})


@app.route('/list-items-json', methods=['GET'])
def list_items_json():
    """列出 _items_store/ 目录下所有可用的 items.json 存档，供 Stage 2 下拉选择。"""
    try:
        import glob as _glob
        from excel_gen import _ITEMS_STORE
        files = sorted(
            _glob.glob(os.path.join(_ITEMS_STORE, '*_items.json')),
            key=os.path.getmtime, reverse=True
        )
        result = []
        for fpath in files:
            try:
                with open(fpath, encoding='utf-8') as fp:
                    d = json.load(fp)
                result.append({
                    'filename':     os.path.basename(fpath),
                    'order_no':     d.get('order_no', ''),
                    'invoice_no':   d.get('invoice_no', ''),
                    'output_path':  d.get('output_path', ''),
                    'generated_at': d.get('generated_at', ''),
                    'item_count':   len(d.get('items', [])),
                })
            except Exception:
                pass
        return jsonify({'success': True, 'files': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/process-pdf', methods=['POST'])
def process_pdf():
    """
    阶段二：处理退税联 PDF，生成发票导入模板、更新基础资料、生成凌航发票。
    请求体：{items_filename, pdf_path}
      items_filename: _items_store/ 中的文件名（如 DB20260512002_items.json）
    """
    try:
        body = request.get_json(force=True) or {}
        pdf_path       = (body.get('pdf_path') or '').strip()
        items_filename = (body.get('items_filename') or '').strip()
        export_date    = (body.get('export_date') or '').strip()
        customs_no     = (body.get('customs_no') or '').strip()
        bl_no          = (body.get('bl_no') or '').strip()

        if not items_filename:
            return jsonify({'success': False, 'error': '请选择要处理的报关批次'}), 400
        if pdf_path and not os.path.exists(pdf_path):
            return jsonify({'success': False, 'error': f'PDF 文件不存在: {pdf_path}'}), 400

        # 从 _items_store 读取存档
        from excel_gen import _ITEMS_STORE
        items_json_path = os.path.join(_ITEMS_STORE, items_filename)
        if not os.path.exists(items_json_path):
            return jsonify({'success': False,
                            'error': f'找不到存档文件: {items_filename}'}), 400
        print(f'[PDF] 使用存档文件: {items_json_path}')

        with open(items_json_path, encoding='utf-8') as f:
            saved = json.load(f)

        # 从存档读取 invoice_no / order_no / output_path
        invoice_no  = saved.get('invoice_no', '')
        order_no    = saved.get('order_no', '')
        output_path = saved.get('output_path', '')
        if not output_path or not os.path.isdir(output_path):
            return jsonify({'success': False,
                            'error': f'存档中的输出目录不存在或已移动: {output_path}'}), 400

        # 供应商映射：优先从 items.json 读（Stage 1 已存），避免重复调 API
        supplier_map = saved.get('supplier_map') or {}
        if supplier_map:
            print(f'[PDF] 使用 items.json 缓存的 supplier_map（{len(supplier_map)} 个）')
        else:
            print('[PDF] items.json 无 supplier_map，降级调 API...')
            try:
                supplier_map, _ = _lookup_suppliers(saved.get('items', []))
            except Exception as e:
                print(f'[WARN] 退税联供应商查询失败: {e}')
                supplier_map = {}

        # 从外管局获取本月汇率（与汇率按钮逻辑相同），用于发票 RMB 金额换算
        try:
            inv_rate, inv_rate_date = _fetch_safe_exchange_rate()
            if inv_rate:
                print(f'[RATE] 发票汇率（外管局）{inv_rate_date}: {inv_rate}')
            else:
                inv_rate = float(saved.get('exchange_rate') or 0)
                print(f'[RATE] 外管局无数据，使用存档汇率: {inv_rate}')
        except Exception as e:
            inv_rate = float(saved.get('exchange_rate') or 0)
            print(f'[WARN] 获取发票汇率失败，使用存档汇率 {inv_rate}: {e}')

        # 回写发票汇率到 items.json，供凌航发票等直接读取
        if inv_rate and inv_rate != float(saved.get('invoice_exchange_rate') or 0):
            saved['invoice_exchange_rate'] = inv_rate
            with open(items_json_path, 'w', encoding='utf-8') as _f:
                json.dump(saved, _f, ensure_ascii=False, indent=2)
            print(f'[RATE] 发票汇率 {inv_rate} 已写入 items.json')

        result = process_tax_pdf({
            'invoice_no':            invoice_no,
            'order_no':              order_no,
            'pdf_path':              pdf_path,
            'output_path':           output_path,
            'items_json_path':       items_json_path,
            'supplier_map':          supplier_map,
            'export_date':           export_date,
            'customs_no':            customs_no,
            'bl_no':                 bl_no,
            'invoice_exchange_rate': inv_rate,
        })
        return jsonify({'success': True, **result})

    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] /process-pdf: {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


def _mock_sales_orders(date_str):
    """销货单页面骨架数据。后续替换为精斗云销货单/销售出库单接口。"""
    date_str = date_str or datetime.now().strftime('%Y-%m-%d')
    base_products = [
        {
            'code': 'C.D767C-46',
            'name': '白钻石手表手镯',
            'spec': '10 Ai06',
            'barcode': '240705C120021',
            'qty': 20,
            'unit': 'DZ',
            'amount': 1980,
            'warehouses': [
                {'name': '新大仓库', 'qty': 166},
                {'name': '在途', 'qty': 54},
                {'name': '工厂', 'qty': 120},
            ],
            'purchase': {'ordered': 80, 'received': 26, 'pending': 54},
        },
        {
            'code': 'C.D767C-47',
            'name': '水晶手表手镯',
            'spec': '10 Aj01',
            'barcode': '240705C120022',
            'qty': 10,
            'unit': 'DZ',
            'amount': 990,
            'warehouses': [
                {'name': '新大仓库', 'qty': 74},
                {'name': '在途', 'qty': 36},
                {'name': '工厂', 'qty': 78},
            ],
            'purchase': {'ordered': 60, 'received': 24, 'pending': 36},
        },
        {
            'code': 'C.D767C-44',
            'name': '五排钻石手表 + 手镯',
            'spec': '10 Aj02',
            'barcode': '240705C120019',
            'qty': 20,
            'unit': 'DZ',
            'amount': 1980,
            'warehouses': [
                {'name': '新大仓库', 'qty': 58},
                {'name': '在途', 'qty': 36},
                {'name': '工厂', 'qty': 96},
            ],
            'purchase': {'ordered': 48, 'received': 12, 'pending': 36},
        },
    ]
    return [
        {
            'number': 'XH20260525008',
            'date': date_str,
            'customerName': 'AL KABAYEL DISCOUNT CENTER',
            'account': '祺航箱包',
            'totalQty': 40,
            'totalAmount': 1081.50,
            'checkStatusName': '未审核',
            'entries': [base_products[2], base_products[0]],
        },
        {
            'number': 'XH20260525007',
            'date': date_str,
            'customerName': 'DRAGON GIFT CENTER',
            'account': '祺航饰品',
            'totalQty': 170,
            'totalAmount': 16670.00,
            'checkStatusName': '未审核',
            'entries': base_products,
        },
        {
            'number': 'XH20260525006',
            'date': date_str,
            'customerName': 'CASH',
            'account': '祺航饰品',
            'totalQty': 19,
            'totalAmount': 1199.10,
            'checkStatusName': '未审核',
            'entries': [base_products[1]],
        },
    ]


_SALES_CACHE_DIR = os.path.join(_DATA_BASE, '_sales_cache')
_SALES_CACHE_DB = os.path.join(_SALES_CACHE_DIR, 'sales_cache.sqlite3')
_LOCAL_JSON_CACHE = {}


def _sales_cache_conn():
    os.makedirs(_SALES_CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(_SALES_CACHE_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sales_orders (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            customer_name TEXT,
            total_qty REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            check_status TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sales_details (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS transfer_orders (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            out_location TEXT,
            check_status TEXT,
            remark TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS transfer_details (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sales_product_quantities (
            account TEXT NOT NULL,
            code TEXT NOT NULL,
            stock_new REAL DEFAULT 0,
            stock_transit REAL DEFAULT 0,
            stock_local REAL DEFAULT 0,
            stock_factory REAL DEFAULT 0,
            factory_qty REAL DEFAULT 0,
            updated_at TEXT NOT NULL,
            error TEXT DEFAULT '',
            PRIMARY KEY (account, code)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS accessory_purchase_orders (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            supplier_name TEXT,
            supplier_number TEXT,
            total_qty REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            bill_status TEXT,
            bill_status_name TEXT,
            entries_count INTEGER DEFAULT 0,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS jdy_suppliers (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            name TEXT,
            category_text TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS accessory_products (
            account TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            category TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, code)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT,
            biz_type TEXT,
            resource TEXT,
            bill_no TEXT,
            action TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            processed_at TEXT DEFAULT ''
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS reorder_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT,
            supplier_number TEXT,
            supplier_name TEXT,
            code TEXT NOT NULL,
            name TEXT,
            spec TEXT,
            barcode TEXT,
            unit TEXT,
            image_url TEXT,
            source_type TEXT,
            source_number TEXT,
            source_date TEXT,
            source_customer TEXT,
            source_user TEXT,
            sales_qty_60 REAL DEFAULT 0,
            stock_new REAL DEFAULT 0,
            stock_transit REAL DEFAULT 0,
            stock_local REAL DEFAULT 0,
            stock_factory REAL DEFAULT 0,
            factory_qty REAL DEFAULT 0,
            suggested_qty REAL DEFAULT 0,
            confirmed_qty REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT DEFAULT '',
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS purchase_inbounds (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            date TEXT,
            supplier_number TEXT,
            supplier_name TEXT,
            total_qty REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS purchase_inbound_attachments (
            account TEXT NOT NULL,
            number TEXT NOT NULL,
            attachment_key TEXT NOT NULL,
            name TEXT,
            url TEXT,
            local_path TEXT,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, number, attachment_key)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bill_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT,
            source_type TEXT,
            source_number TEXT,
            source_date TEXT,
            bill_type TEXT,
            attachment_key TEXT,
            name TEXT,
            url TEXT,
            local_path TEXT,
            file_mime TEXT,
            file_size INTEGER DEFAULT 0,
            download_status TEXT DEFAULT '',
            download_error TEXT DEFAULT '',
            data_json TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_sales_orders_date ON sales_orders(date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_sales_orders_customer ON sales_orders(customer_name)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_transfer_orders_date ON transfer_orders(date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_transfer_orders_out_location ON transfer_orders(out_location)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_accessory_po_supplier ON accessory_purchase_orders(supplier_name)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_accessory_po_date ON accessory_purchase_orders(date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_jdy_suppliers_account_name ON jdy_suppliers(account, name)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_accessory_products_category ON accessory_products(category)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_webhook_events_status ON webhook_events(status, id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_webhook_events_bill ON webhook_events(account, bill_no, resource)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_reorder_items_supplier ON reorder_items(status, supplier_name, supplier_number)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_reorder_items_code ON reorder_items(account, code, status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_purchase_inbounds_code_json ON purchase_inbounds(account, number)')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bill_attachments_unique
        ON bill_attachments(account, source_type, source_number, attachment_key)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bill_attachments_source
        ON bill_attachments(account, source_type, source_number)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bill_attachments_date
        ON bill_attachments(source_date)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bill_attachments_status
        ON bill_attachments(download_status)
    ''')
    cols = {row['name'] for row in conn.execute('PRAGMA table_info(sales_product_quantities)').fetchall()}
    if 'stock_local' not in cols:
        conn.execute('ALTER TABLE sales_product_quantities ADD COLUMN stock_local REAL DEFAULT 0')
    if 'stock_factory' not in cols:
        conn.execute('ALTER TABLE sales_product_quantities ADD COLUMN stock_factory REAL DEFAULT 0')
    return conn


def _cache_upsert_sales_order(conn, order):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''
        INSERT OR REPLACE INTO sales_orders
        (account, number, date, customer_name, total_qty, total_amount, check_status, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        order.get('account') or '',
        order.get('number') or '',
        order.get('date') or '',
        order.get('customerName') or '',
        _num(order.get('totalQty')),
        _num(order.get('totalAmount')),
        str(order.get('checkStatusName') or ''),
        json.dumps(order, ensure_ascii=False),
        now,
    ))


def _cache_upsert_sales_detail(conn, order):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''
        INSERT OR REPLACE INTO sales_details
        (account, number, date, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        order.get('account') or '',
        order.get('number') or '',
        order.get('date') or '',
        json.dumps(order, ensure_ascii=False),
        now,
    ))


def _cache_upsert_sales_product_quantity(conn, account, code, stock, factory_qty, error=''):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''
        INSERT OR REPLACE INTO sales_product_quantities
        (account, code, stock_new, stock_transit, stock_local, stock_factory, factory_qty, updated_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        account or '',
        code or '',
        _num((stock or {}).get('新大仓库')),
        _num((stock or {}).get('在途')),
        _num((stock or {}).get('金华/本地')),
        _num((stock or {}).get('工厂订单')),
        _num(factory_qty),
        now,
        str(error or ''),
    ))


def _read_cached_sales_product_quantities(account, codes):
    codes = [str(c).strip() for c in codes if c]
    if not codes:
        return {}
    result = {}
    with _sales_cache_conn() as conn:
        for i in range(0, len(codes), 800):
            batch = codes[i:i + 800]
            placeholders = ','.join('?' for _ in batch)
            rows = conn.execute(f'''
                SELECT code, stock_new, stock_transit, stock_local, stock_factory, factory_qty, updated_at, error
                FROM sales_product_quantities
                WHERE account = ? AND code IN ({placeholders})
            ''', [account, *batch]).fetchall()
            for row in rows:
                result[row['code']] = {
                    'stock': {
                        '新大仓库': _num(row['stock_new']),
                        '在途': _num(row['stock_transit']),
                        '金华/本地': _num(row['stock_local']),
                        '工厂订单': _num(row['stock_factory']),
                    },
                    'factory_qty': _num(row['factory_qty']),
                    'updated_at': row['updated_at'] or '',
                    'error': row['error'] or '',
                }
    return result


def _sales_order_list_projection(order):
    return {
        'number': order.get('number') or '',
        'date': order.get('date') or '',
        'customerName': order.get('customerName') or '',
        'account': order.get('account') or '',
        'totalQty': order.get('totalQty') or 0,
        'totalAmount': order.get('totalAmount') or 0,
        'checkStatusName': order.get('checkStatusName') or '',
    }


def _read_cached_sales_orders(date_str, account='all', search=''):
    with _sales_cache_conn() as conn:
        clauses = ['date = ?']
        params = [date_str]
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        if search:
            clauses.append('(LOWER(number) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(data_json) LIKE ?)')
            kw = f'%{search.lower()}%'
            params.extend([kw, kw, kw])
        rows = conn.execute(f'''
            SELECT data_json FROM sales_orders
            WHERE {' AND '.join(clauses)}
            ORDER BY number DESC
        ''', params).fetchall()
    return [_sales_order_list_projection(json.loads(r['data_json'])) for r in rows]


def _read_cached_sales_detail(order_no, account=''):
    with _sales_cache_conn() as conn:
        if account and account != 'all':
            row = conn.execute('''
                SELECT data_json FROM sales_details
                WHERE account = ? AND number = ?
            ''', (account, order_no)).fetchone()
        else:
            row = conn.execute('''
                SELECT data_json FROM sales_details
                WHERE number = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (order_no,)).fetchone()
    return json.loads(row['data_json']) if row else None


def _sales_readonly_conn():
    if not os.path.exists(_SALES_CACHE_DB):
        return None
    uri = 'file:' + os.path.abspath(_SALES_CACHE_DB).replace('\\', '/') + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _webhook_status_snapshot(limit=20):
    try:
        limit = int(limit or 20)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 100))
    counts = {'pending': 0, 'processing': 0, 'done': 0, 'failed': 0}
    recent = []
    resource_map = {}
    conn = _sales_readonly_conn()
    if conn is None:
        return {
            'counts': counts,
            'recent': recent,
            'resource_summary': [],
            'notes': ['本地缓存库不存在，尚无 Webhook 事件记录'],
        }
    try:
        for row in conn.execute('SELECT status, COUNT(*) AS c FROM webhook_events GROUP BY status'):
            status = str(row['status'] or '')
            counts[status] = int(row['c'] or 0)

        for row in conn.execute('''
            SELECT resource, status, COUNT(*) AS c
            FROM webhook_events
            GROUP BY resource, status
            ORDER BY resource, status
        '''):
            resource = str(row['resource'] or 'unknown')
            status = str(row['status'] or '')
            item = resource_map.setdefault(resource, {
                'resource': resource,
                'total': 0,
                'pending': 0,
                'processing': 0,
                'done': 0,
                'failed': 0,
            })
            count = int(row['c'] or 0)
            item['total'] += count
            item[status] = count

        rows = conn.execute('''
            SELECT id, account, biz_type, resource, bill_no, action, status,
                   attempts, error, created_at, processed_at, payload_json
            FROM webhook_events
            ORDER BY id DESC
            LIMIT ?
        ''', (limit,)).fetchall()
        for row in rows:
            payload = str(row['payload_json'] or '')
            recent.append({
                'id': row['id'],
                'account': row['account'] or '',
                'biz_type': row['biz_type'] or '',
                'resource': row['resource'] or '',
                'bill_no': row['bill_no'] or '',
                'action': row['action'] or '',
                'status': row['status'] or '',
                'attempts': int(row['attempts'] or 0),
                'error': row['error'] or '',
                'created_at': row['created_at'] or '',
                'processed_at': row['processed_at'] or '',
                'payload_preview': payload[:200],
            })
    finally:
        conn.close()

    notes = [
        'inventory/product/supplier/purchase_inbound 当前只轻记录',
        '请在 JDY 后台确认消息订阅地址是否配置为 http://gongdashuai.top:5008/jdy-webhook',
    ]
    if not counts.get('done'):
        notes.insert(0, '当前没有真实业务推送成功记录')
    if counts.get('failed'):
        notes.append('存在失败事件，请查看最近事件里的 error 字段')
    return {
        'counts': counts,
        'recent': recent,
        'resource_summary': sorted(resource_map.values(), key=lambda x: x['resource']),
        'notes': notes,
    }


def _load_local_json_cached(path):
    try:
        if not os.path.exists(path):
            return {}
        st = os.stat(path)
        key = os.path.abspath(path)
        cached = _LOCAL_JSON_CACHE.get(key)
        sig = (st.st_mtime, st.st_size)
        if cached and cached.get('sig') == sig:
            return cached.get('data') or {}
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        _LOCAL_JSON_CACHE[key] = {'sig': sig, 'data': data}
        return data
    except Exception as e:
        print(f'[LOCAL CACHE] JSON 读取失败 {path}: {e}')
        return {}


def _local_product_cache_path(account):
    account = str(account or '')
    if '箱包' in account:
        return os.path.join(_DATA_BASE, '_cache', 'product_account2.json')
    return os.path.join(_DATA_BASE, '_cache', 'product_account1.json')


def _local_supplier_cache_path(account):
    account = str(account or '')
    if '箱包' in account:
        return os.path.join(_DATA_BASE, '_cache', 'supplier_account2.json')
    return os.path.join(_DATA_BASE, '_cache', 'supplier_account1.json')


def _local_product_index(account):
    data = _load_local_json_cached(_local_product_cache_path(account))
    items = data.get('items') if isinstance(data, dict) else data
    result = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        code = str(_first_value(item, ['productNumber', 'number', 'code'], '')).strip()
        if code:
            result[code] = item
    return result


def _local_supplier_index(account):
    data = _load_local_json_cached(_local_supplier_cache_path(account))
    items = data.get('items') if isinstance(data, dict) else data
    by_id = {}
    by_number = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get('id') or '').strip()
        number = str(item.get('number') or '').strip()
        row = {
            'supplierNumber': number,
            'supplierName': str(item.get('name') or '').strip(),
        }
        if sid:
            by_id[sid] = row
        if number:
            by_number[number] = row
    return {'by_id': by_id, 'by_number': by_number}


def _product_image_url_from_cache(product):
    imgs = product.get('multiImg') if isinstance(product, dict) else []
    if isinstance(imgs, list):
        for img in imgs:
            if isinstance(img, dict):
                url = str(_first_value(img, ['url', 'fileUrl', 'downloadUrl', 'src', 'path'], '')).strip()
                if url:
                    return url
            elif img:
                return str(img)
    return str(_first_value(product or {}, ['imageUrl', 'imgUrl', 'pictureUrl', 'picUrl'], '') or '')


def _sales_summary_supplier_from_cache(account, code, product, code_cache, supplier_index):
    supplier_number = str(_first_value(product or {}, ['supplierNumber', 'supplierNo', 'vendorNumber'], '') or '').strip()
    supplier_name = str(_first_value(product or {}, ['supplierName', 'vendorName'], '') or '').strip()
    default_supplier_id = str((product or {}).get('defaultSupplierId') or '').strip()
    if default_supplier_id and not supplier_name:
        row = (supplier_index.get('by_id') or {}).get(default_supplier_id) or {}
        supplier_number = supplier_number or row.get('supplierNumber') or ''
        supplier_name = supplier_name or row.get('supplierName') or ''
    if supplier_number and not supplier_name:
        row = (supplier_index.get('by_number') or {}).get(supplier_number) or {}
        supplier_name = row.get('supplierName') or ''
    if not (supplier_number or supplier_name):
        cached = (code_cache or {}).get(code) or {}
        po = cached.get('po_result') if isinstance(cached, dict) else {}
        if isinstance(po, dict):
            supplier_number = str(po.get('supplierNumber') or '').strip()
            supplier_name = str(po.get('supplierName') or '').strip()
    return supplier_number, supplier_name


def _read_cached_sales_summary_products(start_date, end_date, account='all', q='', limit=500, offset=0, sort='qty_desc'):
    conn = _sales_readonly_conn()
    if not conn:
        return {'items': [], 'total': 0, 'error_count': 0, 'summary': {'qty': 0, 'amount': 0}}
    q = str(q or '').strip().lower()
    buckets = {}
    error_count = 0
    try:
        clauses = ['date >= ?', 'date <= ?']
        params = [start_date, end_date]
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        rows = conn.execute(f'''
            SELECT account, number, date, data_json
            FROM sales_details
            WHERE {' AND '.join(clauses)}
            ORDER BY date DESC, number DESC
        ''', params).fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            order = json.loads(row['data_json'] or '{}')
        except Exception:
            error_count += 1
            continue
        order_account = order.get('account') or row['account'] or ''
        order_no = order.get('number') or row['number'] or ''
        order_date = str(order.get('date') or row['date'] or '')[:10]
        customer = str(order.get('customerName') or '').strip()
        for entry in (order.get('entries') or []):
            if not isinstance(entry, dict):
                continue
            code = str(_first_value(entry, ['code', 'productNumber', 'number'], '') or '').strip()
            if not code:
                continue
            key = (order_account, code)
            item = buckets.setdefault(key, {
                'account': order_account,
                'code': code,
                'name': '',
                'spec': '',
                'barcode': '',
                'unit': '',
                'image_url': '',
                'category': '',
                'supplier_number': '',
                'supplier_name': '',
                'sales_qty': 0,
                'sales_amount': 0,
                'orders': set(),
                'customers': set(),
                'latest_sale_date': '',
            })
            item['name'] = item['name'] or str(_first_value(entry, ['name', 'productName', 'goodsName'], '') or '')
            item['spec'] = item['spec'] or str(_first_value(entry, ['spec', 'specification', 'model'], '') or '')
            item['barcode'] = item['barcode'] or str(_first_value(entry, ['barcode', 'barCode', 'productBarcode'], '') or '')
            item['unit'] = item['unit'] or str(_first_value(entry, ['unit', 'unitName', 'baseUnitName'], '') or '')
            item['image_url'] = item['image_url'] or str(_first_value(entry, ['imageUrl', 'imgUrl', 'pictureUrl'], '') or '')
            item['sales_qty'] += _num(_first_value(entry, ['qty', 'quantity', 'baseQty', 'mainQty'], 0))
            item['sales_amount'] += _num(_first_value(entry, ['amount', 'taxAmount', 'totalAmount'], 0))
            if order_no:
                item['orders'].add(order_no)
            if customer:
                item['customers'].add(customer)
            if order_date and order_date > item['latest_sale_date']:
                item['latest_sale_date'] = order_date

    product_indexes = {}
    supplier_indexes = {}
    code_cache = _load_code_cache()
    for (acct, code), item in list(buckets.items()):
        if acct not in product_indexes:
            product_indexes[acct] = _local_product_index(acct)
        if acct not in supplier_indexes:
            supplier_indexes[acct] = _local_supplier_index(acct)
        product = product_indexes[acct].get(code) or {}
        item['name'] = item['name'] or str(product.get('productName') or '未缓存')
        item['spec'] = item['spec'] or str(product.get('spec') or '')
        item['barcode'] = item['barcode'] or str(product.get('barcode') or '')
        item['unit'] = item['unit'] or str(product.get('unitName') or '')
        item['image_url'] = item['image_url'] or _product_image_url_from_cache(product)
        item['category'] = str(product.get('categoryName') or '') if product else ''
        supplier_number, supplier_name = _sales_summary_supplier_from_cache(
            acct, code, product, code_cache, supplier_indexes[acct]
        )
        item['supplier_number'] = supplier_number
        item['supplier_name'] = supplier_name or ''

    if buckets:
        conn = _sales_readonly_conn()
        if conn:
            try:
                by_account = {}
                for acct, code in buckets:
                    by_account.setdefault(acct, []).append(code)
                for acct, codes in by_account.items():
                    for i in range(0, len(codes), 800):
                        batch = codes[i:i + 800]
                        placeholders = ','.join('?' for _ in batch)
                        qrows = conn.execute(f'''
                            SELECT code, stock_new, stock_transit, stock_local, stock_factory, factory_qty, error
                            FROM sales_product_quantities
                            WHERE account = ? AND code IN ({placeholders})
                        ''', [acct, *batch]).fetchall()
                        for qr in qrows:
                            item = buckets.get((acct, qr['code']))
                            if not item:
                                continue
                            item['stock_new'] = _num(qr['stock_new'])
                            item['stock_transit'] = _num(qr['stock_transit'])
                            item['stock_local'] = _num(qr['stock_local'])
                            item['stock_factory'] = _num(qr['stock_factory'])
                            item['factory_qty'] = _num(qr['factory_qty'])
                            item['quantity_error'] = qr['error'] or ''
            finally:
                conn.close()

    items = []
    for item in buckets.values():
        item['order_count'] = len(item.pop('orders', set()))
        item['customer_count'] = len(item.pop('customers', set()))
        for key in ['stock_new', 'stock_transit', 'stock_local', 'stock_factory', 'factory_qty']:
            item.setdefault(key, 0)
        item.setdefault('quantity_error', '')
        item['warehouses'] = [
            {'name': '新大仓库', 'qty': item['stock_new']},
            {'name': '在途', 'qty': item['stock_transit']},
            {'name': '金华/本地', 'qty': item['stock_local']},
            {'name': '工厂', 'qty': item['factory_qty']},
        ]
        item['purchase'] = {'ordered': item['factory_qty'], 'received': 0, 'pending': item['factory_qty']}
        if q:
            hay = ' '.join(str(item.get(k) or '').lower() for k in [
                'account', 'code', 'name', 'spec', 'barcode', 'category',
                'supplier_number', 'supplier_name'
            ])
            if q not in hay:
                continue
        items.append(item)

    if sort == 'amount_desc':
        items.sort(key=lambda x: (_num(x.get('sales_amount')), _num(x.get('sales_qty'))), reverse=True)
    elif sort == 'latest_desc':
        items.sort(key=lambda x: (x.get('latest_sale_date') or '', _num(x.get('sales_qty'))), reverse=True)
    elif sort == 'code_asc':
        items.sort(key=lambda x: (x.get('account') or '', x.get('code') or ''))
    else:
        items.sort(key=lambda x: (_num(x.get('sales_qty')), _num(x.get('sales_amount'))), reverse=True)

    total = len(items)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 500), 1000))
    page_items = items[offset:offset + limit]
    return {
        'items': page_items,
        'total': total,
        'error_count': error_count,
        'summary': {
            'qty': sum(_num(x.get('sales_qty')) for x in items),
            'amount': sum(_num(x.get('sales_amount')) for x in items),
        },
    }


def _reorder_entry_identity(entry):
    return str(
        entry.get('code') or entry.get('productNumber') or entry.get('number') or ''
    ).strip()


def _reorder_entry_name(entry):
    return str(
        entry.get('name') or entry.get('productName') or entry.get('materialName') or ''
    ).strip()


def _reorder_purchase_entry_matches(entry, code):
    code = str(code or '').strip().lower()
    if not code:
        return False
    values = [
        entry.get('productNumber'), entry.get('code'), entry.get('number'),
        entry.get('materialNumber'), entry.get('productNo'), entry.get('skuNumber'),
    ]
    return any(str(v or '').strip().lower() == code for v in values)


def _reorder_sales_qty_from_cache(account, code, begin_date='', end_date=''):
    clauses = ['account = ?']
    params = [account or '']
    if begin_date:
        clauses.append('date >= ?')
        params.append(begin_date)
    if end_date:
        clauses.append('date <= ?')
        params.append(end_date)
    total = 0
    with _sales_cache_conn() as conn:
        rows = conn.execute(f'''
            SELECT data_json FROM sales_details
            WHERE {' AND '.join(clauses)}
        ''', params).fetchall()
    target = str(code or '').strip()
    for row in rows:
        try:
            order = json.loads(row['data_json'])
        except Exception:
            continue
        for entry in (order.get('entries') or []):
            if _reorder_entry_identity(entry) == target:
                total += _num(_first_value(entry, ['qty', 'quantity', 'baseQty', 'mainQty'], 0))
    return total


def _reorder_recent_sales_qty(account, code, days=60):
    end = datetime.now().date()
    begin = end - timedelta(days=max(1, int(days or 60)) - 1)
    return _reorder_sales_qty_from_cache(
        account or '',
        code,
        begin.strftime('%Y-%m-%d'),
        end.strftime('%Y-%m-%d'),
    )


def _reorder_supplier_from_cached_sources(account, code):
    account = account or ''
    code = str(code or '').strip()
    if not code:
        return {}
    with _sales_cache_conn() as conn:
        rows = conn.execute('''
            SELECT data_json FROM accessory_purchase_orders
            WHERE account = ? AND data_json LIKE ?
            ORDER BY date DESC, number DESC
            LIMIT 1
        ''', (account, f'%{code}%')).fetchall()
    for row in rows:
        try:
            order = json.loads(row['data_json'])
        except Exception:
            continue
        for entry in (order.get('entries') or []):
            if _reorder_purchase_entry_matches(entry, code):
                return {
                    'supplierNumber': order.get('supplierNumber') or '',
                    'supplierName': order.get('supplierName') or '',
                    'source': 'cached_purchase_order',
                    'orderNumber': order.get('number') or '',
                    'orderDate': order.get('date') or '',
                }
    return {}


def _reorder_supplier_from_live_purchase(cli, code):
    if not cli or not code:
        return {}
    try:
        result = cli.get_purchase_orders_by_product(code, page=1, page_size=10)
        rows = result.get('list') or []
        for row in rows:
            return {
                'supplierNumber': _first_value(row, ['supplierNumber', 'supplierNo', 'vendorNumber'], ''),
                'supplierName': _first_value(row, ['supplierName', 'vendorName'], ''),
                'source': 'purchase_inbound',
                'orderNumber': _first_value(row, ['number', 'billNo', 'id'], ''),
                'orderDate': str(_first_value(row, ['date', 'billDate', 'createTime'], ''))[:10],
            }
    except Exception as e:
        print(f'[REORDER] supplier live lookup failed {code}: {_short_sync_error(e)}')
    return {}


def _extract_purchase_attachments(obj):
    found = []
    seen = set()

    def add(item, hint=''):
        if not isinstance(item, dict):
            return
        url = str(_first_value(item, [
            'url', 'fileUrl', 'downloadUrl', 'attachmentUrl', 'path', 'src'
        ], '') or '').strip()
        name = str(_first_value(item, [
            'name', 'fileName', 'attachmentName', 'title', 'originalName'
        ], '') or '').strip()
        fid = str(_first_value(item, [
            'id', 'fileId', 'attachmentId', 'uid', 'key'
        ], '') or '').strip()
        if not (url or name or fid):
            return
        key = fid or url or name
        if key in seen:
            return
        seen.add(key)
        found.append({
            'id': fid,
            'name': name or hint or fid or '附件',
            'url': url,
            'raw': item,
        })

    def walk(value, key_hint=''):
        if isinstance(value, dict):
            lowered_keys = ' '.join(str(k).lower() for k in value.keys())
            if any(x in lowered_keys for x in ('attach', 'file', 'image', 'pic', 'annex')):
                add(value, key_hint)
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, list):
            for item in value:
                if any(x in str(key_hint).lower() for x in ('attach', 'file', 'image', 'pic', 'annex')):
                    add(item if isinstance(item, dict) else {}, key_hint)
                walk(item, key_hint)

    walk(obj or {})
    return found


ATTACHMENT_SWITCH_DATE = '2026-05-28'
ATTACHMENT_SOURCE_RULE = (
    'source_date <= 2026-05-28 使用购货单附件；'
    'source_date > 2026-05-28 使用采购订单附件'
)


def _attachment_config_dates():
    cfg = _load_ai_config()
    begin = str(cfg.get('sales_factory_purchase_begin_date') or '').strip()
    new_logic = str(cfg.get('sales_factory_new_logic_begin_date') or '').strip()
    return begin, new_logic


def _attachment_config_matches_rule(begin, new_logic):
    return begin == ATTACHMENT_SWITCH_DATE and new_logic == ATTACHMENT_SWITCH_DATE


def _attachment_preferred_source(source_date):
    source_date = str(source_date or '').strip()[:10]
    if not source_date:
        return ''
    return 'purchase_inbound' if source_date <= ATTACHMENT_SWITCH_DATE else 'purchase_order'


def _attachment_is_image(att):
    text = ' '.join(str(att.get(k) or '') for k in ('name', 'url', 'local_path', 'file_mime')).lower()
    return any(x in text for x in ('.png', '.jpg', '.jpeg', '.webp', '.gif', 'image/'))


def _attachment_payload(att, source_type, source_number, source_date, source_account, bill_type=''):
    raw = att.get('raw') if isinstance(att.get('raw'), dict) else att
    local_path = str(att.get('local_path') or att.get('localPath') or '').strip()
    url = str(att.get('url') or '').strip()
    name = str(att.get('name') or att.get('fileName') or att.get('attachmentName') or '附件').strip()
    file_mime = str(att.get('file_mime') or att.get('mime') or att.get('mimeType') or '').strip()
    file_size = _num(att.get('file_size') or att.get('size') or 0)
    return {
        'id': str(att.get('id') or att.get('attachment_key') or att.get('key') or url or name),
        'name': name,
        'url': url,
        'local_path': local_path,
        'localPath': local_path,
        'source_type': source_type,
        'sourceType': source_type,
        'source_number': source_number or '',
        'sourceNumber': source_number or '',
        'source_date': source_date or '',
        'sourceDate': source_date or '',
        'source_account': source_account or '',
        'sourceAccount': source_account or '',
        'bill_type': bill_type or source_type,
        'billType': bill_type or source_type,
        'file_mime': file_mime,
        'fileMime': file_mime,
        'file_size': file_size,
        'fileSize': file_size,
        'download_status': str(att.get('download_status') or ('cached' if local_path else 'not_cached')),
        'downloadStatus': str(att.get('download_status') or ('cached' if local_path else 'not_cached')),
        'download_error': str(att.get('download_error') or ''),
        'downloadError': str(att.get('download_error') or ''),
        'cached': bool(local_path),
        'is_image': _attachment_is_image({
            'name': name,
            'url': url,
            'local_path': local_path,
            'file_mime': file_mime,
        }),
        'isImage': _attachment_is_image({
            'name': name,
            'url': url,
            'local_path': local_path,
            'file_mime': file_mime,
        }),
        'raw': raw,
    }


def _read_local_bill_attachments(conn, account, source_type, source_number):
    source_number = str(source_number or '').strip()
    source_type = str(source_type or '').strip()
    if not source_number or not source_type:
        return []
    clauses = ['source_type = ?', 'source_number = ?']
    params = [source_type, source_number]
    if account:
        clauses.append('account = ?')
        params.append(account)
    rows = conn.execute(f'''
        SELECT account, source_type, source_number, source_date, bill_type,
               attachment_key, name, url, local_path, file_mime, file_size,
               download_status, download_error, data_json, updated_at
        FROM bill_attachments
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC, id DESC
    ''', params).fetchall()
    items = []
    for row in rows:
        try:
            data = json.loads(row['data_json'] or '{}')
        except Exception:
            data = {}
        data.update({
            'id': row['attachment_key'] or data.get('id') or '',
            'attachment_key': row['attachment_key'] or '',
            'name': row['name'] or data.get('name') or '',
            'url': row['url'] or data.get('url') or '',
            'local_path': row['local_path'] or '',
            'file_mime': row['file_mime'] or data.get('file_mime') or data.get('mimeType') or '',
            'file_size': row['file_size'] or 0,
            'download_status': row['download_status'] or '',
            'download_error': row['download_error'] or '',
        })
        items.append(_attachment_payload(
            data,
            row['source_type'] or source_type,
            row['source_number'] or source_number,
            row['source_date'] or '',
            row['account'] or '',
            row['bill_type'] or source_type,
        ))
    return items


def _read_local_purchase_inbound_attachments(conn, account, source_number):
    source_number = str(source_number or '').strip()
    if not source_number:
        return []
    clauses = ['number = ?']
    params = [source_number]
    if account:
        clauses.append('account = ?')
        params.append(account)
    rows = conn.execute(f'''
        SELECT account, number, attachment_key, name, url, local_path, data_json, updated_at
        FROM purchase_inbound_attachments
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC, attachment_key
    ''', params).fetchall()
    items = []
    for row in rows:
        try:
            data = json.loads(row['data_json'] or '{}')
        except Exception:
            data = {}
        data.update({
            'attachment_key': row['attachment_key'],
            'name': row['name'] or data.get('name') or '',
            'url': row['url'] or data.get('url') or '',
            'local_path': row['local_path'] or '',
        })
        items.append(_attachment_payload(
            data,
            'purchase_inbound',
            row['number'],
            '',
            row['account'],
            'purchase_inbound',
        ))
    return items


def _read_local_purchase_order_attachments(conn, account, source_number):
    source_number = str(source_number or '').strip()
    if not source_number:
        return []
    clauses = ['number = ?']
    params = [source_number]
    if account:
        clauses.append('account = ?')
        params.append(account)
    rows = conn.execute(f'''
        SELECT account, number, date, data_json
        FROM accessory_purchase_orders
        WHERE {' AND '.join(clauses)}
        ORDER BY date DESC, number DESC
    ''', params).fetchall()
    items = []
    for row in rows:
        try:
            order = json.loads(row['data_json'] or '{}')
        except Exception:
            order = {}
        source_date = str(order.get('date') or row['date'] or '')[:10]
        for att in _extract_purchase_attachments(order):
            items.append(_attachment_payload(
                att,
                'purchase_order',
                row['number'],
                source_date,
                row['account'],
                'purchase_order',
            ))
    return items


def _cache_upsert_purchase_inbound(conn, account, order):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    number = _first_value(order, ['number', 'billNo', 'id'], '')
    conn.execute('''
        INSERT OR REPLACE INTO purchase_inbounds
        (account, number, date, supplier_number, supplier_name, total_qty, total_amount, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        account or '',
        number or '',
        str(_first_value(order, ['date', 'billDate', 'createTime'], ''))[:10],
        _first_value(order, ['supplierNumber', 'supplierNo', 'vendorNumber'], ''),
        _first_value(order, ['supplierName', 'vendorName'], ''),
        _num(_first_value(order, ['totalQty', 'qty', 'totalQuantity'], 0)),
        _num(_first_value(order, ['totalAmount', 'amount'], 0)),
        json.dumps(order, ensure_ascii=False),
        now,
    ))
    for idx, att in enumerate(_extract_purchase_attachments(order)):
        key = att.get('id') or att.get('url') or att.get('name') or str(idx)
        conn.execute('''
            INSERT OR REPLACE INTO purchase_inbound_attachments
            (account, number, attachment_key, name, url, local_path, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT local_path FROM purchase_inbound_attachments WHERE account=? AND number=? AND attachment_key=?), ''), ?, ?)
        ''', (
            account or '', number or '', key, att.get('name') or '', att.get('url') or '',
            account or '', number or '', key,
            json.dumps(att, ensure_ascii=False), now,
        ))


def _read_cached_purchase_history(account, code, limit=20):
    code = str(code or '').strip()
    if not code:
        return []
    clauses = ['data_json LIKE ?']
    params = [f'%{code}%']
    if account and account != 'all':
        clauses.append('account = ?')
        params.append(account)
    with _sales_cache_conn() as conn:
        rows = conn.execute(f'''
            SELECT account, number, data_json FROM purchase_inbounds
            WHERE {' AND '.join(clauses)}
            ORDER BY date DESC, number DESC
            LIMIT ?
        ''', [*params, int(limit or 20)]).fetchall()
    items = []
    for row in rows:
        try:
            order = json.loads(row['data_json'])
        except Exception:
            continue
        matched_entries = [
            e for e in (order.get('entries') or [])
            if _reorder_purchase_entry_matches(e, code)
        ]
        if not matched_entries:
            continue
        qty = sum(_purchase_entry_order_qty(e, prefer_actual=True) or _purchase_entry_order_qty(e) for e in matched_entries)
        items.append({
            'account': row['account'],
            'number': _first_value(order, ['number', 'billNo', 'id'], row['number']),
            'date': str(_first_value(order, ['date', 'billDate', 'createTime'], ''))[:10],
            'supplierNumber': _first_value(order, ['supplierNumber', 'supplierNo', 'vendorNumber'], ''),
            'supplierName': _first_value(order, ['supplierName', 'vendorName'], ''),
            'qty': qty,
            'entries': matched_entries,
            'attachments': _extract_purchase_attachments(order),
        })
    return items


def _reorder_item_payload_from_entry(entry, account='', source=None, cli=None):
    source = source or {}
    account = entry.get('account') or account or ''
    code = _reorder_entry_identity(entry)
    warehouses = entry.get('warehouses') or []
    stock = {str(w.get('name') or ''): _num(w.get('qty')) for w in warehouses if isinstance(w, dict)}
    qmap = _read_cached_sales_product_quantities(account, [code]) if code else {}
    cached_q = qmap.get(code) or {}
    cached_stock = cached_q.get('stock') or {}
    supplier = {
        'supplierNumber': entry.get('supplier_number') or entry.get('supplierNumber') or '',
        'supplierName': entry.get('supplier_name') or entry.get('supplierName') or '',
    }
    if not (supplier.get('supplierNumber') or supplier.get('supplierName')):
        supplier = _reorder_supplier_from_cached_sources(account, code) or {}
    if not supplier and source.get('type') != 'sales_summary':
        supplier = _reorder_supplier_from_live_purchase(cli, code) or {}
    image_url = entry.get('imageUrl') or ''
    if not image_url and cli and code:
        try:
            product = cli.get_product_by_code(code) or {}
            imgs = product.get('multiImg') or []
            if imgs and isinstance(imgs, list):
                image_url = imgs[0].get('url') or ''
        except Exception:
            pass
    sales_qty_60 = _reorder_recent_sales_qty(account, code)
    factory_qty = _num(cached_q.get('factory_qty') or (entry.get('purchase') or {}).get('pending') or 0)
    return {
        'account': account or '',
        'supplierNumber': supplier.get('supplierNumber') or '',
        'supplierName': supplier.get('supplierName') or '未识别供应商',
        'code': code,
        'name': _reorder_entry_name(entry),
        'spec': entry.get('spec') or '',
        'barcode': entry.get('barcode') or '',
        'unit': entry.get('unit') or '',
        'imageUrl': image_url,
        'sourceType': source.get('type') or 'sales',
        'sourceNumber': source.get('number') or '',
        'sourceDate': source.get('date') or '',
        'sourceCustomer': source.get('customer') or '',
        'sourceUser': source.get('user') or '',
        'salesQty60': sales_qty_60,
        'stockNew': _num(cached_stock.get('新大仓库') or cached_stock.get('鏂板ぇ浠撳簱') or stock.get('新大仓库') or stock.get('鏂板ぇ浠撳簱')),
        'stockTransit': _num(cached_stock.get('在途') or cached_stock.get('鍦ㄩ€?') or stock.get('在途') or stock.get('鍦ㄩ€?')),
        'stockLocal': _num(cached_stock.get('金华/本地') or cached_stock.get('閲戝崕/鏈湴') or stock.get('金华/本地') or stock.get('閲戝崕/鏈湴')),
        'stockFactory': _num(cached_stock.get('工厂订单') or cached_stock.get('宸ュ巶璁㈠崟') or stock.get('工厂') or stock.get('宸ュ巶')),
        'factoryQty': factory_qty,
        'suggestedQty': 0,
        'confirmedQty': 0,
        'raw': entry,
        'supplierSource': supplier,
    }


def _cache_insert_reorder_item(conn, item):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    existing = conn.execute('''
        SELECT id FROM reorder_items
        WHERE status = 'pending'
          AND COALESCE(account, '') = ?
          AND code = ?
          AND COALESCE(source_number, '') = ?
        LIMIT 1
    ''', (item.get('account') or '', item.get('code') or '', item.get('sourceNumber') or '')).fetchone()
    data_json = json.dumps(item, ensure_ascii=False)
    if existing:
        conn.execute('''
            UPDATE reorder_items
               SET supplier_number=?, supplier_name=?, name=?, spec=?, barcode=?, unit=?,
                   image_url=?, sales_qty_60=?, stock_new=?, stock_transit=?, stock_local=?,
                   stock_factory=?, factory_qty=?, data_json=?, updated_at=?
             WHERE id=?
        ''', (
            item.get('supplierNumber') or '', item.get('supplierName') or '',
            item.get('name') or '', item.get('spec') or '', item.get('barcode') or '',
            item.get('unit') or '', item.get('imageUrl') or '',
            _num(item.get('salesQty60')), _num(item.get('stockNew')),
            _num(item.get('stockTransit')), _num(item.get('stockLocal')),
            _num(item.get('stockFactory')), _num(item.get('factoryQty')),
            data_json, now, existing['id'],
        ))
        return existing['id'], False
    cur = conn.execute('''
        INSERT INTO reorder_items
        (account, supplier_number, supplier_name, code, name, spec, barcode, unit,
         image_url, source_type, source_number, source_date, source_customer, source_user,
         sales_qty_60, stock_new, stock_transit, stock_local, stock_factory, factory_qty,
         suggested_qty, confirmed_qty, status, note, data_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?, ?)
    ''', (
        item.get('account') or '', item.get('supplierNumber') or '', item.get('supplierName') or '',
        item.get('code') or '', item.get('name') or '', item.get('spec') or '',
        item.get('barcode') or '', item.get('unit') or '', item.get('imageUrl') or '',
        item.get('sourceType') or '', item.get('sourceNumber') or '', item.get('sourceDate') or '',
        item.get('sourceCustomer') or '', item.get('sourceUser') or '',
        _num(item.get('salesQty60')), _num(item.get('stockNew')), _num(item.get('stockTransit')),
        _num(item.get('stockLocal')), _num(item.get('stockFactory')), _num(item.get('factoryQty')),
        _num(item.get('suggestedQty')), _num(item.get('confirmedQty')),
        data_json, now, now,
    ))
    return cur.lastrowid, True


def _reorder_row_to_item(row):
    try:
        data = json.loads(row['data_json'] or '{}')
    except Exception:
        data = {}
    data.update({
        'id': row['id'],
        'account': row['account'] or '',
        'supplierNumber': row['supplier_number'] or '',
        'supplier_number': row['supplier_number'] or '',
        'supplierName': row['supplier_name'] or '',
        'supplier_name': row['supplier_name'] or '',
        'code': row['code'] or '',
        'name': row['name'] or '',
        'spec': row['spec'] or '',
        'barcode': row['barcode'] or '',
        'unit': row['unit'] or '',
        'imageUrl': row['image_url'] or '',
        'image_url': row['image_url'] or '',
        'sourceType': row['source_type'] or '',
        'source_type': row['source_type'] or '',
        'sourceNumber': row['source_number'] or '',
        'source_number': row['source_number'] or '',
        'sourceDate': row['source_date'] or '',
        'source_date': row['source_date'] or '',
        'sourceCustomer': row['source_customer'] or '',
        'source_customer': row['source_customer'] or '',
        'salesQty60': _num(row['sales_qty_60']),
        'sales_qty_60': _num(row['sales_qty_60']),
        'stockNew': _num(row['stock_new']),
        'stock_new': _num(row['stock_new']),
        'stockTransit': _num(row['stock_transit']),
        'stock_transit': _num(row['stock_transit']),
        'stockLocal': _num(row['stock_local']),
        'stock_local': _num(row['stock_local']),
        'stockFactory': _num(row['stock_factory']),
        'stock_factory': _num(row['stock_factory']),
        'factoryQty': _num(row['factory_qty']),
        'factory_qty': _num(row['factory_qty']),
        'suggestedQty': _num(row['suggested_qty']),
        'suggested_qty': _num(row['suggested_qty']),
        'confirmedQty': _num(row['confirmed_qty']),
        'confirmed_qty': _num(row['confirmed_qty']),
        'status': row['status'] or '',
        'note': row['note'] or '',
        'createdAt': row['created_at'] or '',
        'created_at': row['created_at'] or '',
        'updatedAt': row['updated_at'] or '',
        'updated_at': row['updated_at'] or '',
        'warehouses': [
            {'name': '新大仓库', 'qty': _num(row['stock_new'])},
            {'name': '在途', 'qty': _num(row['stock_transit'])},
            {'name': '金华/本地', 'qty': _num(row['stock_local'])},
            {'name': '工厂', 'qty': _num(row['factory_qty'])},
        ],
    })
    return data


def _reorder_stock_from_cache(conn, account, code):
    if not code:
        return {}
    params = [code]
    where = 'code = ?'
    if account:
        where += ' AND account = ?'
        params.append(account)
    row = conn.execute(f'''
        SELECT stock_new, stock_transit, stock_local, stock_factory, factory_qty
        FROM sales_product_quantities
        WHERE {where}
        ORDER BY updated_at DESC
        LIMIT 1
    ''', params).fetchone()
    if not row:
        return {}
    return {
        'stockNew': _num(row['stock_new']),
        'stockTransit': _num(row['stock_transit']),
        'stockLocal': _num(row['stock_local']),
        'stockFactory': _num(row['stock_factory']),
        'factoryQty': _num(row['factory_qty']),
    }


def _reorder_entries_from_order(order):
    if not isinstance(order, dict):
        return []
    entries = (
        order.get('entries') or order.get('details') or order.get('items') or
        order.get('lines') or order.get('materialEntries') or order.get('billEntry') or
        order.get('billEntries') or order.get('entry') or []
    )
    if isinstance(entries, dict):
        entries = [entries]
    return entries if isinstance(entries, list) else []


def _reorder_recent_style_from_entry(entry, account, supplier, source_order, conn):
    code = _reorder_entry_identity(entry)
    if not code:
        return None
    stock = _reorder_stock_from_cache(conn, account, code)
    return {
        'account': account or '',
        'supplierNumber': supplier.get('supplierNumber') or '',
        'supplierName': supplier.get('supplierName') or '未识别供应商',
        'code': code,
        'name': _reorder_entry_name(entry),
        'spec': _first_value(entry, ['spec', 'model', 'specification', 'materialModel', 'materialSpec'], ''),
        'barcode': _first_value(entry, ['barcode', 'barCode', 'number', 'materialNumber'], ''),
        'unit': _first_value(entry, ['unit', 'unitName', 'baseUnitName'], ''),
        'imageUrl': _first_value(entry, ['imageUrl', 'image', 'picUrl', 'picture'], ''),
        'sourceNumber': source_order.get('number') or source_order.get('billNo') or '',
        'sourceDate': str(source_order.get('date') or source_order.get('billDate') or '')[:10],
        'salesQty60': _reorder_recent_sales_qty(account, code),
        'warehouses': [
            {'name': '新大仓库', 'qty': stock.get('stockNew', 0)},
            {'name': '在途', 'qty': stock.get('stockTransit', 0)},
            {'name': '金华/本地', 'qty': stock.get('stockLocal', 0)},
            {'name': '工厂', 'qty': stock.get('factoryQty', stock.get('stockFactory', 0))},
        ],
    }


def _normalize_transfer_order(row, account):
    entries = row.get('entries') or row.get('details') or row.get('items') or row.get('lines') or []
    out_locs = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        loc = (
            entry.get('outLocationName')
            or entry.get('outWarehouseName')
            or entry.get('outStockName')
            or ''
        )
        if loc and loc not in out_locs:
            out_locs.append(loc)
    number = _first_value(row, ['number', 'billNo', 'id'], '')
    date_str = str(_first_value(row, ['date', 'billDate', 'createTime'], ''))[:10]
    norm = dict(row)
    norm.update({
        'number': number,
        'date': date_str,
        '_account': account,
        '_outLocNames': out_locs,
        '_outLocName': out_locs[0] if out_locs else '',
    })
    return norm


_TRANSFER_ORDER_HASH_VERSION = 'transfer-order-v1'


def _transfer_order_signature(order):
    keep = dict(order or {})
    keep.pop('cacheHash', None)
    keep.pop('cacheHashVersion', None)
    raw = json.dumps(keep, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _cached_transfer_hash(conn, account, number):
    row = conn.execute('''
        SELECT data_json FROM transfer_details
        WHERE account = ? AND number = ?
    ''', (account or '', number or '')).fetchone()
    if not row:
        return None
    try:
        cached = json.loads(row['data_json'])
        if cached.get('cacheHashVersion') == _TRANSFER_ORDER_HASH_VERSION and cached.get('cacheHash'):
            return cached.get('cacheHash')
        return _transfer_order_signature(cached)
    except Exception:
        return ''


def _cache_upsert_transfer_order(conn, order):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    order = dict(order or {})
    order['cacheHash'] = _transfer_order_signature(order)
    order['cacheHashVersion'] = _TRANSFER_ORDER_HASH_VERSION
    account = order.get('_account') or order.get('account') or ''
    number = order.get('number') or order.get('billNo') or order.get('id') or ''
    date_str = str(order.get('date') or order.get('billDate') or order.get('createTime') or '')[:10]
    conn.execute('''
        INSERT OR REPLACE INTO transfer_orders
        (account, number, date, out_location, check_status, remark, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        account,
        number,
        date_str,
        order.get('_outLocName') or '',
        str(order.get('checkStatusName') or order.get('statusName') or order.get('checkStatus') or ''),
        order.get('remark') or '',
        json.dumps(order, ensure_ascii=False),
        now,
    ))
    conn.execute('''
        INSERT OR REPLACE INTO transfer_details
        (account, number, date, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        account,
        number,
        date_str,
        json.dumps(order, ensure_ascii=False),
        now,
    ))


def _cache_one_sales_order_from_jdy(cli, account, number, include_quantities=True):
    if not cli or not number:
        return False
    detail = cli.get_sales_order_detail(number)
    order = _normalize_sales_order(detail, account)
    if not (order.get('number') or order.get('entries')):
        return False
    orders = _enrich_sales_orders_batch(
        cli, [order], include_quantities=include_quantities, account=account
    )
    order = orders[0] if orders else order
    order['cacheHash'] = _sales_order_signature(order)
    order['cacheHashVersion'] = _SALES_ORDER_HASH_VERSION
    with _sales_cache_conn() as conn:
        _cache_upsert_sales_order(conn, order)
        _cache_upsert_sales_detail(conn, order)
        conn.commit()
    return True


def _cache_one_transfer_order_from_jdy(cli, account, number):
    if not cli or not number:
        return False
    detail = cli.get_transfer_order_detail(number)
    order = _normalize_transfer_order(detail, account)
    if not (order.get('number') or order.get('billNo') or order.get('id')):
        return False
    with _sales_cache_conn() as conn:
        _cache_upsert_transfer_order(conn, order)
        conn.commit()
    return True


def _cache_one_accessory_purchase_order_from_jdy(cli, account, number):
    if not cli or not number:
        return False
    result = cli.get_purchase_order_requests(
        page=1, page_size=1, search=number, bill_status=0
    )
    rows = result.get('list') or []
    if not rows:
        return False
    with _sales_cache_conn() as conn:
        for row in rows:
            row_no = str(_first_value(row, ['number', 'billNo', 'id'], '')).strip()
            if row_no and row_no != str(number).strip():
                continue
            supplier_no = str(_first_value(row, ['supplierNumber', 'supplierNo', 'vendorNumber'], '')).strip()
            supplier = _read_cached_jdy_supplier(conn, account, supplier_no) if supplier_no else {}
            if supplier_no and supplier is None:
                try:
                    supplier = cli.get_supplier_by_number(supplier_no, status=2) or {}
                    if supplier:
                        _cache_upsert_jdy_supplier(conn, account, supplier)
                except Exception:
                    supplier = {}
            order = _normalize_accessory_purchase_order(row, account, supplier or {})
            if not _supplier_matches_accessory(supplier or {}, row):
                continue
            order = _enrich_accessory_purchase_order(order)
            _cache_upsert_accessory_purchase_order(conn, order)
            conn.commit()
            return True
    return False


def _flatten_webhook_items(value):
    if value is None:
        return []
    if isinstance(value, list):
        rows = []
        for item in value:
            rows.extend(_flatten_webhook_items(item))
        return rows
    if isinstance(value, dict):
        rows = [value]
        for key in ('data', 'items', 'list', 'detail', 'details'):
            if isinstance(value.get(key), (list, dict)):
                rows.extend(_flatten_webhook_items(value.get(key)))
        return rows
    return []


def _extract_webhook_bill_numbers(payload):
    payload = payload or {}
    numbers = []
    keys = [
        'number', 'billNo', 'billNumber', 'bill_no', 'billId', 'id',
        'sourceBillNo', 'srcBillNo', 'orderNo', 'orderNumber',
    ]
    list_keys = ['numbers', 'billNos', 'billNoList', 'ids', 'idList']
    for item in _flatten_webhook_items(payload):
        for key in keys:
            val = item.get(key)
            if val not in (None, ''):
                numbers.append(str(val).strip())
        for key in list_keys:
            val = item.get(key)
            if isinstance(val, list):
                numbers.extend(str(x).strip() for x in val if x not in (None, ''))
            elif val not in (None, ''):
                numbers.extend(x.strip() for x in str(val).replace(';', ',').split(',') if x.strip())
    return [x for x in dict.fromkeys(numbers) if x]


def _jdy_account_from_payload(payload):
    payload = payload or {}
    cfg1 = _load_jdy_config()
    cfg2 = _load_jdy_config2()
    app_key = str(payload.get('appKey') or payload.get('app_key') or '')
    account_id = str(payload.get('accountId') or payload.get('dbId') or payload.get('dbid') or payload.get('outerInstanceId') or '')
    if app_key and app_key == str(cfg2.get('app_key') or ''):
        return cfg2.get('name', '箱包')
    if app_key and app_key == str(cfg1.get('app_key') or ''):
        return cfg1.get('name', '饰品')
    if account_id and account_id == str(cfg2.get('db_id') or ''):
        return cfg2.get('name', '箱包')
    if account_id and account_id == str(cfg1.get('db_id') or ''):
        return cfg1.get('name', '饰品')
    return ''


def _jdy_resource_from_event(biz_type, payload):
    text = ' '.join([
        str(biz_type or ''),
        str(payload.get('bizType') or ''),
        str(payload.get('resource') or ''),
        str(payload.get('type') or ''),
        str(payload.get('billType') or ''),
        str(payload.get('formId') or ''),
        str(payload.get('entity') or ''),
    ]).lower()
    mapping = [
        ('transfer', ('transfer', 'inv_transfer', '调拨')),
        ('sales', ('sal_bill_outbound', 'delivery', 'saleout', 'sales', '销货单')),
        ('sales_order', ('sal_bill_order', 'saleorder', '销货订单')),
        ('accessory_purchase', ('pur_bill_order', 'purchaseorder', 'purchase_order', '购货订单')),
        ('purchase_inbound', ('pur_bill_inbound', 'purchase/list', '购货单')),
        ('inventory', ('inv_inventory', 'inventory', '库存')),
        ('product', ('bd_material', 'product', '商品')),
        ('supplier', ('bd_supplier', 'supplier', '供应商')),
    ]
    for resource, needles in mapping:
        if any(needle.lower() in text for needle in needles):
            return resource
    return str(payload.get('resource') or biz_type or 'unknown')


def _enqueue_jdy_webhook_events(biz_type, payload):
    payload = payload or {}
    account = _jdy_account_from_payload(payload)
    resource = _jdy_resource_from_event(biz_type, payload)
    action = str(payload.get('operation') or payload.get('action') or payload.get('event') or biz_type or '')
    numbers = _extract_webhook_bill_numbers(payload) or ['']
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserted = 0
    with _sales_cache_conn() as conn:
        for number in numbers:
            conn.execute('''
                INSERT INTO webhook_events
                (account, biz_type, resource, bill_no, action, status, attempts, payload_json, error, created_at, processed_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, '', ?, '')
            ''', (
                account, str(biz_type or ''), resource, number, action,
                json.dumps(payload, ensure_ascii=False), now,
            ))
            inserted += 1
        conn.commit()
    _webhook_state.update({'last_event_at': now, 'pending': _webhook_pending_count(), 'message': f'queued {inserted} event(s)'})
    _start_webhook_worker()
    return inserted


def _webhook_pending_count():
    try:
        with _sales_cache_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM webhook_events WHERE status = 'pending'").fetchone()
        return int(row['c'] or 0)
    except Exception:
        return 0


def _claim_webhook_event():
    with _webhook_queue_lock:
        with _sales_cache_conn() as conn:
            row = conn.execute('''
                SELECT * FROM webhook_events
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT 1
            ''').fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE webhook_events SET status = 'processing', attempts = attempts + 1 WHERE id = ?",
                (row['id'],),
            )
            conn.commit()
            return dict(row)


def _finish_webhook_event(event_id, success=True, error=''):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = 'done' if success else 'failed'
    with _sales_cache_conn() as conn:
        conn.execute(
            "UPDATE webhook_events SET status = ?, error = ?, processed_at = ? WHERE id = ?",
            (status, str(error or '')[:500], now, event_id),
        )
        conn.commit()
    _webhook_state.update({
        'last_processed_at': now,
        'last_error': '' if success else str(error or '')[:300],
        'pending': _webhook_pending_count(),
    })


def _process_webhook_event(event):
    account = event.get('account') or ''
    resource = (event.get('resource') or '').lower()
    number = (event.get('bill_no') or '').strip()
    if not account:
        payload = json.loads(event.get('payload_json') or '{}')
        account = _jdy_account_from_payload(payload)
    cli, account_name = _sales_client_for_account(account)
    if not cli:
        raise RuntimeError(f'account not configured: {account or "-"}')
    if not number:
        _log_event('JDY_WEBHOOK', f'event has no bill number, queued only: resource={resource} account={account_name}')
        return False
    if resource in ('sales', 'sales_order') or 'sal_bill' in resource or 'delivery' in resource:
        return _cache_one_sales_order_from_jdy(cli, account_name, number)
    if resource == 'transfer' or 'transfer' in resource:
        return _cache_one_transfer_order_from_jdy(cli, account_name, number)
    if resource in ('accessory_purchase', 'purchase_order') or 'pur_bill_order' in resource or 'purchaseorder' in resource:
        return _cache_one_accessory_purchase_order_from_jdy(cli, account_name, number)
    if resource in ('inventory', 'product', 'supplier', 'purchase_inbound'):
        _log_event('JDY_WEBHOOK', f'light event recorded: resource={resource} account={account_name} number={number}')
        return True
    _log_event('JDY_WEBHOOK', f'unknown event recorded: resource={resource} account={account_name} number={number}')
    return True


def _webhook_worker_loop():
    _webhook_state['running'] = True
    while not _webhook_worker_stop.is_set():
        event = _claim_webhook_event()
        if not event:
            _webhook_state.update({'running': False, 'pending': _webhook_pending_count()})
            _webhook_worker_stop.wait(5)
            _webhook_state['running'] = True
            continue
        try:
            _webhook_state.update({
                'message': f"processing {event.get('resource')} {event.get('bill_no')}",
                'pending': _webhook_pending_count(),
            })
            ok = _process_webhook_event(event)
            _finish_webhook_event(event['id'], True, '' if ok else 'recorded only')
            _webhook_state['processed'] = int(_webhook_state.get('processed') or 0) + 1
            time.sleep(1.5)
        except Exception as e:
            _finish_webhook_event(event['id'], False, _short_sync_error(e))
            _log_event('JDY_WEBHOOK_ERROR', f"{event.get('resource')} {event.get('bill_no')}: {_short_sync_error(e)}")
            time.sleep(3)


def _start_webhook_worker():
    global _webhook_worker_thread
    if _webhook_worker_thread and _webhook_worker_thread.is_alive():
        return
    _webhook_worker_thread = threading.Thread(target=_webhook_worker_loop, daemon=True, name='jdy-webhook-worker')
    _webhook_worker_thread.start()


def _read_cached_transfer_orders(begin_date='', end_date='', account='all', search=''):
    with _sales_cache_conn() as conn:
        clauses = []
        params = []
        if begin_date:
            clauses.append('date >= ?')
            params.append(begin_date)
        if end_date:
            clauses.append('date <= ?')
            params.append(end_date)
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        if search:
            clauses.append('(LOWER(number) LIKE ? OR LOWER(data_json) LIKE ?)')
            kw = f'%{search.lower()}%'
            params.extend([kw, kw])
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = conn.execute(f'''
            SELECT data_json FROM transfer_orders
            {where}
            ORDER BY date DESC, number DESC
        ''', params).fetchall()
    return [json.loads(r['data_json']) for r in rows]


def _read_cached_transfer_detail(order_no, account=''):
    with _sales_cache_conn() as conn:
        if account and account != 'all':
            row = conn.execute('''
                SELECT data_json FROM transfer_details
                WHERE account = ? AND number = ?
            ''', (account, order_no)).fetchone()
        else:
            row = conn.execute('''
                SELECT data_json FROM transfer_details
                WHERE number = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (order_no,)).fetchone()
    return json.loads(row['data_json']) if row else None


def _transfer_cache_stats():
    with _sales_cache_conn() as conn:
        row = conn.execute('''
            SELECT COUNT(*) AS orders_count,
                   MIN(date) AS min_date,
                   MAX(date) AS max_date,
                   MAX(updated_at) AS updated_at
            FROM transfer_orders
        ''').fetchone()
        details = conn.execute('SELECT COUNT(*) AS details_count FROM transfer_details').fetchone()
    return {
        'orders_count': row['orders_count'] or 0,
        'details_count': details['details_count'] or 0,
        'min_date': row['min_date'] or '',
        'max_date': row['max_date'] or '',
        'updated_at': row['updated_at'] or '',
        'db_path': _SALES_CACHE_DB,
    }


def _sales_cache_stats():
    with _sales_cache_conn() as conn:
        row = conn.execute('''
            SELECT COUNT(*) AS orders_count,
                   MIN(date) AS min_date,
                   MAX(date) AS max_date,
                   MAX(updated_at) AS updated_at
            FROM sales_orders
        ''').fetchone()
        details = conn.execute('SELECT COUNT(*) AS details_count FROM sales_details').fetchone()
        qty = conn.execute('''
            SELECT COUNT(*) AS product_quantity_count,
                   MAX(updated_at) AS quantity_updated_at
            FROM sales_product_quantities
        ''').fetchone()
    return {
        'orders_count': row['orders_count'] or 0,
        'details_count': details['details_count'] or 0,
        'product_quantity_count': qty['product_quantity_count'] or 0,
        'min_date': row['min_date'] or '',
        'max_date': row['max_date'] or '',
        'updated_at': row['updated_at'] or '',
        'quantity_updated_at': qty['quantity_updated_at'] or '',
        'db_path': _SALES_CACHE_DB,
    }


_sales_sync_lock = threading.Lock()
_sales_sync_stop = threading.Event()
_sales_sync_thread = None
_sales_sync_state = {
    'running': False,
    'last_started_at': '',
    'last_finished_at': '',
    'last_success': False,
    'last_error': '',
    'last_reason': '',
    'next_run_at': '',
    'phase': '',
    'account': '',
    'current': 0,
    'total': 0,
    'message': '',
    'summary': {},
}
_accessory_sync_lock = threading.Lock()
_accessory_sync_state = {
    'running': False,
    'last_started_at': '',
    'last_finished_at': '',
    'last_success': False,
    'last_error': '',
    'last_reason': '',
    'next_run_at': '',
    'summary': {},
}
_accessory_product_sync_lock = threading.Lock()
_accessory_product_sync_state = {
    'running': False,
    'last_started_at': '',
    'last_finished_at': '',
    'last_success': False,
    'last_error': '',
    'last_reason': '',
    'summary': {},
}

ACCESSORY_MATERIAL_CATEGORIES = ['绳子', '卡片', '泡壳', '塑料盒子', '塑料桶', '贴纸', '吸塑卡片', '纸盒', '纸箱']
_SALES_ORDER_HASH_VERSION = 'sales-order-v2'

_webhook_queue_lock = threading.Lock()
_webhook_worker_thread = None
_webhook_worker_stop = threading.Event()
_webhook_state = {
    'running': False,
    'last_event_at': '',
    'last_processed_at': '',
    'last_error': '',
    'pending': 0,
    'processed': 0,
    'message': '',
}
_daily_compare_state = {
    'last_date': '',
    'last_started_at': '',
    'last_finished_at': '',
    'last_error': '',
}


def _sales_sync_config():
    cfg = _load_ai_config()
    def _first_text(*names):
        for name in names:
            value = str(cfg.get(name) or '').strip()
            if value:
                return value
        return ''

    def _int(name, default, min_value=1, max_value=365):
        try:
            value = int(cfg.get(name, default))
        except Exception:
            value = default
        return max(min_value, min(max_value, value))
    expire_action = str(cfg.get('sales_cache_expire_action') or 'delete').strip().lower()
    if expire_action not in ('delete', 'archive'):
        expire_action = 'delete'
    manual_days = _int(
        'sales_cache_manual_days',
        _int('sales_incremental_lookback_days', 3, 1, 60),
        1,
        60,
    )
    factory_begin = _first_text('sales_factory_purchase_begin_date', 'factory_purchase_begin_date')
    factory_new_begin = _first_text(
        'sales_factory_new_logic_begin_date',
        'factory_new_logic_begin_date',
        'sales_factory_purchase_begin_date',
        'factory_purchase_begin_date',
    )
    accessory_begin = _first_text(
        'accessory_purchase_begin_date',
        'sales_accessory_purchase_begin_date',
        'sales_factory_purchase_begin_date',
        'factory_purchase_begin_date',
    )
    accessory_terms = _first_text('accessory_supplier_terms', 'accessory_supplier_keywords')
    return {
        'enabled': bool(cfg.get('sales_auto_sync_enabled', False)),
        'interval_minutes': _int('sales_auto_sync_interval_minutes', 5, 1, 1440),
        'keep_days': _int('sales_cache_keep_days', 30, 1, 365),
        'lookback_days': manual_days,
        'manual_days': manual_days,
        'daily_compare_time': _first_text('sales_cache_daily_time', 'jdy_daily_compare_time'),
        'expire_action': expire_action,
        'archive_path': str(cfg.get('sales_cache_archive_path') or '').strip(),
        'factory_purchase_begin_date': factory_begin,
        'factory_new_logic_begin_date': factory_new_begin,
        'factory_disabled_suppliers': _first_text('sales_factory_disabled_suppliers', 'factory_disabled_suppliers'),
        'accessory_enabled': bool(cfg.get('accessory_auto_sync_enabled', False)),
        'accessory_interval_minutes': _int('accessory_auto_sync_interval_minutes', 5, 1, 1440),
        'accessory_purchase_begin_date': accessory_begin,
        'accessory_supplier_terms': accessory_terms or '辅料供应商\n辅料',
        'accessory_supplier_cache_hours': _int('accessory_supplier_cache_hours', 24, 1, 720),
    }


def _split_sales_factory_disabled_suppliers(text):
    if isinstance(text, list):
        raw = []
        for item in text:
            raw.extend(str(item or '').splitlines())
    else:
        raw = str(text or '').replace('，', ',').replace('；', ';').splitlines()
    terms = []
    for line in raw:
        for part in str(line).replace(';', ',').split(','):
            val = part.strip()
            if val:
                terms.append(val)
    return [x for x in dict.fromkeys(terms)]


def _sales_factory_purchase_filter_config():
    cfg = _sales_sync_config()
    return {
        'begin_date': cfg.get('factory_purchase_begin_date') or '',
        'new_logic_begin_date': cfg.get('factory_purchase_begin_date') or '',
        'disabled_suppliers': _split_sales_factory_disabled_suppliers(
            cfg.get('factory_disabled_suppliers') or ''
        ),
    }


def _purchase_order_allowed_for_factory(order, filter_conf=None):
    filter_conf = filter_conf or _sales_factory_purchase_filter_config()
    begin_date = filter_conf.get('begin_date') or ''
    if begin_date:
        order_date = str(_first_value(order, ['date', 'billDate', 'createTime', 'checkTime'], ''))[:10]
        if order_date and order_date < begin_date:
            return False
    supplier_no = str(_first_value(order, ['supplierNumber', 'supplierNo', 'vendorNumber'], '')).strip()
    supplier_name = str(_first_value(order, ['supplierName', 'vendorName', 'contactName'], '')).strip()
    supplier_no_l = supplier_no.lower()
    supplier_name_l = supplier_name.lower()
    for term in filter_conf.get('disabled_suppliers') or []:
        t = str(term or '').strip().lower()
        if not t:
            continue
        if t == supplier_no_l or t == supplier_name_l or (supplier_name_l and t in supplier_name_l):
            return False
    return True


def _fetch_factory_purchase_orders(cli, code, filter_conf=None, page_size=100, max_pages=10):
    filter_conf = filter_conf or _sales_factory_purchase_filter_config()
    rows = []
    for page in range(1, max_pages + 1):
        po = cli.get_purchase_order_requests(
            page=page,
            page_size=page_size,
            bill_status=0,
            begin_date=filter_conf.get('begin_date') or '',
            product_number=code,
        )
        batch = po.get('list') or []
        if not batch:
            break
        rows.extend([row for row in batch if _purchase_order_allowed_for_factory(row, filter_conf)])
        total = po.get('total') or 0
        if len(batch) < page_size:
            break
        if total and page * page_size >= int(total):
            break
    return rows


def _fetch_checked_factory_purchase_orders(cli, code, filter_conf=None, page_size=100, max_pages=10):
    filter_conf = filter_conf or _sales_factory_purchase_filter_config()
    rows = []
    for page in range(1, max_pages + 1):
        po = cli.get_purchase_order_requests(
            page=page,
            page_size=page_size,
            check_status=1,
            begin_date=filter_conf.get('begin_date') or '',
            product_number=code,
        )
        batch = po.get('list') or []
        if not batch:
            break
        rows.extend([row for row in batch if _purchase_order_allowed_for_factory(row, filter_conf)])
        total = po.get('total') or 0
        if len(batch) < page_size:
            break
        if total and page * page_size >= int(total):
            break
    return rows


def _sales_order_signature(order):
    keep = {
        'number': order.get('number') or '',
        'date': order.get('date') or '',
        'customerName': order.get('customerName') or '',
        'account': order.get('account') or '',
        'totalQty': _num(order.get('totalQty')),
        'totalAmount': _num(order.get('totalAmount')),
        'checkStatusName': order.get('checkStatusName') or '',
        'entries': [
            {
                'code': e.get('code') or '',
                'name': e.get('name') or '',
                'qty': _num(e.get('qty')),
                'unit': e.get('unit') or '',
                'amount': _num(e.get('amount')),
            }
            for e in (order.get('entries') or [])
        ],
    }
    raw = json.dumps(keep, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _cached_sales_hash(conn, account, number):
    row = conn.execute('''
        SELECT data_json FROM sales_details
        WHERE account = ? AND number = ?
    ''', (account or '', number or '')).fetchone()
    if not row:
        return ''
    try:
        cached = json.loads(row['data_json'])
        if cached.get('cacheHashVersion') == _SALES_ORDER_HASH_VERSION and cached.get('cacheHash'):
            return cached.get('cacheHash')
        return _sales_order_signature(cached)
    except Exception:
        return ''


def _sales_sources_for_account(account='all'):
    cfg1 = _load_jdy_config();  name1 = cfg1.get('name', '饰品')
    cfg2 = _load_jdy_config2(); name2 = cfg2.get('name', '箱包')
    sources = []
    if account in ('', 'all', name1):
        sources.append((_ensure_jdy_client, name1))
    if account in ('', 'all', name2):
        sources.append((_ensure_jdy_client2, name2))
    return sources


def _collect_sales_quantity_refs(rows):
    refs = set()
    for row in rows:
        try:
            order = json.loads(row['data_json'])
        except Exception:
            continue
        account = order.get('account') or row['account'] or ''
        for entry in (order.get('entries') or []):
            code = str(entry.get('code') or '').strip()
            if code:
                refs.add((account, code))
    return refs


def _cleanup_orphan_sales_quantities(conn):
    rows = conn.execute('SELECT account, data_json FROM sales_details').fetchall()
    refs = _collect_sales_quantity_refs(rows)
    all_rows = conn.execute('SELECT account, code FROM sales_product_quantities').fetchall()
    removed = 0
    for row in all_rows:
        key = (row['account'] or '', row['code'] or '')
        if key not in refs:
            conn.execute('DELETE FROM sales_product_quantities WHERE account = ? AND code = ?', key)
            removed += 1
    return removed


def _cleanup_sales_cache_range(begin_date, end_date, action='delete', archive_path=''):
    action = (action or 'delete').lower()
    archived_file = ''
    with _sales_cache_conn() as conn:
        order_rows = conn.execute('''
            SELECT account, number, date, data_json, updated_at
            FROM sales_orders
            WHERE date >= ? AND date <= ?
            ORDER BY date, number
        ''', (begin_date, end_date)).fetchall()
        detail_rows = conn.execute('''
            SELECT account, number, date, data_json, updated_at
            FROM sales_details
            WHERE date >= ? AND date <= ?
            ORDER BY date, number
        ''', (begin_date, end_date)).fetchall()
        refs = _collect_sales_quantity_refs(detail_rows)
        qty_rows = []
        for account, code in refs:
            row = conn.execute('''
                SELECT account, code, stock_new, stock_transit, stock_local, factory_qty, updated_at, error
                FROM sales_product_quantities
                WHERE account = ? AND code = ?
            ''', (account, code)).fetchone()
            if row:
                qty_rows.append(dict(row))

        if action == 'archive' and archive_path:
            os.makedirs(archive_path, exist_ok=True)
            safe_begin = begin_date.replace('-', '')
            safe_end = end_date.replace('-', '')
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            archived_file = os.path.join(
                archive_path,
                f'sales_cache_archive_{safe_begin}_{safe_end}_{stamp}.json'
            )
            payload = {
                'begin_date': begin_date,
                'end_date': end_date,
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'sales_orders': [dict(r) for r in order_rows],
                'sales_details': [dict(r) for r in detail_rows],
                'sales_product_quantities': qty_rows,
            }
            with open(archived_file, 'w', encoding='utf-8') as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)

        conn.execute('DELETE FROM sales_orders WHERE date >= ? AND date <= ?', (begin_date, end_date))
        conn.execute('DELETE FROM sales_details WHERE date >= ? AND date <= ?', (begin_date, end_date))
        orphan_quantities = _cleanup_orphan_sales_quantities(conn)
        conn.commit()

    return {
        'deleted_orders': len(order_rows),
        'deleted_details': len(detail_rows),
        'deleted_orphan_quantities': orphan_quantities,
        'archived_file': archived_file,
        'stats': _sales_cache_stats(),
    }


def _cleanup_sales_cache_retention(conf):
    today = datetime.now().date()
    keep_start = today - timedelta(days=int(conf.get('keep_days') or 30))
    end_date = (keep_start - timedelta(days=1)).strftime('%Y-%m-%d')
    begin_date = '1900-01-01'
    with _sales_cache_conn() as conn:
        row = conn.execute('SELECT MIN(date) AS min_date FROM sales_orders').fetchone()
    if not row or not row['min_date'] or row['min_date'] > end_date:
        return {'deleted_orders': 0, 'deleted_details': 0, 'deleted_orphan_quantities': 0, 'archived_file': ''}
    return _cleanup_sales_cache_range(
        begin_date,
        end_date,
        conf.get('expire_action') or 'delete',
        conf.get('archive_path') or '',
    )


def _run_sales_incremental_sync(reason='manual', account='all'):
    if not _sales_sync_lock.acquire(blocking=False):
        return {'running': True, 'message': 'sales sync already running'}
    started_at = datetime.now()
    _sales_sync_state.update({
        'running': True,
        'last_started_at': started_at.strftime('%Y-%m-%d %H:%M:%S'),
        'last_finished_at': '',
        'last_success': False,
        'last_error': '',
        'last_reason': reason,
        'phase': 'starting',
        'account': '',
        'current': 0,
        'total': 0,
        'message': '准备同步',
    })
    try:
        conf = _sales_sync_config()
        today = datetime.now().date()
        keep_start = today - timedelta(days=int(conf.get('keep_days') or 30))
        lookback_start = today - timedelta(days=int(conf.get('lookback_days') or 3) - 1)
        begin_date = max(keep_start, lookback_start).strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')

        total_seen = 0
        total_changed = 0
        total_cached = 0
        errors = []
        _log_event('SALES_SYNC', f'start reason={reason} account={account} begin={begin_date} end={end_date}')
        with _sales_cache_conn() as conn:
            sources = _sales_sources_for_account(account)
            _sales_sync_state.update({'total': len(sources), 'current': 0})
            for idx, (cli_fn, name) in enumerate(sources, 1):
                _sales_sync_state.update({
                    'phase': 'fetching',
                    'account': name,
                    'current': idx,
                    'message': f'{name}: 读取 {begin_date}~{end_date}',
                })
                try:
                    cli = _client_for_sync_account(name, cli_fn)
                    if not cli:
                        err = f'{name}: JDY API not configured'
                        errors.append(err)
                        _log_event('SALES_SYNC_ERROR', err)
                        continue
                    raw_rows = _fetch_live_sales_orders_by_day(cli, begin_date, end_date)
                    orders = [_normalize_sales_order(row, name) for row in raw_rows]
                    total_seen += len(orders)
                    changed_orders = []
                    for order in orders:
                        order_hash = _sales_order_signature(order)
                        order['cacheHash'] = order_hash
                        if _cached_sales_hash(conn, name, order.get('number')) != order_hash:
                            changed_orders.append(order)
                    if changed_orders:
                        _sales_sync_state.update({'phase': 'enriching', 'message': f'{name}: 补全 {len(changed_orders)} 张改动单'})
                        changed_orders = _enrich_sales_orders_batch(
                            cli, changed_orders, include_quantities=True, account=name
                        )
                        for order in changed_orders:
                            order['cacheHash'] = _sales_order_signature(order)
                            order['cacheHashVersion'] = _SALES_ORDER_HASH_VERSION
                            _cache_upsert_sales_order(conn, order)
                            _cache_upsert_sales_detail(conn, order)
                        conn.commit()
                    total_changed += len(changed_orders)
                    total_cached += len(orders)
                    print(f'[SALES SYNC] {name}: seen={len(orders)} changed={len(changed_orders)} window={begin_date}~{end_date}')
                    _log_event('SALES_SYNC', f'{name}: seen={len(orders)} changed={len(changed_orders)} window={begin_date}~{end_date}')
                except Exception as e:
                    err = f'{name}: {e}'
                    errors.append(err)
                    print(f'[SALES SYNC ERROR] {err}')
                    _log_event('SALES_SYNC_ERROR', err)
                time.sleep(1.0)

        _sales_sync_state.update({'phase': 'cleanup', 'message': '清理过期本地缓存'})
        cleanup = _cleanup_sales_cache_retention(conf)
        finished_at = datetime.now()
        summary = {
            'begin_date': begin_date,
            'end_date': end_date,
            'seen': total_seen,
            'changed': total_changed,
            'cached': total_cached,
            'errors': errors,
            'cleanup': cleanup,
            'stats': _sales_cache_stats(),
            'duration_seconds': round((finished_at - started_at).total_seconds(), 2),
        }
        _sales_sync_state.update({
            'running': False,
            'last_finished_at': finished_at.strftime('%Y-%m-%d %H:%M:%S'),
            'last_success': not bool(errors),
            'last_error': '; '.join(errors[:5]),
            'phase': 'done',
            'message': f'完成：扫描 {total_seen} 张，更新 {total_changed} 张',
            'summary': summary,
        })
        _log_event('SALES_SYNC', f'finish seen={total_seen} changed={total_changed} cached={total_cached} errors={errors}')
        return summary
    except Exception as e:
        finished_at = datetime.now()
        _sales_sync_state.update({
            'running': False,
            'last_finished_at': finished_at.strftime('%Y-%m-%d %H:%M:%S'),
            'last_success': False,
            'last_error': str(e),
            'phase': 'error',
            'message': str(e),
            'summary': {},
        })
        raise
    finally:
        _sales_sync_lock.release()


def _daily_compare_due(conf, now):
    daily_time = str(conf.get('daily_compare_time') or '').strip()
    if not daily_time:
        return False
    try:
        hh, mm = daily_time.split(':')[:2]
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        return False
    today_key = now.strftime('%Y-%m-%d')
    if _daily_compare_state.get('last_date') == today_key:
        return False
    return now >= target


def _sales_sync_worker():
    last_sales_run_at = None
    last_accessory_run_at = None
    _start_webhook_worker()
    while not _sales_sync_stop.is_set():
        try:
            conf = _sales_sync_config()
            sales_interval_seconds = int(conf.get('interval_minutes') or 5) * 60
            accessory_interval_seconds = int(conf.get('accessory_interval_minutes') or 5) * 60
            now = datetime.now()
            # High-frequency polling is disabled. JDY changes should arrive via webhook;
            # manual sync and daily compare remain available for reconciliation.
            sales_due = False
            accessory_due = False
            if sales_due:
                last_sales_run_at = now
                _run_sales_incremental_sync(reason='auto')
            if _daily_compare_due(conf, now):
                _daily_compare_state.update({
                    'last_date': now.strftime('%Y-%m-%d'),
                    'last_started_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'last_error': '',
                })
                try:
                    _run_sales_incremental_sync(reason='daily-compare')
                    _run_accessory_incremental_sync(reason='daily-compare')
                    begin = (now.date() - timedelta(days=int(conf.get('lookback_days') or 3) - 1)).strftime('%Y-%m-%d')
                    end = now.strftime('%Y-%m-%d')
                    _refresh_transfer_cache(begin, end, 'all', '')
                    _daily_compare_state['last_finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                except Exception as e:
                    _daily_compare_state.update({
                        'last_error': _short_sync_error(e),
                        'last_finished_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    })
            if accessory_due:
                last_accessory_run_at = now
                _run_accessory_incremental_sync(reason='auto')
                _run_accessory_product_sync(reason='auto')
            if False and conf.get('enabled') and last_sales_run_at:
                next_at = last_sales_run_at + timedelta(seconds=sales_interval_seconds)
                _sales_sync_state['next_run_at'] = next_at.strftime('%Y-%m-%d %H:%M:%S')
            else:
                _sales_sync_state['next_run_at'] = ''
            if False and conf.get('accessory_enabled') and last_accessory_run_at:
                next_at = last_accessory_run_at + timedelta(seconds=accessory_interval_seconds)
                _accessory_sync_state['next_run_at'] = next_at.strftime('%Y-%m-%d %H:%M:%S')
            else:
                _accessory_sync_state['next_run_at'] = ''
        except Exception as e:
            _sales_sync_state.update({
                'running': False,
                'last_success': False,
                'last_error': str(e),
                'last_finished_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            print(f'[SALES SYNC WORKER ERROR] {e}')
        _sales_sync_stop.wait(10)


def _start_sales_sync_worker():
    global _sales_sync_thread
    if _sales_sync_thread and _sales_sync_thread.is_alive():
        return
    _sales_sync_thread = threading.Thread(target=_sales_sync_worker, daemon=True, name='sales-sync-worker')
    _sales_sync_thread.start()


def _num(v, default=0):
    try:
        if v in (None, ''):
            return default
        return float(str(v).replace(',', ''))
    except Exception:
        return default


def _first_value(obj, keys, default=''):
    for key in keys:
        val = obj.get(key)
        if val not in (None, ''):
            return val
    return default


def _accessory_supplier_terms():
    cfg = _load_ai_config()
    raw = str(cfg.get('accessory_supplier_terms') or '辅料供应商\n辅料\n辅材供应商\n辅材').strip()
    terms = []
    for line in raw.replace(';', '\n').replace(',', '\n').splitlines():
        val = line.strip()
        if val:
            terms.append(val)
            if '辅料' in val:
                terms.append(val.replace('辅料', '辅材'))
            if '辅材' in val:
                terms.append(val.replace('辅材', '辅料'))
    return [x for x in dict.fromkeys(terms)]


def _supplier_matches_accessory(supplier, order=None):
    supplier = supplier or {}
    order = order or {}
    terms = [t.lower() for t in _accessory_supplier_terms() if t]
    if not terms:
        return False
    fields = [
        'supplierCategoryName', 'supplierCategory', 'categoryName', 'category',
        'typeName', 'supplierTypeName', 'className', 'groupName',
        'newRecTypeName', 'recTypeName', 'remark', 'description',
    ]
    values = []
    for src in (supplier, order):
        for key in fields:
            val = src.get(key)
            if val not in (None, ''):
                values.append(str(val))
    # Fallback for accounts where supplier category is stored in the visible supplier name.
    values.extend([
        str(_first_value(order, ['supplierName', 'vendorName'], '')),
        str(_first_value(supplier, ['name', 'supplierName'], '')),
    ])
    haystack = '\n'.join(values).lower()
    return any(term in haystack for term in terms)


def _supplier_category_text(supplier):
    supplier = supplier or {}
    fields = [
        'supplierCategoryName', 'supplierCategory', 'categoryName', 'category',
        'typeName', 'supplierTypeName', 'className', 'groupName',
        'newRecTypeName', 'recTypeName', 'remark', 'description',
    ]
    values = []
    for key in fields:
        val = supplier.get(key)
        if val not in (None, ''):
            values.append(str(val))
    return '\n'.join(values)


def _cache_upsert_jdy_supplier(conn, account, supplier):
    number = str(_first_value(supplier, ['number', 'supplierNumber', 'supplierNo'], '')).strip()
    if not number:
        return
    name = str(_first_value(supplier, ['name', 'supplierName'], '')).strip()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''
        INSERT OR REPLACE INTO jdy_suppliers
        (account, number, name, category_text, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        account or '',
        number,
        name,
        _supplier_category_text(supplier),
        json.dumps(supplier, ensure_ascii=False),
        now,
    ))


def _read_cached_jdy_supplier(conn, account, number):
    number = str(number or '').strip()
    if not number:
        return None
    row = conn.execute('''
        SELECT data_json FROM jdy_suppliers
        WHERE account = ? AND number = ?
    ''', (account or '', number)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row['data_json'])
    except Exception:
        return None


def _supplier_cache_stats(account=''):
    with _sales_cache_conn() as conn:
        clauses = []
        params = []
        if account:
            clauses.append('account = ?')
            params.append(account)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        row = conn.execute(f'''
            SELECT COUNT(*) AS suppliers_count, MAX(updated_at) AS updated_at
            FROM jdy_suppliers
            {where}
        ''', params).fetchone()
    return {
        'suppliers_count': row['suppliers_count'] or 0,
        'updated_at': row['updated_at'] or '',
    }


def _supplier_cache_fresh(account, max_age_hours):
    stats = _supplier_cache_stats(account)
    if not stats.get('suppliers_count') or not stats.get('updated_at'):
        return False
    try:
        ts = datetime.strptime(stats['updated_at'], '%Y-%m-%d %H:%M:%S')
    except Exception:
        return False
    return datetime.now() - ts < timedelta(hours=int(max_age_hours or 24))


def _refresh_supplier_cache_for_account(cli, account_name, max_age_hours=24):
    if _supplier_cache_fresh(account_name, max_age_hours):
        return {'skipped': True, **_supplier_cache_stats(account_name)}
    page = 1
    page_size = 20
    total_seen = 0
    with _sales_cache_conn() as conn:
        while True:
            result = cli.get_suppliers(page=page, page_size=page_size, status=2)
            rows = result.get('list') or []
            for supplier in rows:
                _cache_upsert_jdy_supplier(conn, account_name, supplier)
            conn.commit()
            total_seen += len(rows)
            total = result.get('total') or total_seen
            if not rows or len(rows) < page_size:
                break
            if total and total_seen >= int(total):
                break
            page += 1
            if page > 200:
                break
    stats = _supplier_cache_stats(account_name)
    stats.update({'skipped': False, 'seen': total_seen})
    return stats


def _normalize_accessory_purchase_entry(entry):
    return {
        'code': _first_value(entry, ['productNumber', 'productCode', 'number', 'code'], ''),
        'name': _first_value(entry, ['productName', 'name', 'goodsName'], ''),
        'spec': _first_value(entry, ['specification', 'spec', 'model', 'skuName'], ''),
        'barcode': _first_value(entry, ['barCode', 'barcode', 'productBarcode'], ''),
        'qty': _num(_first_value(entry, ['qty', 'mainQty', 'quantity', 'baseQty'], 0)),
        'unit': _first_value(entry, ['unitName', 'unit', 'baseUnitName'], ''),
        'price': _num(_first_value(entry, ['price', 'taxPrice'], 0)),
        'amount': _num(_first_value(entry, ['amount', 'taxAmount', 'totalAmount'], 0)),
        'location': _first_value(entry, ['location', 'locationName', 'warehouseName'], ''),
    }


def _normalize_accessory_purchase_order(row, account, supplier=None):
    entries = [
        _normalize_accessory_purchase_entry(e)
        for e in (row.get('entries') or row.get('items') or row.get('details') or [])
        if isinstance(e, dict)
    ]
    supplier = supplier or {}
    return {
        'number': _first_value(row, ['number', 'billNo', 'id'], ''),
        'date': str(_first_value(row, ['date', 'billDate', 'createTime'], ''))[:10],
        'supplierName': _first_value(row, ['supplierName', 'vendorName'], '') or supplier.get('name') or '',
        'supplierNumber': _first_value(row, ['supplierNumber', 'supplierNo', 'vendorNumber'], '') or supplier.get('number') or '',
        'account': account,
        'totalQty': _num(_first_value(row, ['totalQty', 'qty', 'quantity', 'totalQuantity'], 0)),
        'totalAmount': _num(_first_value(row, ['totalAmount', 'amount', 'taxAmount'], 0)),
        'billStatus': _first_value(row, ['billStatus', 'status'], ''),
        'billStatusName': _first_value(row, ['billStatusName', 'statusName'], ''),
        'checkStatus': row.get('checkStatus'),
        'description': _first_value(row, ['description', 'remark'], ''),
        'entries': entries,
        '_product_codes': [e.get('code') for e in entries if e.get('code')],
        '_raw': row,
        '_supplier': supplier,
    }


def _cache_upsert_accessory_purchase_order(conn, order):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''
        INSERT OR REPLACE INTO accessory_purchase_orders
        (account, number, date, supplier_name, supplier_number, total_qty,
         total_amount, bill_status, bill_status_name, entries_count, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        order.get('account') or '',
        order.get('number') or '',
        order.get('date') or '',
        order.get('supplierName') or '',
        order.get('supplierNumber') or '',
        _num(order.get('totalQty')),
        _num(order.get('totalAmount')),
        str(order.get('billStatus') or ''),
        str(order.get('billStatusName') or ''),
        len(order.get('entries') or []),
        json.dumps(order, ensure_ascii=False),
        now,
    ))


def _accessory_purchase_order_projection(order):
    return {
        'number': order.get('number') or '',
        'date': order.get('date') or '',
        'supplierName': order.get('supplierName') or '',
        'supplierNumber': order.get('supplierNumber') or '',
        'account': order.get('account') or '',
        'totalQty': order.get('totalQty') or 0,
        'totalAmount': order.get('totalAmount') or 0,
        'billStatusName': order.get('billStatusName') or '',
        'entriesCount': len(order.get('entries') or []),
        'productCodes': order.get('_product_codes') or [],
    }


def _read_cached_accessory_purchase_orders(account='all', search=''):
    with _sales_cache_conn() as conn:
        clauses = []
        params = []
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        if search:
            clauses.append('(LOWER(number) LIKE ? OR LOWER(supplier_name) LIKE ? OR LOWER(supplier_number) LIKE ? OR LOWER(data_json) LIKE ?)')
            kw = f'%{search.lower()}%'
            params.extend([kw, kw, kw, kw])
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = conn.execute(f'''
            SELECT data_json FROM accessory_purchase_orders
            {where}
            ORDER BY date DESC, number DESC
        ''', params).fetchall()
    return [_accessory_purchase_order_projection(json.loads(r['data_json'])) for r in rows]


def _read_cached_accessory_purchase_order_detail(order_no, account=''):
    with _sales_cache_conn() as conn:
        if account and account != 'all':
            row = conn.execute('''
                SELECT data_json FROM accessory_purchase_orders
                WHERE number = ? AND account = ?
                LIMIT 1
            ''', (order_no, account)).fetchone()
        else:
            row = conn.execute('''
                SELECT data_json FROM accessory_purchase_orders
                WHERE number = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (order_no,)).fetchone()
    return json.loads(row['data_json']) if row else None


def _enrich_accessory_purchase_order(order):
    entries = order.get('entries') or []
    codes = [str(x.get('code') or '').strip() for x in entries if x.get('code')]
    product_map = {}
    needs_product = any(not (e.get('imageUrl') or e.get('barcode')) for e in entries)
    if needs_product and codes:
        cli, _ = _sales_client_for_account(order.get('account') or '')
        if cli:
            product_map = _product_map_by_codes(cli, codes)

    for entry in entries:
        code = str(entry.get('code') or '').strip()
        product = product_map.get(code) or {}
        if not product:
            continue
        entry['name'] = entry.get('name') or product.get('name') or product.get('productName') or ''
        entry['spec'] = product.get('spec') or entry.get('spec') or ''
        entry['barcode'] = product.get('barcode') or entry.get('barcode') or ''
        entry['unit'] = product.get('unitName') or entry.get('unit') or ''
        entry['license'] = product.get('proLicense') or entry.get('license') or ''
        imgs = product.get('multiImg') or []
        if imgs and isinstance(imgs, list):
            entry['imageUrl'] = imgs[0].get('url') or entry.get('imageUrl') or ''
    order['_product_codes'] = [e.get('code') for e in entries if e.get('code')]
    return order


def _enrich_accessory_purchase_orders_batch(cli, orders):
    codes = []
    for order in orders or []:
        for entry in (order.get('entries') or []):
            code = str(entry.get('code') or '').strip()
            if code:
                codes.append(code)
    product_map = _product_map_by_codes(cli, list(dict.fromkeys(codes))) if codes else {}

    for order in orders or []:
        for entry in (order.get('entries') or []):
            code = str(entry.get('code') or '').strip()
            product = product_map.get(code) or {}
            if not product:
                continue
            entry['name'] = entry.get('name') or product.get('name') or product.get('productName') or ''
            entry['spec'] = product.get('spec') or entry.get('spec') or ''
            entry['barcode'] = product.get('barcode') or entry.get('barcode') or ''
            entry['unit'] = product.get('unitName') or entry.get('unit') or ''
            entry['license'] = product.get('proLicense') or entry.get('license') or ''
            imgs = product.get('multiImg') or []
            if imgs and isinstance(imgs, list):
                entry['imageUrl'] = imgs[0].get('url') or entry.get('imageUrl') or ''
        order['_product_codes'] = [e.get('code') for e in (order.get('entries') or []) if e.get('code')]
    return orders


def _normalize_accessory_product(product, account):
    imgs = product.get('multiImg') or []
    image_url = ''
    if imgs and isinstance(imgs, list):
        image_url = imgs[0].get('url') or ''
    return {
        'account': account,
        'code': product.get('productNumber') or product.get('number') or '',
        'name': product.get('productName') or product.get('name') or '',
        'spec': product.get('spec') or product.get('specification') or '',
        'barcode': product.get('barcode') or product.get('barCode') or '',
        'category': product.get('categoryName') or product.get('category') or '',
        'unit': product.get('unitName') or product.get('unit') or '',
        'imageUrl': image_url,
        'license': product.get('proLicense') or '',
    }


def _fetch_accessory_products(cli, account, category='耗材', search='', page_size=100):
    category_kw = str(category or '').strip()
    search_kw = str(search or '').strip()

    def _read(use_category_filter=True, max_pages=20):
        page = 1
        rows = []
        total = None
        while True:
            result = cli.get_products(
                page=page,
                page_size=page_size,
                product_number=search_kw,
                category_name=(category_kw if (use_category_filter and not search_kw) else ''),
            )
            batch = result.get('list') or []
            for item in batch:
                norm = _normalize_accessory_product(item, account)
                if category_kw and not search_kw and category_kw not in str(norm.get('category') or ''):
                    continue
                if search_kw and search_kw.lower() not in str(norm.get('code') or '').lower():
                    continue
                rows.append(norm)
            total = result.get('total') or total
            if not batch or len(batch) < page_size:
                break
            if total and page * page_size >= int(total):
                break
            page += 1
            if page > max_pages:
                break
        return rows

    rows = _read(use_category_filter=True, max_pages=20)
    if category_kw and not search_kw and not rows:
        rows = _read(use_category_filter=False, max_pages=5)
    return rows


def _accessory_material_stock_from_inventory(cli, code):
    buckets = {'厂房物料': 0, '新村物料': 0}
    try:
        inv = cli.get_inventory_by_product(code, page_size=100)
        for row in (inv.get('list') or []):
            if str(row.get('productNumber') or '').strip() != str(code).strip():
                continue
            name = str(row.get('locationName') or row.get('location') or '').strip()
            qty = _num(row.get('qty') or row.get('quantity') or row.get('availableQty') or 0)
            if '厂房物料' in name or ('厂房' in name and '物料' in name):
                buckets['厂房物料'] += qty
            elif '新村物料' in name or ('新村' in name and '物料' in name):
                buckets['新村物料'] += qty
    except Exception as e:
        print(f'[ACCESSORY PRODUCT] inventory failed {code}: {_short_sync_error(e)}')
    return buckets


def _accessory_material_stock_from_product(product):
    buckets = {'厂房物料': 0, '新村物料': 0}
    for prop in (product.get('propertys') or []):
        name = str(prop.get('locationName') or prop.get('location') or '').strip()
        qty = _num(prop.get('quantity') or prop.get('qty') or prop.get('availableQty') or 0)
        if '厂房物料' in name or ('厂房' in name and '物料' in name):
            buckets['厂房物料'] += qty
        elif '新村物料' in name or ('新村' in name and '物料' in name):
            buckets['新村物料'] += qty
    return buckets


def _refresh_accessory_product_stocks_from_inventory(cli, account, conn):
    product_rows = conn.execute(
        'SELECT code, data_json FROM accessory_products WHERE account = ?',
        (account,),
    ).fetchall()
    product_map = {}
    for row in product_rows:
        code = row['code'] or ''
        if not code:
            continue
        try:
            product_map[code] = json.loads(row['data_json'])
        except Exception:
            product_map[code] = {'account': account, 'code': code}
    if not product_map:
        return {'scanned_inventory': 0, 'updated_stock': 0}

    stock_map = {code: {'厂房物料': 0, '新村物料': 0} for code in product_map}
    page = 1
    page_size = 500
    scanned = 0
    while True:
        result = cli._request(
            'GET',
            '/jdyscm/inventory/list',
            body=None,
            query=cli._api_query({'page': page, 'pageSize': page_size}),
            timeout=120,
        )
        rows = result.get('items') or result.get('list') or []
        scanned += len(rows)
        for item in rows:
            code = str(item.get('productNumber') or '').strip()
            if code not in stock_map:
                continue
            name = str(item.get('locationName') or item.get('location') or '').strip()
            qty = _num(item.get('qty') or item.get('quantity') or item.get('availableQty') or 0)
            if '厂房物料' in name or ('厂房' in name and '物料' in name):
                stock_map[code]['厂房物料'] += qty
            elif '新村物料' in name or ('新村' in name and '物料' in name):
                stock_map[code]['新村物料'] += qty
        total = result.get('records') or result.get('totalsize') or 0
        if not rows or len(rows) < page_size:
            break
        if total and page * page_size >= int(total):
            break
        page += 1
        if page > 300:
            break

    updated = 0
    for code, stock in stock_map.items():
        product = product_map.get(code) or {}
        product['stock'] = stock
        _cache_upsert_accessory_product(conn, product)
        updated += 1
    conn.commit()
    return {'scanned_inventory': scanned, 'updated_stock': updated}


def _cache_upsert_accessory_product(conn, product):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''
        INSERT OR REPLACE INTO accessory_products
        (account, code, name, category, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        product.get('account') or '',
        product.get('code') or '',
        product.get('name') or '',
        product.get('category') or '',
        json.dumps(product, ensure_ascii=False),
        now,
    ))


def _accessory_product_cache_stats():
    with _sales_cache_conn() as conn:
        row = conn.execute('''
            SELECT COUNT(*) AS products_count,
                   MAX(updated_at) AS updated_at
            FROM accessory_products
        ''').fetchone()
    return {
        'products_count': row['products_count'] or 0,
        'updated_at': row['updated_at'] or '',
        'categories': ACCESSORY_MATERIAL_CATEGORIES,
        'db_path': _SALES_CACHE_DB,
    }


def _read_cached_accessory_products(search='', category='', supplier=''):
    search = str(search or '').strip().lower()
    category = str(category or '').strip()
    supplier_account = ''
    supplier_number = ''
    if '|' in str(supplier or ''):
        supplier_account, supplier_number = [x.strip() for x in str(supplier).split('|', 1)]
    elif supplier:
        supplier_number = str(supplier).strip()

    clauses = []
    params = []
    if supplier_account:
        clauses.append('account = ?')
        params.append(supplier_account)
    if category and category != 'all':
        clauses.append('category = ?')
        params.append(category)
    if search:
        clauses.append('(LOWER(code) LIKE ? OR LOWER(name) LIKE ? OR LOWER(data_json) LIKE ?)')
        kw = f'%{search}%'
        params.extend([kw, kw, kw])
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    with _sales_cache_conn() as conn:
        rows = conn.execute(f'''
            SELECT data_json FROM accessory_products
            {where}
            ORDER BY account, category, code
        ''', params).fetchall()

    items = []
    categories = set()
    for row in rows:
        try:
            product = json.loads(row['data_json'])
        except Exception:
            continue
        code = product.get('code') or ''
        factory_qty, factory_orders = _accessory_factory_qty_from_cache(
            product.get('account') or '', code, supplier_number
        )
        if supplier_number and factory_qty <= 0:
            continue
        product['factoryQty'] = factory_qty
        product['factoryOrders'] = factory_orders[:8]
        product['factoryOrderCount'] = len(factory_orders)
        categories.add(product.get('category') or '')
        items.append(product)

    summary = {
        'qty': sum(_num(x.get('factoryQty')) for x in items),
        'factoryStock': sum(_num((x.get('stock') or {}).get('厂房物料')) for x in items),
        'xincunStock': sum(_num((x.get('stock') or {}).get('新村物料')) for x in items),
    }
    return items, summary, sorted(x for x in categories if x)


def _run_accessory_product_sync(reason='manual', account='all'):
    if not _accessory_product_sync_lock.acquire(blocking=False):
        return {'running': True, 'message': 'accessory product sync already running'}
    started_at = datetime.now()
    _accessory_product_sync_state.update({
        'running': True,
        'last_started_at': started_at.strftime('%Y-%m-%d %H:%M:%S'),
        'last_finished_at': '',
        'last_success': False,
        'last_error': '',
        'last_reason': reason,
    })
    try:
        scanned = 0
        cached = 0
        inventory_scanned = 0
        stock_updated = 0
        errors = []
        warnings = []
        category_set = set(ACCESSORY_MATERIAL_CATEGORIES)
        with _sales_cache_conn() as conn:
            for cli_fn, name in _sales_sources_for_account(account):
                try:
                    cli = cli_fn()
                    if not cli:
                        errors.append(f'{name}: 请先配置 JDY API')
                        continue
                    cats = []
                    try:
                        cat_result = cli.get_product_categories(page=1, page_size=300)
                        cats = cat_result.get('list') or []
                    except Exception as e:
                        errors.append(f'{name}: 商品分类读取失败 {_short_sync_error(e)}')
                    target_cats = [
                        c for c in cats
                        if str(c.get('name') or '').strip() in category_set
                    ]
                    if not target_cats:
                        warnings.append(f'{name}: 未找到耗材子分类')
                        continue
                    for cat in target_cats:
                        page = 1
                        page_size = 200
                        while True:
                            result = cli.get_products(
                                page=page,
                                page_size=page_size,
                                category_id=cat.get('id') or '',
                            )
                            rows = result.get('list') or []
                            rows = [
                                r for r in rows
                                if str(r.get('categoryName') or '').strip() == str(cat.get('name') or '').strip()
                            ]
                            scanned += len(rows)
                            for item in rows:
                                product = _normalize_accessory_product(item, name)
                                if product.get('category') not in category_set:
                                    continue
                                product['stock'] = _accessory_material_stock_from_product(item)
                                _cache_upsert_accessory_product(conn, product)
                                cached += 1
                            conn.commit()
                            if len(rows) < page_size:
                                break
                            page += 1
                            if page > 200:
                                break
                        # 精斗云该接口有时 records 仍返回全商品数，以上以本页实际分类命中数判断结束。
                    try:
                        stock_result = _refresh_accessory_product_stocks_from_inventory(cli, name, conn)
                        inventory_scanned += stock_result.get('scanned_inventory') or 0
                        stock_updated += stock_result.get('updated_stock') or 0
                    except Exception as e:
                        warnings.append(f'{name}: 库存同步失败 {_short_sync_error(e)}')
                    print(f'[ACCESSORY PRODUCT SYNC] {name}: scanned={scanned} cached={cached}')
                except Exception as e:
                    err = f'{name}: {_short_sync_error(e)}'
                    errors.append(err)
                    print(f'[ACCESSORY PRODUCT SYNC ERROR] {err}')
            conn.commit()
        finished_at = datetime.now()
        summary = {
            'scanned': scanned,
            'cached': cached,
            'inventory_scanned': inventory_scanned,
            'stock_updated': stock_updated,
            'categories': ACCESSORY_MATERIAL_CATEGORIES,
            'errors': errors,
            'warnings': warnings,
            'stats': _accessory_product_cache_stats(),
            'duration_seconds': round((finished_at - started_at).total_seconds(), 2),
        }
        _accessory_product_sync_state.update({
            'running': False,
            'last_finished_at': finished_at.strftime('%Y-%m-%d %H:%M:%S'),
            'last_success': not bool(errors),
            'last_error': '; '.join(errors[:5]),
            'summary': summary,
        })
        return summary
    except Exception as e:
        _accessory_product_sync_state.update({
            'running': False,
            'last_finished_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'last_success': False,
            'last_error': str(e),
            'summary': {},
        })
        raise
    finally:
        _accessory_product_sync_lock.release()


def _accessory_factory_qty_from_cache(account, code, supplier_number=''):
    qty = 0
    orders = []
    with _sales_cache_conn() as conn:
        clauses = ['data_json LIKE ?']
        params = [f'%{code}%']
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        if supplier_number:
            clauses.append('supplier_number = ?')
            params.append(supplier_number)
        rows = conn.execute(f'''
            SELECT data_json FROM accessory_purchase_orders
            WHERE {' AND '.join(clauses)}
            ORDER BY date DESC, number DESC
        ''', params).fetchall()
    for row in rows:
        try:
            order = json.loads(row['data_json'])
        except Exception:
            continue
        hit_qty = 0
        for entry in (order.get('entries') or []):
            if str(entry.get('code') or '').strip() == str(code).strip():
                hit_qty += _num(entry.get('qty') or 0)
        if hit_qty:
            qty += hit_qty
            orders.append({
                'number': order.get('number') or '',
                'date': order.get('date') or '',
                'supplierName': order.get('supplierName') or '',
                'supplierNumber': order.get('supplierNumber') or '',
                'qty': hit_qty,
            })
    return qty, orders


def _read_accessory_supplier_options(account='all'):
    terms = [t.lower() for t in _accessory_supplier_terms() if t]
    with _sales_cache_conn() as conn:
        params = []
        clauses = []
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = conn.execute(f'''
            SELECT account, number, name, category_text
            FROM jdy_suppliers
            {where}
            ORDER BY account, number
        ''', params).fetchall()
    result = []
    for row in rows:
        text = f"{row['category_text'] or ''} {row['name'] or ''}".lower()
        if terms and not any(t in text for t in terms):
            continue
        result.append({
            'account': row['account'] or '',
            'number': row['number'] or '',
            'name': row['name'] or '',
            'category': row['category_text'] or '',
        })
    return result


def _accessory_purchase_cache_stats():
    with _sales_cache_conn() as conn:
        row = conn.execute('''
            SELECT COUNT(*) AS orders_count,
                   MIN(date) AS min_date,
                   MAX(date) AS max_date,
                   MAX(updated_at) AS updated_at
            FROM accessory_purchase_orders
        ''').fetchone()
    return {
        'orders_count': row['orders_count'] or 0,
        'min_date': row['min_date'] or '',
        'max_date': row['max_date'] or '',
        'updated_at': row['updated_at'] or '',
        'db_path': _SALES_CACHE_DB,
    }


def _normalize_sales_entry(entry):
    qty = _num(_first_value(entry, ['qty', 'quantity', 'baseQty', 'unitQty', 'number'], 0))
    amount = _num(_first_value(entry, ['taxAmount', 'amount', 'totalAmount', 'allAmount'], 0))
    price = _num(_first_value(entry, [
        'taxPrice', 'price', 'unitPrice', 'discountPrice', 'salePrice'
    ], 0))
    if not price and qty:
        price = round(amount / qty, 6)
    wh_name = _first_value(entry, [
        'stockName', 'warehouseName', 'locationName', 'outLocationName',
        'inLocationName', 'storageName'
    ], '')
    warehouses = []
    if wh_name:
        warehouses.append({'name': wh_name, 'qty': qty})
    return {
        'code': _first_value(entry, ['productNumber', 'productCode', 'number', 'code'], ''),
        'name': _first_value(entry, ['productName', 'name', 'goodsName'], ''),
        'spec': _first_value(entry, ['specification', 'spec', 'model', 'skuName'], ''),
        'barcode': _first_value(entry, ['barCode', 'barcode', 'productBarcode'], ''),
        'qty': qty,
        'unit': _first_value(entry, ['unitName', 'unit', 'baseUnitName'], ''),
        'price': price,
        'amount': amount,
        'warehouses': warehouses,
        # 真实采购订单待收数量后续接采购订单接口聚合；这里保持只读占位。
        'purchase': {'ordered': 0, 'received': 0, 'pending': 0},
    }


def _product_map_by_codes(cli, codes):
    """批量读取商品档案，用于补规格、条码、仓库库存。只读。"""
    codes = [c for c in dict.fromkeys([str(c).strip() for c in codes if c])]
    result = {}
    for i in range(0, len(codes), 30):
        batch = codes[i:i + 30]
        try:
            for item in (cli.get_products_by_codes(batch) or []):
                no = str(item.get('productNumber') or '').strip()
                if no:
                    result[no] = item
        except Exception as e:
            print(f'[SALES] 批量商品档案读取失败: {e}')
        missing = [c for c in batch if c not in result]
        for code in missing:
            try:
                item = cli.get_product_by_code(code)
                if item:
                    result[code] = item
            except Exception as e:
                print(f'[SALES] 商品档案读取失败 {code}: {e}')
    return result


def _is_local_stock_location(name):
    name = str(name or '').strip()
    if not name:
        return False
    if any(x in name for x in ['金华', '本地', '义乌', '国内']):
        return True
    if any(x in name for x in ['新大', '在途', '工厂', '厂家', '厂房物料', '新村物料']):
        return False
    return False


def _empty_sales_stock_buckets():
    return {'新大仓库': 0, '在途': 0, '金华/本地': 0, '工厂订单': 0}


def _is_factory_order_location(name):
    name = str(name or '').strip()
    return bool(name and ('工厂订单' in name or '厂家订单' in name))


def _stock_from_product(product):
    """从商品档案 propertys 提取仓库库存：新大仓库、在途、金华/本地。"""
    buckets = _empty_sales_stock_buckets()
    for prop in (product.get('propertys') or []):
        name = str(prop.get('locationName') or prop.get('location') or '').strip()
        qty = _num(prop.get('quantity') or prop.get('qty') or prop.get('availableQty') or 0)
        if not name:
            continue
        if '新大' in name:
            buckets['新大仓库'] += qty
        elif '在途' in name:
            buckets['在途'] += qty
        elif _is_factory_order_location(name):
            buckets['工厂订单'] += qty
        elif _is_local_stock_location(name):
            buckets['金华/本地'] += qty
    return buckets


def _stock_from_inventory(cli, code):
    """从即时库存接口提取新大仓库、在途、金华/本地数量。只读。"""
    buckets = _empty_sales_stock_buckets()
    try:
        inv = cli.get_inventory_by_product(code, page_size=80)
        for row in (inv.get('list') or []):
            if str(row.get('productNumber') or '').strip() != str(code).strip():
                continue
            name = str(row.get('locationName') or '').strip()
            qty = _num(row.get('qty') or 0)
            if '新大' in name:
                buckets['新大仓库'] += qty
            elif '在途' in name:
                buckets['在途'] += qty
            elif _is_factory_order_location(name):
                buckets['工厂订单'] += qty
            elif _is_local_stock_location(name):
                buckets['金华/本地'] += qty
    except Exception as e:
        print(f'[SALES] 即时库存读取失败 {code}: {e}')
    return buckets


def _stock_from_inventory_strict(cli, code):
    """从即时库存接口提取新大仓库、在途、金华/本地数量。失败时抛出异常，避免把失败缓存成 0。"""
    buckets = _empty_sales_stock_buckets()
    inv = cli.get_inventory_by_product(code, page_size=80)
    for row in (inv.get('list') or []):
        if str(row.get('productNumber') or '').strip() != str(code).strip():
            continue
        name = str(row.get('locationName') or '').strip()
        qty = _num(row.get('qty') or 0)
        if '新大' in name:
            buckets['新大仓库'] += qty
        elif '在途' in name:
            buckets['在途'] += qty
        elif _is_factory_order_location(name):
            buckets['工厂订单'] += qty
        elif _is_local_stock_location(name):
            buckets['金华/本地'] += qty
    return buckets


def _sales_order_uses_new_factory_logic(order_date=''):
    begin_date = (_sales_sync_config().get('factory_purchase_begin_date') or '').strip()
    if not begin_date:
        return False
    order_date = str(order_date or '')[:10]
    return bool(order_date and order_date >= begin_date)


def _sales_quantity_logic_key(order_date=''):
    conf = _sales_sync_config()
    begin_date = (conf.get('factory_purchase_begin_date') or '').strip()
    if begin_date and str(order_date or '')[:10] >= begin_date:
        return f'factory-new:{begin_date}'
    return f'factory-warehouse-before:{begin_date or ""}'


def _purchase_entry_order_qty(entry, prefer_actual=False):
    if prefer_actual:
        actual = _num(_first_value(entry, [
            'receiveQty', 'receivedQty', 'actualQty', 'actualReceiveQty',
            'stockInQty', 'inQty', 'realQty', 'associatedQty'
        ], 0))
        if actual:
            return actual
    return _num(_first_value(entry, ['qty', 'mainQty', 'quantity', 'baseQty'], 0))


def _factory_qty_from_purchase(cli, code):
    """
    从未转购货单的购货订单里聚合工厂订单数量。只读。
    """
    ordered = 0
    try:
        filter_conf = _sales_factory_purchase_filter_config()
        for order in _fetch_factory_purchase_orders(cli, code, filter_conf):
            for entry in (order.get('entries') or []):
                if str(entry.get('productNumber') or '').strip() != str(code).strip():
                    continue
                ordered += _num(entry.get('qty') or entry.get('mainQty') or 0)
    except Exception as e:
        print(f'[SALES] 工厂采购数量读取失败 {code}: {e}')
    return ordered


def _factory_qty_from_checked_purchase_strict(cli, code, stock=None):
    ordered = 0
    filter_conf = _sales_factory_purchase_filter_config()
    for order in _fetch_checked_factory_purchase_orders(cli, code, filter_conf):
        linked = bool(_first_value(order, [
            'sourceBillNo', 'sourceNumber', 'linkNumber', 'relationNumber',
            'associatedNumber', 'convertNumber', 'purchaseNumber'
        ], ''))
        for entry in (order.get('entries') or []):
            if str(entry.get('productNumber') or '').strip() != str(code).strip():
                continue
            ordered += _purchase_entry_order_qty(entry, prefer_actual=linked)
    transit = _num((stock or {}).get('在途'))
    local = _num((stock or {}).get('金华/本地'))
    return max(ordered - transit - local, 0)


def _factory_qty_from_purchase_strict(cli, code, stock=None, order_date=''):
    """按切换日期读取工厂数量：切换日前读仓库，切换日及以后读采购订单。"""
    if _sales_order_uses_new_factory_logic(order_date):
        return _factory_qty_from_checked_purchase_strict(cli, code, stock)
    if stock is None:
        stock = _stock_from_inventory_strict(cli, code)
    return _num((stock or {}).get('工厂订单'))


def _apply_sales_quantities(order, quantity_map):
    missing = 0
    for entry in (order.get('entries') or []):
        code = str(entry.get('code') or '').strip()
        q = quantity_map.get(code)
        if not q:
            missing += 1
            entry.setdefault('warehouses', [])
            entry.setdefault('purchase', {'ordered': 0, 'received': 0, 'pending': 0})
            entry['quantityStatus'] = 'missing'
            continue
        stock = q.get('stock') or {}
        factory_qty = _num(q.get('factory_qty'))
        entry['warehouses'] = [
            {'name': '新大仓库', 'qty': _num(stock.get('新大仓库'))},
            {'name': '在途', 'qty': _num(stock.get('在途'))},
            {'name': '金华/本地', 'qty': _num(stock.get('金华/本地'))},
            {'name': '工厂', 'qty': factory_qty},
        ]
        entry['purchase'] = {'ordered': factory_qty, 'received': 0, 'pending': factory_qty}
        entry['quantityStatus'] = 'ok' if not q.get('error') else 'stale'
    order['quantityMissingCount'] = missing
    return order


def _enrich_sales_order(cli, order):
    """给销货单详情补充商品档案、仓库和工厂采购数量。"""
    entries = order.get('entries') or []
    codes = [x.get('code') for x in entries if x.get('code')]
    product_map = _product_map_by_codes(cli, codes)
    factory_cache = {}
    stock_cache = {}
    unique_codes = [c for c in dict.fromkeys(codes) if c]
    if unique_codes:
        workers = min(8, len(unique_codes))
        with ThreadPoolExecutor(max_workers=workers) as exe:
            futures = {exe.submit(_factory_qty_from_purchase, cli, code): code for code in unique_codes}
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    factory_cache[code] = fut.result()
                except Exception as e:
                    print(f'[SALES] 工厂采购数量并发读取失败 {code}: {e}')
                    factory_cache[code] = 0
        with ThreadPoolExecutor(max_workers=workers) as exe:
            futures = {exe.submit(_stock_from_inventory, cli, code): code for code in unique_codes}
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    stock_cache[code] = fut.result()
                except Exception as e:
                    print(f'[SALES] 即时库存并发读取失败 {code}: {e}')
                    stock_cache[code] = _empty_sales_stock_buckets()
    for entry in entries:
        code = entry.get('code')
        product = product_map.get(code) or {}
        if product:
            entry['spec'] = product.get('spec') or entry.get('spec') or ''
            entry['barcode'] = product.get('barcode') or entry.get('barcode') or ''
            entry['unit'] = product.get('unitName') or entry.get('unit') or ''
            entry['license'] = product.get('proLicense') or ''
            imgs = product.get('multiImg') or []
            if imgs and isinstance(imgs, list):
                entry['imageUrl'] = imgs[0].get('url') or ''
            stock = stock_cache.get(code) or _stock_from_product(product)
        else:
            stock = stock_cache.get(code) or _empty_sales_stock_buckets()
        factory_qty = factory_cache.get(code, 0)
        if _sales_order_uses_new_factory_logic(order.get('date') or ''):
            try:
                factory_qty = _factory_qty_from_purchase_strict(
                    cli, code, stock=stock, order_date=order.get('date') or ''
                )
            except Exception as e:
                print(f'[SALES] 新工厂逻辑读取失败 {code}: {e}')
        entry['warehouses'] = [
            {'name': '新大仓库', 'qty': stock.get('新大仓库', 0)},
            {'name': '在途', 'qty': stock.get('在途', 0)},
            {'name': '金华/本地', 'qty': stock.get('金华/本地', 0)},
            {'name': '工厂', 'qty': factory_qty},
        ]
        entry['purchase'] = {
            'ordered': factory_qty,
            'received': 0,
            'pending': factory_qty,
        }
    return order


def _normalize_sales_order(row, account):
    entries = row.get('entries') or row.get('details') or row.get('items') or row.get('lines') or []
    norm_entries = [_normalize_sales_entry(e) for e in entries if isinstance(e, dict)]
    total_qty = _num(_first_value(row, ['totalQty', 'qty', 'quantity', 'totalQuantity'], 0))
    if not total_qty and norm_entries:
        total_qty = sum(_num(x.get('qty')) for x in norm_entries)
    return {
        'number': _first_value(row, ['number', 'billNo', 'id'], ''),
        'date': str(_first_value(row, ['date', 'billDate', 'createTime'], ''))[:10],
        'customerName': _first_value(row, ['customerName', 'clientName', 'contactName', 'customer'], ''),
        'account': account,
        'totalQty': total_qty,
        'totalAmount': _num(_first_value(row, ['amount', 'taxAmount', 'totalAmount', 'allAmount', 'discountAmount'], 0)),
        'checkStatusName': _first_value(row, ['checkStatusName', 'statusName', 'status'], ''),
        'entries': norm_entries,
        '_raw': row,
    }


def _query_live_sales_orders(date_str, account, search):
    cfg1 = _load_jdy_config();  name1 = cfg1.get('name', '饰品')
    cfg2 = _load_jdy_config2(); name2 = cfg2.get('name', '箱包')
    sources = []
    if account in ('', 'all', name1):
        sources.append((_ensure_jdy_client, name1))
    if account in ('', 'all', name2):
        sources.append((_ensure_jdy_client2, name2))

    all_items, errors = [], []
    for cli_fn, name in sources:
        try:
            cli = cli_fn()
            if not cli:
                errors.append(f'{name}: 请先配置 JDY API')
                continue
            result = cli.get_sales_orders(page=1, page_size=200, search=search,
                                          begin_date=date_str, end_date=date_str)
            for row in (result.get('list') or []):
                all_items.append(_normalize_sales_order(row, name))
        except Exception as e:
            errors.append(f'{name}: {e}')
    all_items.sort(key=lambda x: x.get('date') or '', reverse=True)
    return all_items, errors


def _fetch_live_sales_orders_all(cli, begin_date, end_date, search=''):
    page = 1
    page_size = 50
    all_rows = []
    total = None
    while True:
        result = cli.get_sales_orders(page=page, page_size=page_size, search=search,
                                      begin_date=begin_date, end_date=end_date)
        rows = result.get('list') or []
        all_rows.extend(rows)
        total = result.get('total') or total
        if not rows or len(rows) < page_size:
            break
        if total and len(all_rows) >= int(total):
            break
        page += 1
        if page > 50:
            break
        time.sleep(0.6)
    return all_rows


def _iter_date_strings(begin_date, end_date):
    try:
        start = datetime.strptime(str(begin_date)[:10], '%Y-%m-%d').date()
        end = datetime.strptime(str(end_date)[:10], '%Y-%m-%d').date()
    except Exception:
        return [begin_date] if begin_date else []
    if end < start:
        start, end = end, start
    days = []
    cur = start
    while cur <= end:
        days.append(cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)
    return days


def _fetch_live_sales_orders_by_day(cli, begin_date, end_date, search=''):
    rows = []
    seen_numbers = set()
    for day in _iter_date_strings(begin_date, end_date):
        day_rows = _fetch_live_sales_orders_all(cli, day, day, search=search)
        print(f'[SALES FETCH] {day}: {len(day_rows)} rows')
        _log_event('SALES_SYNC', f'{day}: fetched {len(day_rows)} rows')
        for row in day_rows:
            key = str(row.get('number') or row.get('billNo') or row.get('id') or '')
            if key and key in seen_numbers:
                continue
            if key:
                seen_numbers.add(key)
            rows.append(row)
    return rows


def _short_sync_error(error):
    text = str(error or '').replace('\r', ' ').replace('\n', ' ').strip()
    if 'HTTP 504' in text or 'Gateway Time-out' in text:
        return '精斗云接口 504 超时，请稍后重试或调晚最早日期'
    return text[:240]


def _fetch_live_accessory_purchase_orders_all(cli, account_name, search='', begin_date=''):
    page = 1
    page_size = 20
    all_rows = []
    total = None
    supplier_cache = {}
    while True:
        result = cli.get_purchase_order_requests(
            page=page,
            page_size=page_size,
            search=search,
            bill_status=0,
            begin_date=begin_date,
        )
        rows = result.get('list') or []
        with _sales_cache_conn() as conn:
            for row in rows:
                if begin_date:
                    order_date = str(_first_value(row, ['date', 'billDate', 'createTime'], ''))[:10]
                    if order_date and order_date < begin_date:
                        continue
                supplier_no = str(_first_value(row, ['supplierNumber', 'supplierNo', 'vendorNumber'], '')).strip()
                supplier = supplier_cache.get(supplier_no)
                if supplier is None:
                    supplier = _read_cached_jdy_supplier(conn, account_name, supplier_no) if supplier_no else {}
                if supplier_no and supplier is None:
                    try:
                        supplier = cli.get_supplier_by_number(supplier_no, status=2) or {}
                        if supplier:
                            _cache_upsert_jdy_supplier(conn, account_name, supplier)
                            conn.commit()
                    except Exception as e:
                        print(f'[ACCESSORY] supplier read failed {account_name}/{supplier_no}: {_short_sync_error(e)}')
                        supplier = {}
                supplier_cache[supplier_no] = supplier or {}
                if _supplier_matches_accessory(supplier or {}, row):
                    all_rows.append(_normalize_accessory_purchase_order(row, account_name, supplier or {}))
        total = result.get('total') or total
        if not rows or len(rows) < page_size:
            break
        if total and page * page_size >= int(total):
            break
        page += 1
        if page > 80:
            break
    return all_rows


def _run_accessory_incremental_sync(reason='manual', account='all'):
    if not _accessory_sync_lock.acquire(blocking=False):
        return {'running': True, 'message': 'accessory sync already running'}
    started_at = datetime.now()
    _accessory_sync_state.update({
        'running': True,
        'last_started_at': started_at.strftime('%Y-%m-%d %H:%M:%S'),
        'last_finished_at': '',
        'last_success': False,
        'last_error': '',
        'last_reason': reason,
    })
    try:
        conf = _sales_sync_config()
        begin_date = conf.get('accessory_purchase_begin_date') or ''
        supplier_cache_results = {}
        total_seen = 0
        total_cached = 0
        errors = []
        warnings = []
        seen_keys = set()
        successful_accounts = set()
        with _sales_cache_conn() as conn:
            for cli_fn, name in _sales_sources_for_account(account):
                try:
                    cli = cli_fn()
                    if not cli:
                        errors.append(f'{name}: JDY API not configured')
                        continue
                    try:
                        supplier_cache_results[name] = _refresh_supplier_cache_for_account(
                            cli, name, conf.get('accessory_supplier_cache_hours') or 24
                        )
                    except Exception as e:
                        warn = f'{name}: 供应商缓存刷新失败，已改用订单供应商逐个缓存（{_short_sync_error(e)}）'
                        warnings.append(warn)
                        print(f'[ACCESSORY SYNC WARN] {warn}')
                    rows = _fetch_live_accessory_purchase_orders_all(
                        cli, name, begin_date=begin_date
                    )
                    rows = _enrich_accessory_purchase_orders_batch(cli, rows)
                    total_seen += len(rows)
                    for order in rows:
                        key = (order.get('account') or '', order.get('number') or '')
                        seen_keys.add(key)
                        _cache_upsert_accessory_purchase_order(conn, order)
                    total_cached += len(rows)
                    successful_accounts.add(name)
                    print(f'[ACCESSORY SYNC] {name}: cached={len(rows)} begin={begin_date or "-"}')
                except Exception as e:
                    err = f'{name}: {_short_sync_error(e)}'
                    errors.append(err)
                    print(f'[ACCESSORY SYNC ERROR] {err}')

            if account in ('', 'all'):
                current_rows = conn.execute('SELECT account, number FROM accessory_purchase_orders').fetchall()
                for row in current_rows:
                    key = (row['account'] or '', row['number'] or '')
                    if key[0] in successful_accounts and key not in seen_keys:
                        conn.execute(
                            'DELETE FROM accessory_purchase_orders WHERE account = ? AND number = ?',
                            key,
                        )
            else:
                if account in successful_accounts:
                    current_rows = conn.execute(
                        'SELECT account, number FROM accessory_purchase_orders WHERE account = ?',
                        (account,),
                    ).fetchall()
                    for row in current_rows:
                        key = (row['account'] or '', row['number'] or '')
                        if key not in seen_keys:
                            conn.execute(
                                'DELETE FROM accessory_purchase_orders WHERE account = ? AND number = ?',
                                key,
                            )
            conn.commit()

        finished_at = datetime.now()
        summary = {
            'begin_date': begin_date,
            'seen': total_seen,
            'cached': total_cached,
            'errors': errors,
            'warnings': warnings,
            'supplier_cache': supplier_cache_results,
            'stats': _accessory_purchase_cache_stats(),
            'duration_seconds': round((finished_at - started_at).total_seconds(), 2),
        }
        _accessory_sync_state.update({
            'running': False,
            'last_finished_at': finished_at.strftime('%Y-%m-%d %H:%M:%S'),
            'last_success': not bool(errors),
            'last_error': '; '.join(errors[:5]),
            'summary': summary,
        })
        return summary
    except Exception as e:
        _accessory_sync_state.update({
            'running': False,
            'last_finished_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'last_success': False,
            'last_error': str(e),
            'summary': {},
        })
        raise
    finally:
        _accessory_sync_lock.release()


def _fetch_sales_quantity_map(cli, account, codes, order_date=''):
    """批量读取并缓存商品库存/工厂数量。失败的商品沿用旧缓存，不写入假 0。"""
    codes = [c for c in dict.fromkeys([str(c).strip() for c in codes if c])]
    cached = _read_cached_sales_product_quantities(account, codes)
    if not codes:
        return cached, []

    errors = []
    workers = min(16, len(codes))

    def _fetch_one(code):
        stock = _stock_from_inventory_strict(cli, code)
        factory_qty = _factory_qty_from_purchase_strict(cli, code, stock=stock, order_date=order_date)
        return code, {'stock': stock, 'factory_qty': factory_qty, 'error': ''}

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_fetch_one, code): code for code in codes}
        with _sales_cache_conn() as conn:
            done = 0
            for fut in as_completed(futures):
                code = futures[fut]
                done += 1
                try:
                    _, row = fut.result()
                    cached[code] = row
                    _cache_upsert_sales_product_quantity(
                        conn, account, code, row['stock'], row['factory_qty'], ''
                    )
                except Exception as e:
                    err = str(e)
                    errors.append(f'{code}: {err}')
                    if code in cached:
                        cached[code]['error'] = err
                    print(f'[SALES CACHE] 数量读取失败 {account}/{code}: {err}')
                if done % 100 == 0 or done == len(codes):
                    print(f'[SALES CACHE] {account} 数量进度 {done}/{len(codes)}')
            conn.commit()
    return cached, errors


def _enrich_sales_orders_batch(cli, orders, include_quantities=False, account=''):
    """批量补全一批销货单。

    月度缓存只批量补商品档案，库存和工厂采购数量在打开单据详情时懒加载，
    避免一次同步把每个商品都逐个查库存/采购而卡很久。
    """
    all_codes = []
    for order in orders:
        for entry in (order.get('entries') or []):
            code = entry.get('code')
            if code:
                all_codes.append(code)
    unique_codes = [c for c in dict.fromkeys(all_codes) if c]
    product_map = _product_map_by_codes(cli, unique_codes)
    quantity_map = {}
    if include_quantities and unique_codes:
        quantity_map, _ = _fetch_sales_quantity_map(cli, account, unique_codes)

    for order in orders:
        for entry in (order.get('entries') or []):
            code = entry.get('code')
            product = product_map.get(code) or {}
            if product:
                entry['spec'] = product.get('spec') or entry.get('spec') or ''
                entry['barcode'] = product.get('barcode') or entry.get('barcode') or ''
                entry['unit'] = product.get('unitName') or entry.get('unit') or ''
                entry['license'] = product.get('proLicense') or ''
                imgs = product.get('multiImg') or []
                if imgs and isinstance(imgs, list):
                    entry['imageUrl'] = imgs[0].get('url') or ''
            if include_quantities:
                _apply_sales_quantities({'entries': [entry]}, quantity_map)
            else:
                entry.setdefault('warehouses', [])
                entry.setdefault('purchase', {'ordered': 0, 'received': 0, 'pending': 0})
        if include_quantities:
            order['quantityLogicVersion'] = _sales_quantity_logic_key(order.get('date') or '')
    return orders


def _sales_client_for_account(account):
    cfg1 = _load_jdy_config();  name1 = cfg1.get('name', '饰品')
    cfg2 = _load_jdy_config2(); name2 = cfg2.get('name', '箱包')
    if account == name2:
        return _ensure_jdy_client2(), name2
    if account == name1:
        return _ensure_jdy_client(), name1
    return None, account


def _sales_detail_needs_quantity_enrich(order):
    entries = order.get('entries') or []
    if not entries:
        return False
    required = {'新大仓库', '在途', '金华/本地', '工厂'}
    for entry in entries:
        names = {str(w.get('name') or '') for w in (entry.get('warehouses') or [])}
        if not required.issubset(names):
            return True
    return False


def _refresh_cached_sales_detail_quantities(order):
    account = order.get('account') or ''
    cli, account_name = _sales_client_for_account(account)
    if not cli:
        return order, '当前账套未配置 JDY API，无法刷新库存和工厂数量。'
    codes = [
        str(entry.get('code') or '').strip()
        for entry in (order.get('entries') or [])
        if str(entry.get('code') or '').strip()
    ]
    if not codes:
        return order, ''
    quantity_map, errors = _fetch_sales_quantity_map(
        cli,
        account_name or account,
        codes,
        order_date=order.get('date') or '',
    )
    _apply_sales_quantities(order, quantity_map)
    order['quantityLogicVersion'] = _sales_quantity_logic_key(order.get('date') or '')
    with _sales_cache_conn() as conn:
        _cache_upsert_sales_detail(conn, order)
        _cache_upsert_sales_order(conn, order)
        conn.commit()
    return order, '；'.join(errors[:5])


def _refresh_sales_cache(begin_date, end_date, account='all'):
    cfg1 = _load_jdy_config();  name1 = cfg1.get('name', '饰品')
    cfg2 = _load_jdy_config2(); name2 = cfg2.get('name', '箱包')
    sources = []
    if account in ('', 'all', name1):
        sources.append((_ensure_jdy_client, name1))
    if account in ('', 'all', name2):
        sources.append((_ensure_jdy_client2, name2))

    total_orders = 0
    total_details = 0
    errors = []
    with _sales_cache_conn() as conn:
        for cli_fn, name in sources:
            try:
                cli = cli_fn()
                if not cli:
                    errors.append(f'{name}: 请先配置 JDY API')
                    continue
                raw_rows = _fetch_live_sales_orders_by_day(cli, begin_date, end_date)
                orders = [_normalize_sales_order(row, name) for row in raw_rows]
                orders = _enrich_sales_orders_batch(cli, orders, include_quantities=True, account=name)
                for order in orders:
                    _cache_upsert_sales_order(conn, order)
                    _cache_upsert_sales_detail(conn, order)
                conn.commit()
                total_orders += len(orders)
                total_details += len(orders)
                print(f'[SALES CACHE] {name} {begin_date}~{end_date}: {len(orders)} 单')
            except Exception as e:
                errors.append(f'{name}: {e}')
                print(f'[SALES CACHE ERROR] {name}: {e}')
    return {
        'orders': total_orders,
        'details': total_details,
        'errors': errors,
        'stats': _sales_cache_stats(),
    }


def _refresh_sales_quantities_from_cache(begin_date, end_date, account='all'):
    cfg1 = _load_jdy_config();  name1 = cfg1.get('name', '饰品')
    cfg2 = _load_jdy_config2(); name2 = cfg2.get('name', '箱包')
    clients = {}
    if account in ('', 'all', name1):
        clients[name1] = _ensure_jdy_client()
    if account in ('', 'all', name2):
        clients[name2] = _ensure_jdy_client2()

    total_orders = 0
    total_codes = 0
    errors = []
    with _sales_cache_conn() as conn:
        clauses = ['date >= ?', 'date <= ?']
        params = [begin_date, end_date]
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        rows = conn.execute(f'''
            SELECT account, number, data_json
            FROM sales_details
            WHERE {' AND '.join(clauses)}
            ORDER BY date, number
        ''', params).fetchall()

    by_account = {}
    for row in rows:
        by_account.setdefault(row['account'], []).append(json.loads(row['data_json']))

    with _sales_cache_conn() as conn:
        for acct, orders in by_account.items():
            cli = clients.get(acct)
            if not cli:
                errors.append(f'{acct}: 请先配置 JDY API')
                continue
            codes = []
            for order in orders:
                for entry in (order.get('entries') or []):
                    if entry.get('code'):
                        codes.append(entry.get('code'))
            unique_codes = [c for c in dict.fromkeys(codes) if c]
            total_codes += len(unique_codes)
            quantity_map, qty_errors = _fetch_sales_quantity_map(cli, acct, unique_codes)
            if qty_errors:
                errors.extend([f'{acct}/{x}' for x in qty_errors[:50]])
                if len(qty_errors) > 50:
                    errors.append(f'{acct}: 另有 {len(qty_errors) - 50} 个商品数量读取失败')
            for order in orders:
                _apply_sales_quantities(order, quantity_map)
                _cache_upsert_sales_order(conn, order)
                _cache_upsert_sales_detail(conn, order)
            conn.commit()
            total_orders += len(orders)
            print(f'[SALES CACHE] {acct} 本地明细数量补全: {len(orders)} 单, {len(unique_codes)} 款')

    return {
        'orders': total_orders,
        'codes': total_codes,
        'errors': errors,
        'stats': _sales_cache_stats(),
    }


@app.route('/sales-orders', methods=['GET'])
def sales_orders_list():
    """
    销货单接口。source=live 只读精斗云；source=mock 使用本地模拟数据。
    """
    try:
        date_str = request.args.get('date', '') or datetime.now().strftime('%Y-%m-%d')
        account = request.args.get('account', 'all')
        search = request.args.get('search', '').strip().lower()
        source = request.args.get('source', 'cache')
        errors = []
        if source == 'cache':
            items = _read_cached_sales_orders(date_str, account, search)
        elif source == 'live':
            items, errors = _query_live_sales_orders(date_str, account, search)
            if not items and errors:
                return jsonify({'success': False, 'error': '；'.join(errors), 'list': [], 'mock': False})
        else:
            items = _mock_sales_orders(date_str)
            if account and account != 'all':
                items = [x for x in items if x.get('account') == account]
            if search:
                items = [
                    x for x in items
                    if search in (x.get('number', '') + x.get('customerName', '')).lower()
                ]
        return jsonify({
            'success': True,
            'list': items,
            'total': len(items),
            'summary': {
                'qty': sum(float(x.get('totalQty') or 0) for x in items),
                'amount': sum(float(x.get('totalAmount') or 0) for x in items),
            },
            'mock': source == 'mock',
            'cache': source == 'cache',
            'errors': errors,
            'cache_stats': _sales_cache_stats() if source == 'cache' else None,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/sales-summary-products', methods=['GET'])
def sales_summary_products():
    """销售汇总按商品：只读取本地 SQLite/JSON 缓存，不调用 JDY。"""
    try:
        today = datetime.now().date()
        start_default = (today - timedelta(days=59)).strftime('%Y-%m-%d')
        end_default = today.strftime('%Y-%m-%d')
        start_date = (request.args.get('start_date') or request.args.get('begin_date') or start_default)[:10]
        end_date = (request.args.get('end_date') or end_default)[:10]
        account = request.args.get('account', 'all') or 'all'
        q = request.args.get('q', '') or request.args.get('search', '')
        sort = request.args.get('sort', 'qty_desc') or 'qty_desc'
        try:
            limit = int(request.args.get('limit', 500))
        except Exception:
            limit = 500
        try:
            offset = int(request.args.get('offset', 0))
        except Exception:
            offset = 0
        result = _read_cached_sales_summary_products(
            start_date, end_date, account=account, q=q, limit=limit, offset=offset, sort=sort
        )
        return jsonify({
            'success': True,
            'source': 'local_cache',
            'start_date': start_date,
            'end_date': end_date,
            'account': account,
            'total': result['total'],
            'limit': max(1, min(limit, 1000)),
            'offset': max(0, offset),
            'error_count': result['error_count'],
            'items': result['items'],
            'summary': result['summary'],
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] sales_summary_products: {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


@app.route('/sales-order/<order_no>', methods=['GET'])
def sales_order_detail(order_no):
    """
    销货单详情接口。source=cache 读本地缓存；source=live 只读精斗云；source=mock 使用本地模拟数据。
    """
    try:
        date_str = request.args.get('date', '') or datetime.now().strftime('%Y-%m-%d')
        account = request.args.get('account', 'all')
        source = request.args.get('source', 'cache')
        if source == 'cache':
            cached = _read_cached_sales_detail(order_no, account)
            if cached:
                warning = ''
                logic_key = _sales_quantity_logic_key(cached.get('date') or date_str)
                uses_new_logic = _sales_order_uses_new_factory_logic(cached.get('date') or date_str)
                should_refresh_qty = (
                    _sales_detail_needs_quantity_enrich(cached)
                    or (uses_new_logic and cached.get('quantityLogicVersion') != logic_key)
                )
                if should_refresh_qty:
                    try:
                        cached, warning = _refresh_cached_sales_detail_quantities(cached)
                    except Exception as e:
                        warning = f'本单库存/工厂数量刷新失败，已显示本地旧缓存：{e}'
                return jsonify({
                    'success': True,
                    'data': cached,
                    'mock': False,
                    'cache': True,
                    'warning': warning,
                })
            return jsonify({'success': False, 'error': '本地缓存未找到这张销货单，请先同步本月数据或切换实际读取'}), 404
        if source == 'live':
            cfg2 = _load_jdy_config2()
            cfg1 = _load_jdy_config()
            name1 = cfg1.get('name', '饰品')
            name2 = cfg2.get('name', '箱包')
            candidates = []
            if account == name2:
                candidates = [(_ensure_jdy_client2, name2)]
            elif account == name1:
                candidates = [(_ensure_jdy_client, name1)]
            else:
                candidates = [(_ensure_jdy_client, name1), (_ensure_jdy_client2, name2)]

            errors = []
            for cli_fn, name in candidates:
                try:
                    cli = cli_fn()
                    if not cli:
                        errors.append(f'{name}: 请先配置 JDY API')
                        continue
                    detail = cli.get_sales_order_detail(order_no)
                    if not isinstance(detail, dict) or not (detail.get('entries') or detail.get('number')):
                        errors.append(f'{name}: 未找到销货单')
                        continue
                    if detail.get('number') and str(detail.get('number')) != str(order_no):
                        errors.append(f'{name}: 未找到销货单')
                        continue
                    order = _normalize_sales_order(detail, name)
                    return jsonify({'success': True, 'data': _enrich_sales_order(cli, order), 'mock': False, 'cache': False})
                except Exception as e:
                    errors.append(f'{name}: {e}')
            return jsonify({'success': False, 'error': '；'.join(errors) or '未找到销货单'}), 404
        orders = _mock_sales_orders(date_str)
        for item in orders:
            if item.get('number') == order_no and (account in ('', 'all') or item.get('account') == account):
                return jsonify({'success': True, 'data': item, 'mock': True})
        return jsonify({'success': False, 'error': '未找到销货单'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/sales-cache/status', methods=['GET'])
def sales_cache_status():
    """查看销货单本地缓存状态。"""
    return jsonify({'success': True, 'stats': _sales_cache_stats()})


@app.route('/sales-cache-refresh', methods=['POST'])
def sales_cache_refresh():
    """只读精斗云并刷新本地销货单缓存。"""
    try:
        payload = request.get_json(silent=True) or {}
        today = datetime.now()
        begin_default = today.replace(day=1).strftime('%Y-%m-%d')
        end_default = today.strftime('%Y-%m-%d')
        begin_date = (
            request.args.get('begin_date')
            or payload.get('begin_date')
            or begin_default
        )
        end_date = (
            request.args.get('end_date')
            or payload.get('end_date')
            or end_default
        )
        account = request.args.get('account') or payload.get('account') or 'all'
        quantities_only = str(
            request.args.get('quantities_only')
            or payload.get('quantities_only')
            or ''
        ).lower() in ('1', 'true', 'yes', 'y')
        if quantities_only:
            result = _refresh_sales_quantities_from_cache(begin_date, end_date, account)
        else:
            result = _refresh_sales_cache(begin_date, end_date, account)
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/sales-sync-config', methods=['GET'])
def sales_sync_config_get():
    return jsonify({
        'success': True,
        'config': _sales_sync_config(),
        'state': _sales_sync_state,
        'config_debug': _runtime_config_debug(),
    })


@app.route('/sales-sync-config', methods=['POST'])
def sales_sync_config_set():
    try:
        data = request.get_json(force=True) or {}

        def _bounded_int(name, default, min_value, max_value):
            try:
                value = int(data.get(name, default))
            except Exception:
                value = default
            return max(min_value, min(max_value, value))

        expire_action = str(data.get('expire_action') or 'delete').strip().lower()
        if expire_action not in ('delete', 'archive'):
            expire_action = 'delete'
        daily_compare_time = str(data.get('daily_compare_time') or '').strip()
        archive_path = str(data.get('archive_path') or '').strip()
        factory_switch_date = str(data.get('factory_purchase_begin_date') or '').strip()
        factory_disabled_suppliers = str(data.get('factory_disabled_suppliers') or '').strip()
        accessory_purchase_begin_date = str(data.get('accessory_purchase_begin_date') or '').strip()
        accessory_supplier_terms = str(data.get('accessory_supplier_terms') or '').strip()
        updates = {
            'sales_auto_sync_enabled': False,
            'sales_auto_sync_interval_minutes': _bounded_int('interval_minutes', 5, 1, 1440),
            'sales_cache_keep_days': _bounded_int('keep_days', 30, 1, 365),
            'sales_incremental_lookback_days': _bounded_int('lookback_days', 3, 1, 60),
            'sales_cache_manual_days': _bounded_int('lookback_days', 3, 1, 60),
            'sales_cache_expire_action': expire_action,
            'accessory_auto_sync_enabled': False,
            'accessory_auto_sync_interval_minutes': _bounded_int('accessory_interval_minutes', 5, 1, 1440),
            'accessory_supplier_terms': str(data.get('accessory_supplier_terms') or '').strip() or '辅料供应商\n辅料',
        }
        if not accessory_supplier_terms:
            updates.pop('accessory_supplier_terms', None)
        if daily_compare_time:
            updates['jdy_daily_compare_time'] = daily_compare_time
            updates['sales_cache_daily_time'] = daily_compare_time
        if archive_path:
            updates['sales_cache_archive_path'] = archive_path
        if factory_switch_date:
            updates['sales_factory_purchase_begin_date'] = factory_switch_date
            updates['sales_factory_new_logic_begin_date'] = factory_switch_date
            updates['factory_purchase_begin_date'] = factory_switch_date
            updates['factory_new_logic_begin_date'] = factory_switch_date
        if factory_disabled_suppliers:
            updates['sales_factory_disabled_suppliers'] = factory_disabled_suppliers
            updates['factory_disabled_suppliers'] = factory_disabled_suppliers
        if accessory_purchase_begin_date:
            updates['accessory_purchase_begin_date'] = accessory_purchase_begin_date
        if accessory_supplier_terms:
            updates['accessory_supplier_terms'] = accessory_supplier_terms
            updates['accessory_supplier_keywords'] = accessory_supplier_terms
        _save_jdy_config(updates)
        return jsonify({'success': True, 'config': _sales_sync_config()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/sales-sync/status', methods=['GET'])
def sales_sync_status():
    _webhook_state['pending'] = _webhook_pending_count()
    return jsonify({
        'success': True,
        'config': _sales_sync_config(),
        'state': _sales_sync_state,
        'webhook_state': _webhook_state,
        'daily_compare_state': _daily_compare_state,
    })


@app.route('/jdy-webhook/status', methods=['GET'])
def jdy_webhook_status():
    snapshot = _webhook_status_snapshot(request.args.get('limit') or 20)
    state = dict(_webhook_state)
    state['pending'] = snapshot['counts'].get('pending', 0)
    return jsonify({
        'success': True,
        'endpoint': {
            'local_ready': True,
            'path': '/jdy-webhook',
            'public': True,
            'method': 'GET/POST',
            'remote_url': 'http://gongdashuai.top:5008/jdy-webhook',
        },
        'state': state,
        'counts': snapshot['counts'],
        'recent': snapshot['recent'],
        'resource_summary': snapshot['resource_summary'],
        'notes': snapshot['notes'],
    })


@app.route('/sales-sync-run', methods=['POST'])
def sales_sync_run():
    try:
        wait = str(request.args.get('wait') or '').lower() in ('1', 'true', 'yes', 'y')
        if wait:
            result = _run_sales_incremental_sync(reason='manual')
            return jsonify({'success': True, 'started': True, 'result': result, 'state': _sales_sync_state})
        if _sales_sync_lock.locked() or _sales_sync_state.get('running'):
            return jsonify({'success': True, 'started': False, 'running': True, 'state': _sales_sync_state})
        t = threading.Thread(
            target=_run_sales_incremental_sync,
            kwargs={'reason': 'manual'},
            daemon=True,
            name='sales-sync-manual',
        )
        t.start()
        return jsonify({'success': True, 'started': True, 'running': True, 'state': _sales_sync_state})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'state': _sales_sync_state}), 500


@app.route('/sales-cache-cleanup', methods=['POST'])
def sales_cache_cleanup():
    try:
        data = request.get_json(force=True) or {}
        begin_date = str(data.get('begin_date') or '').strip()
        end_date = str(data.get('end_date') or '').strip()
        if not begin_date or not end_date:
            return jsonify({'success': False, 'error': 'begin_date and end_date are required'}), 400
        action = str(data.get('action') or 'delete').strip().lower()
        if action not in ('delete', 'archive'):
            action = 'delete'
        archive_path = str(data.get('archive_path') or '').strip()
        result = _cleanup_sales_cache_range(begin_date, end_date, action, archive_path)
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reorder-items/import', methods=['POST'])
def reorder_items_import():
    try:
        data = request.get_json(force=True) or {}
        items = data.get('items') or []
        source = data.get('source') or {}
        order_no = str(data.get('order_number') or source.get('number') or '').strip()
        account = str(data.get('account') or source.get('account') or '').strip()
        if not items and order_no:
            order = _read_cached_sales_detail(order_no, account)
            if not order:
                return jsonify({'success': False, 'error': '本地没有这张销售单详情，请先同步销售单'}), 404
            source = {
                'type': 'sales',
                'number': order.get('number') or order_no,
                'date': order.get('date') or '',
                'customer': order.get('customerName') or '',
                'user': session.get('auth_name') or session.get('auth_user') or '',
            }
            account = order.get('account') or account
            wanted = {str(x).strip() for x in (data.get('codes') or []) if str(x).strip()}
            items = [
                e for e in (order.get('entries') or [])
                if not wanted or _reorder_entry_identity(e) in wanted
            ]
        if not items:
            return jsonify({'success': False, 'error': '请选择需要返单的商品'}), 400

        cli = None
        try:
            if source.get('type') == 'sales_summary':
                cli = None
            elif account:
                for cli_fn, name in _sales_sources_for_account(account):
                    if name == account:
                        cli = cli_fn()
                        break
            if source.get('type') != 'sales_summary':
                cli = cli or _ensure_jdy_client() or _ensure_jdy_client2()
        except Exception:
            cli = None

        inserted = 0
        updated = 0
        created_ids = []
        with _sales_cache_conn() as conn:
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                item_account = raw.get('account') or account
                item = _reorder_item_payload_from_entry(raw, item_account, source, cli)
                if not item.get('code'):
                    continue
                rid, is_new = _cache_insert_reorder_item(conn, item)
                created_ids.append(rid)
                if is_new:
                    inserted += 1
                else:
                    updated += 1
            conn.commit()
        return jsonify({
            'success': True,
            'inserted': inserted,
            'updated': updated,
            'ids': created_ids,
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] reorder_items_import: {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


@app.route('/reorder-suppliers', methods=['GET'])
def reorder_suppliers():
    try:
        status = request.args.get('status', 'pending').strip() or 'pending'
        account = request.args.get('account', '').strip()
        search = request.args.get('search', '').strip().lower()
        clauses = []
        params = []
        if status and status != 'all':
            clauses.append('status = ?')
            params.append(status)
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        if search:
            clauses.append('''
                (
                    LOWER(COALESCE(supplier_name, '')) LIKE ?
                    OR LOWER(COALESCE(supplier_number, '')) LIKE ?
                    OR LOWER(COALESCE(code, '')) LIKE ?
                    OR LOWER(COALESCE(name, '')) LIKE ?
                )
            ''')
            kw = f'%{search}%'
            params.extend([kw, kw, kw, kw])
        where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
        with _sales_cache_conn() as conn:
            rows = conn.execute(f'''
                SELECT supplier_number, supplier_name,
                       COUNT(*) AS count,
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                       SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_count,
                       SUM(CASE WHEN status IN ('completed', 'done', 'ordered') THEN 1 ELSE 0 END) AS completed_count,
                       SUM(CASE WHEN status = 'ignored' THEN 1 ELSE 0 END) AS ignored_count,
                       SUM(sales_qty_60) AS sales_qty_60,
                       SUM(stock_new) AS stock_new,
                       SUM(stock_transit) AS stock_transit,
                       SUM(stock_local) AS stock_local,
                       SUM(factory_qty) AS factory_qty,
                       SUM(suggested_qty) AS suggested_qty,
                       SUM(confirmed_qty) AS confirmed_qty,
                       MIN(created_at) AS first_created_at,
                       MAX(created_at) AS latest_created_at,
                       MAX(updated_at) AS updated_at,
                       GROUP_CONCAT(DISTINCT account) AS accounts
                FROM reorder_items
                {where}
                GROUP BY COALESCE(supplier_number, ''), COALESCE(supplier_name, '')
                ORDER BY latest_created_at DESC, updated_at DESC, supplier_name, supplier_number
            ''', params).fetchall()
        items = []
        for row in rows:
            supplier_number = row['supplier_number'] or ''
            supplier_name = row['supplier_name'] or '未识别供应商'
            items.append({
                'supplierKey': supplier_number or supplier_name,
                'supplierNumber': supplier_number,
                'supplier_number': supplier_number,
                'supplierName': supplier_name,
                'supplier_name': supplier_name,
                'count': row['count'] or 0,
                'itemCount': row['count'] or 0,
                'item_count': row['count'] or 0,
                'pendingCount': row['pending_count'] or 0,
                'confirmedCount': row['confirmed_count'] or 0,
                'completedCount': row['completed_count'] or 0,
                'ignoredCount': row['ignored_count'] or 0,
                'salesQty60': _num(row['sales_qty_60']),
                'stockNew': _num(row['stock_new']),
                'stockTransit': _num(row['stock_transit']),
                'stockLocal': _num(row['stock_local']),
                'factoryQty': _num(row['factory_qty']),
                'suggestedQty': _num(row['suggested_qty']),
                'confirmedQty': _num(row['confirmed_qty']),
                'latestCreatedAt': row['latest_created_at'] or '',
                'createdAt': row['first_created_at'] or '',
                'updatedAt': row['updated_at'] or '',
                'accounts': row['accounts'] or '',
            })
        return jsonify({'success': True, 'list': items, 'total': len(items)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reorder-items', methods=['GET'])
def reorder_items():
    try:
        status = request.args.get('status', 'pending').strip() or 'pending'
        account = request.args.get('account', '').strip()
        supplier = request.args.get('supplier', '').strip()
        supplier_number = request.args.get('supplier_number', '').strip()
        supplier_name = request.args.get('supplier_name', '').strip()
        search = request.args.get('search', '').strip().lower()
        clauses = []
        params = []
        if status and status != 'all':
            clauses.append('status = ?')
            params.append(status)
        if account and account != 'all':
            clauses.append('account = ?')
            params.append(account)
        if supplier_number:
            clauses.append('supplier_number = ?')
            params.append(supplier_number)
        elif supplier_name:
            clauses.append('supplier_name = ?')
            params.append(supplier_name)
        elif supplier:
            clauses.append('(supplier_number = ? OR supplier_name = ?)')
            params.extend([supplier, supplier])
        if search:
            clauses.append('''
                (
                    LOWER(COALESCE(code, '')) LIKE ?
                    OR LOWER(COALESCE(name, '')) LIKE ?
                    OR LOWER(COALESCE(spec, '')) LIKE ?
                    OR LOWER(COALESCE(barcode, '')) LIKE ?
                    OR LOWER(COALESCE(supplier_name, '')) LIKE ?
                    OR LOWER(COALESCE(supplier_number, '')) LIKE ?
                )
            ''')
            kw = f'%{search}%'
            params.extend([kw, kw, kw, kw, kw, kw])
        where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
        with _sales_cache_conn() as conn:
            rows = conn.execute(f'''
                SELECT * FROM reorder_items
                {where}
                ORDER BY supplier_name, supplier_number, updated_at DESC, id DESC
            ''', params).fetchall()
        items = [_reorder_row_to_item(row) for row in rows]
        summary = {
            'count': len(items),
            'salesQty60': sum(_num(x.get('salesQty60')) for x in items),
            'stockNew': sum(_num(x.get('stockNew')) for x in items),
            'stockTransit': sum(_num(x.get('stockTransit')) for x in items),
            'stockLocal': sum(_num(x.get('stockLocal')) for x in items),
            'factoryQty': sum(_num(x.get('factoryQty')) for x in items),
            'suggestedQty': sum(_num(x.get('suggestedQty')) for x in items),
            'confirmedQty': sum(_num(x.get('confirmedQty')) for x in items),
        }
        return jsonify({'success': True, 'list': items, 'summary': summary})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reorder-recent-styles', methods=['GET'])
def reorder_recent_styles():
    supplier = (request.args.get('supplier') or '').strip()
    account = (request.args.get('account') or '').strip()
    limit = max(1, min(int(request.args.get('limit') or 50), 100))
    if not supplier:
        return jsonify({'success': False, 'error': '缺少 supplier 参数'}), 400
    seen = set()
    items = []
    with _sales_cache_conn() as conn:
        queries = [
            ('accessory_purchase_orders', '''
                SELECT account, number, data_json FROM accessory_purchase_orders
                WHERE supplier_name = ? {account_where}
                ORDER BY date DESC, updated_at DESC
                LIMIT 200
            '''),
            ('purchase_inbounds', '''
                SELECT account, number, data_json FROM purchase_inbounds
                WHERE supplier_name = ? {account_where}
                ORDER BY date DESC, updated_at DESC
                LIMIT 200
            '''),
        ]
        for table, sql_tpl in queries:
            account_where = 'AND account = ?' if account else ''
            params = [supplier] + ([account] if account else [])
            rows = conn.execute(sql_tpl.format(account_where=account_where), params).fetchall()
            for row in rows:
                try:
                    order = json.loads(row['data_json'] or '{}')
                except Exception:
                    continue
                supplier_info = {
                    'supplierName': supplier,
                    'supplierNumber': _first_value(order, ['supplierNumber', 'supplierNo', 'vendorNumber'], ''),
                }
                for entry in _reorder_entries_from_order(order):
                    if not isinstance(entry, dict):
                        continue
                    item = _reorder_recent_style_from_entry(entry, row['account'] or account, supplier_info, order, conn)
                    if not item:
                        continue
                    key = (item.get('account') or '', item.get('code') or '')
                    if key in seen:
                        continue
                    seen.add(key)
                    item['sourceTable'] = table
                    items.append(item)
                    if len(items) >= limit:
                        return jsonify({'success': True, 'list': items, 'total': len(items), 'supplier': supplier})
    return jsonify({'success': True, 'list': items, 'total': len(items), 'supplier': supplier})


def _normalize_reorder_status(status):
    status = str(status or '').strip()
    aliases = {'done': 'completed', 'ordered': 'completed'}
    return aliases.get(status, status)


@app.route('/reorder-items/<int:item_id>', methods=['POST', 'PATCH'])
def reorder_item_update(item_id):
    try:
        data = request.get_json(force=True) or {}
        allowed = {'pending', 'confirmed', 'completed', 'ignored'}
        status = _normalize_reorder_status(data.get('status'))
        if status and status not in allowed:
            return jsonify({'success': False, 'error': '状态无效'}), 400
        confirmed_qty = _num(data.get('confirmed_qty'))
        note = str(data.get('note') or '').strip()
        updates = []
        params = []
        if status:
            updates.append('status = ?')
            params.append(status)
        if 'confirmed_qty' in data:
            updates.append('confirmed_qty = ?')
            params.append(confirmed_qty)
        if 'note' in data:
            updates.append('note = ?')
            params.append(note)
        if not updates:
            return jsonify({'success': True, 'updated': False})
        updates.append('updated_at = ?')
        params.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        params.append(item_id)
        with _sales_cache_conn() as conn:
            cur = conn.execute(f"UPDATE reorder_items SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
            row = conn.execute('SELECT * FROM reorder_items WHERE id = ?', (item_id,)).fetchone()
        return jsonify({
            'success': True,
            'updated': bool(cur.rowcount),
            'item': _reorder_row_to_item(row) if row else None,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reorder-item-attachments/<int:item_id>', methods=['GET'])
def reorder_item_attachments(item_id):
    try:
        config_begin, config_new_logic = _attachment_config_dates()
        with _sales_cache_conn() as conn:
            row = conn.execute('SELECT * FROM reorder_items WHERE id = ?', (item_id,)).fetchone()
            if not row:
                return jsonify({
                    'success': False,
                    'error': '返单商品不存在',
                    'attachment_switch_date': ATTACHMENT_SWITCH_DATE,
                    'source_rule': ATTACHMENT_SOURCE_RULE,
                    'config_factory_purchase_begin_date': config_begin,
                    'config_factory_new_logic_begin_date': config_new_logic,
                    'config_matches_attachment_rule': _attachment_config_matches_rule(config_begin, config_new_logic),
                }), 404
            item = _reorder_row_to_item(row)
            account = str(item.get('account') or '').strip()
            source_number = str(item.get('source_number') or item.get('sourceNumber') or '').strip()
            source_date = str(item.get('source_date') or item.get('sourceDate') or '').strip()[:10]
            preferred_source = _attachment_preferred_source(source_date)
            attachments = []
            message = ''

            if not source_date:
                message = '返单来源日期为空，无法判断附件来源'
            else:
                attachments = _read_local_bill_attachments(conn, account, preferred_source, source_number)
                if not attachments and preferred_source == 'purchase_inbound':
                    attachments = _read_local_purchase_inbound_attachments(conn, account, source_number)
                    if not attachments:
                        message = '主来源无附件或本地未缓存'
                elif not attachments and preferred_source == 'purchase_order':
                    attachments = _read_local_purchase_order_attachments(conn, account, source_number)
                    if not attachments:
                        message = '采购订单附件本地未缓存'

        return jsonify({
            'success': True,
            'item': {
                'id': item.get('id'),
                'account': item.get('account') or '',
                'code': item.get('code') or '',
                'name': item.get('name') or '',
                'source_type': item.get('source_type') or item.get('sourceType') or '',
                'source_number': source_number,
                'source_date': source_date,
            },
            'attachment_switch_date': ATTACHMENT_SWITCH_DATE,
            'config_factory_purchase_begin_date': config_begin,
            'config_factory_new_logic_begin_date': config_new_logic,
            'config_matches_attachment_rule': _attachment_config_matches_rule(config_begin, config_new_logic),
            'preferred_source': preferred_source,
            'source_rule': ATTACHMENT_SOURCE_RULE,
            'attachments': attachments,
            'message': message or ('已读取本地附件' if attachments else '主来源无附件或本地未缓存'),
            'local_only': True,
            'live_lookup': False,
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] reorder_item_attachments: {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


@app.route('/reorder-product-history/<path:code>', methods=['GET'])
def reorder_product_history(code):
    try:
        account = request.args.get('account', '').strip()
        refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        allow_live = request.args.get('allow_live', '').lower() in ('1', 'true', 'yes')
        if refresh and allow_live:
            for cli_fn, name in _sales_sources_for_account(account or 'all'):
                cli = cli_fn()
                if not cli:
                    continue
                try:
                    result = cli.get_purchase_orders_by_product(code, page=1, page_size=20)
                    with _sales_cache_conn() as conn:
                        for row in result.get('list') or []:
                            _cache_upsert_purchase_inbound(conn, name, row)
                        conn.commit()
                except Exception as e:
                    print(f'[REORDER] refresh purchase history failed {name}/{code}: {_short_sync_error(e)}')
        items = _read_cached_purchase_history(account, code, limit=30)
        year = datetime.now().strftime('%Y')
        year_items = [x for x in items if str(x.get('date') or '').startswith(year)]
        return jsonify({
            'success': True,
            'code': code,
            'list': items,
            'history': items,
            'yearCount': len(year_items),
            'latest': items[0] if items else None,
            'source': 'local_cache',
            'liveRefreshSkipped': bool(refresh and not allow_live),
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] reorder_product_history: {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


@app.route('/purchase-inbound-verify', methods=['GET'])
def purchase_inbound_verify():
    try:
        number = request.args.get('number', '').strip()
        code = request.args.get('code', '').strip()
        account = request.args.get('account', 'all').strip() or 'all'
        if not number:
            return jsonify({'success': False, 'error': '缺少购货单号'}), 400
        results = []
        errors = []
        for cli_fn, name in _sales_sources_for_account(account):
            cli = cli_fn()
            if not cli:
                errors.append(f'{name}: 未配置 JDY')
                continue
            try:
                result = cli.get_purchase_orders(page=1, page_size=20, search=number)
                rows = result.get('list') or []
                for row in rows:
                    row_no = str(_first_value(row, ['number', 'billNo', 'id'], '')).strip()
                    if row_no and row_no != number:
                        continue
                    entries = row.get('entries') or []
                    matched = [e for e in entries if _reorder_purchase_entry_matches(e, code)] if code else entries
                    attachments = _extract_purchase_attachments(row)
                    with _sales_cache_conn() as conn:
                        _cache_upsert_purchase_inbound(conn, name, row)
                        conn.commit()
                    results.append({
                        'account': name,
                        'number': row_no or number,
                        'date': str(_first_value(row, ['date', 'billDate', 'createTime'], ''))[:10],
                        'supplierNumber': _first_value(row, ['supplierNumber', 'supplierNo', 'vendorNumber'], ''),
                        'supplierName': _first_value(row, ['supplierName', 'vendorName'], ''),
                        'containsProduct': bool(matched),
                        'matchedEntries': matched,
                        'attachments': attachments,
                        'attachmentCount': len(attachments),
                        'rawKeys': sorted(str(k) for k in row.keys()),
                    })
            except Exception as e:
                errors.append(f'{name}: {_short_sync_error(e)}')
        return jsonify({
            'success': True,
            'number': number,
            'code': code,
            'list': results,
            'errors': errors,
        })
    except Exception as e:
        tb = traceback.format_exc()
        print(f'[ERROR] purchase_inbound_verify: {tb}')
        return jsonify({'success': False, 'error': str(e), 'traceback': tb}), 500


@app.route('/accessory-orders', methods=['GET'])
def accessory_orders_list():
    try:
        account = request.args.get('account', 'all')
        search = request.args.get('search', '').strip().lower()
        items = _read_cached_accessory_purchase_orders(account, search)
        return jsonify({
            'success': True,
            'list': items,
            'total': len(items),
            'summary': {
                'qty': sum(float(x.get('totalQty') or 0) for x in items),
                'amount': sum(float(x.get('totalAmount') or 0) for x in items),
            },
            'cache': True,
            'cache_stats': _accessory_purchase_cache_stats(),
            'state': _accessory_sync_state,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/accessory-order/<order_no>', methods=['GET'])
def accessory_order_detail(order_no):
    try:
        account = request.args.get('account', '').strip()
        order = _read_cached_accessory_purchase_order_detail(order_no, account)
        if not order:
            return jsonify({'success': False, 'error': '未找到本地辅料订单'}), 404
        order = _enrich_accessory_purchase_order(order)
        with _sales_cache_conn() as conn:
            _cache_upsert_accessory_purchase_order(conn, order)
            conn.commit()
        return jsonify({'success': True, 'data': order, 'cache': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/accessory-suppliers', methods=['GET'])
def accessory_suppliers():
    try:
        account = request.args.get('account', 'all').strip()
        items = _read_accessory_supplier_options(account)
        return jsonify({'success': True, 'list': items, 'total': len(items)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/accessory-products', methods=['GET'])
def accessory_products():
    try:
        category = request.args.get('category', 'all').strip() or 'all'
        search = request.args.get('search', '').strip()
        supplier = request.args.get('supplier', '').strip()
        items, summary, categories = _read_cached_accessory_products(search, category, supplier)
        stats = _accessory_product_cache_stats()
        if not items and not stats.get('products_count') and not _accessory_product_sync_state.get('running'):
            threading.Thread(
                target=_run_accessory_product_sync,
                kwargs={'reason': 'auto-empty'},
                daemon=True,
                name='accessory-product-sync-auto-empty',
            ).start()
        return jsonify({
            'success': True,
            'list': items,
            'total': len(items),
            'summary': summary,
            'categories': ACCESSORY_MATERIAL_CATEGORIES,
            'matched_categories': categories,
            'cache_stats': stats,
            'sync_state': _accessory_product_sync_state,
            'errors': [],
            'error': '',
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/accessory-products-sync/status', methods=['GET'])
def accessory_products_sync_status():
    return jsonify({
        'success': True,
        'state': _accessory_product_sync_state,
        'stats': _accessory_product_cache_stats(),
    })


@app.route('/accessory-products-sync-run', methods=['POST'])
def accessory_products_sync_run():
    try:
        wait = str(request.args.get('wait') or '').lower() in ('1', 'true', 'yes', 'y')
        if wait:
            result = _run_accessory_product_sync(reason='manual')
            return jsonify({'success': True, 'started': True, 'result': result, 'state': _accessory_product_sync_state})
        if _accessory_product_sync_lock.locked() or _accessory_product_sync_state.get('running'):
            return jsonify({'success': True, 'started': False, 'running': True, 'state': _accessory_product_sync_state})
        t = threading.Thread(
            target=_run_accessory_product_sync,
            kwargs={'reason': 'manual'},
            daemon=True,
            name='accessory-product-sync-manual',
        )
        t.start()
        return jsonify({'success': True, 'started': True, 'running': True, 'state': _accessory_product_sync_state})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'state': _accessory_product_sync_state}), 500


@app.route('/accessory-orders-sync/status', methods=['GET'])
def accessory_orders_sync_status():
    return jsonify({
        'success': True,
        'config': _sales_sync_config(),
        'state': _accessory_sync_state,
        'stats': _accessory_purchase_cache_stats(),
        'supplier_stats': _supplier_cache_stats(),
    })


@app.route('/accessory-orders-sync-run', methods=['POST'])
def accessory_orders_sync_run():
    try:
        wait = str(request.args.get('wait') or '').lower() in ('1', 'true', 'yes', 'y')
        account = request.args.get('account') or 'all'
        if wait:
            result = _run_accessory_incremental_sync(reason='manual', account=account)
            return jsonify({'success': True, 'started': True, 'result': result, 'state': _accessory_sync_state})
        if _accessory_sync_lock.locked() or _accessory_sync_state.get('running'):
            return jsonify({'success': True, 'started': False, 'running': True, 'state': _accessory_sync_state})
        t = threading.Thread(
            target=_run_accessory_incremental_sync,
            kwargs={'reason': 'manual', 'account': account},
            daemon=True,
            name='accessory-sync-manual',
        )
        t.start()
        return jsonify({'success': True, 'started': True, 'running': True, 'state': _accessory_sync_state})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'state': _accessory_sync_state}), 500


@app.route('/jdy')
def jdy_ui():
    """加载 PyWebView 桌面 UI（jdy.html）"""
    import os
    html_path = os.path.join(_BASE, 'templates', 'jdy.html')
    if not os.path.exists(html_path):
        # 生产包中查 exe 同目录
        exe_dir = _EXE_DIR if getattr(sys, 'frozen', False) else _BASE
        html_path = os.path.join(exe_dir, 'templates', 'jdy.html')
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}
    except FileNotFoundError:
        return '<h2>jdy.html 未找到</h2>', 404


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # 启动前初始化两个 JDY 账套客户端（如已配置）
    cfg = _load_jdy_config()
    if cfg['client_id'] and cfg['app_key'] and cfg['app_secret'] and cfg['db_id']:
        jdy_api.init_client(
            cfg['client_id'], cfg['app_key'], cfg['app_secret'],
            cfg['db_id'], cfg.get('domain', ''), cfg.get('app_signature', ''),
            cfg.get('client_secret', '')
        )
        print(f'[JDY] 账套1({cfg.get("name","饰品")}) 已初始化，dbId={cfg["db_id"]}')

    cfg2 = _load_jdy_config2()
    if cfg2['client_id'] and cfg2['app_key'] and cfg2['app_secret'] and cfg2['db_id']:
        jdy_api.init_client_2(
            cfg2['client_id'], cfg2['app_key'], cfg2['app_secret'],
            cfg2['db_id'], cfg2.get('domain', ''), cfg2.get('app_signature', ''),
            cfg2.get('client_secret', '')
        )
        print(f'[JDY] 账套2({cfg2.get("name","箱包")}) 已初始化，dbId={cfg2["db_id"]}')

    print(f'✅ 报关服务已启动：http://localhost:{PORT}')
    print(f'   按 Ctrl+C 停止服务')
    if _workers_disabled():
        print('[WORKERS] workers disabled by QIHANG_DISABLE_WORKERS')
    else:
        _start_sales_sync_worker()
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=PORT, threads=16,
              channel_timeout=300, cleanup_interval=30)
    except ImportError:
        # waitress 未安装时降级为 Flask 开发服务器（仅单线程，不推荐多用户）
        print('[WARN] waitress 未安装，使用 Flask 开发服务器（单线程）')
        print('       建议运行: pip install waitress')
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
