"""
精斗云报关桌面应用 — PyWebView 入口
运行方式：python jdy_app.py
          或打包后双击 jdy_app.exe
"""
import sys
import os
import threading
import time

# ── PyInstaller 路径兼容 ─────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    _BASE    = sys._MEIPASS
    _EXE_DIR = os.path.dirname(sys.executable)
    sys.path.insert(0, _EXE_DIR)   # 允许从 exe 同目录热更新 .py 文件
else:
    _BASE    = os.path.dirname(os.path.abspath(__file__))
    _EXE_DIR = _BASE

sys.path.insert(0, _BASE)

# ── 导入 Flask 应用（复用 server.py） ────────────────────────────────────────
from server import app, PORT
import jdy_api
from ai_identify import _load_config

PORT_JDY = PORT   # 与 server.py 同端口


def _start_flask():
    """在后台线程启动 Flask（不阻塞主线程）"""
    # 初始化 JDY 客户端
    cfg = _load_config()
    ak  = cfg.get('jdy_app_key', '')
    cs  = cfg.get('jdy_client_secret', '')
    db  = cfg.get('jdy_db_id', '')
    if ak and cs and db:
        jdy_api.init_client(ak, cfg.get('jdy_app_secret') or cs, cs, db)
        print(f'[JDY] 客户端已初始化 dbId={db}')

    print(f'[Flask] 启动 http://127.0.0.1:{PORT_JDY}')
    app.run(host='127.0.0.1', port=PORT_JDY, debug=False, use_reloader=False)


def _wait_for_flask(timeout=10):
    """等待 Flask 就绪（轮询 /health）"""
    import http.client
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection('127.0.0.1', PORT_JDY, timeout=2)
            conn.request('GET', '/health')
            resp = conn.getresponse()
            if resp.status == 200:
                return True
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        time.sleep(0.3)
    return False


def main():
    # 1. 后台启动 Flask
    t = threading.Thread(target=_start_flask, daemon=True)
    t.start()

    # 2. 等待服务就绪
    if not _wait_for_flask():
        print('[WARN] Flask 启动超时，仍然打开窗口…')

    # 3. 打开 PyWebView 窗口
    try:
        import webview
    except ImportError:
        print('[ERROR] 未安装 pywebview，请执行: pip install pywebview')
        sys.exit(1)

    url = f'http://127.0.0.1:{PORT_JDY}/jdy'
    print(f'[WebView] 打开 {url}')

    window = webview.create_window(
        title='报关助手 — 精斗云工作台',
        url=url,
        width=1280,
        height=820,
        min_size=(900, 600),
        resizable=True,
        text_select=True,
    )

    # macOS 需要在主线程运行 GUI
    webview.start(debug=False)


if __name__ == '__main__':
    main()
