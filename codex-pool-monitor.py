# -*- coding: utf-8 -*-
"""codex-pool 远程监控服务 (阶段二, 只读)

部署在 VPS 上: 接收本地 GUI 推送的账号快照(access_token), 自行轮询各账号
5h/周额度, 提供状态页与 JSON API, 异常(凭证失效/打满/全员打满)推送告警。

- 只读: 只调用免费的 usage 接口, 绝不刷新 token(避免与本地刷新轮换打架),
  绝不执行切换, 不持有 refresh_token。
- 安全: 所有请求需 X-Auth-Key(或 ?key=); 快照文件权限 0600。
  明文 HTTP 部署时, token 会在传输中暴露 — 生产建议套 TLS(caddy/nginx)
  或仅走 SSH 隧道。

用法(VPS):  python3 codex-pool-monitor.py            # 读同目录 monitor-config.json
配置示例:
  {"port": 8787, "auth_key": "换成强随机串", "poll_minutes": 20,
   "telegram_bot_token": "", "telegram_chat_id": "", "webhook_url": ""}
"""
import importlib.util
import json
import os
import pathlib
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / 'monitor-data'
CFG_FILE = HERE / 'monitor-config.json'
ACCOUNTS_FILE = DATA / 'accounts.json'
STATE_FILE = DATA / 'state.json'

# 复用核心库的额度查询 (需与 codex-pool.py 同目录)
_spec = importlib.util.spec_from_file_location('poolcore', HERE / 'codex-pool.py')
pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pool)


def log(msg):
    print(f'[{time.strftime("%m-%d %H:%M:%S")}] {msg}', flush=True)


def load_cfg():
    cfg = {'port': 8787, 'auth_key': '', 'poll_minutes': 20,
           'telegram_bot_token': '', 'telegram_chat_id': '', 'webhook_url': ''}
    try:
        cfg.update(json.loads(CFG_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass
    return cfg


CFG = load_cfg()
if not CFG['auth_key']:
    sys.exit('拒绝启动: monitor-config.json 里必须配置非空 auth_key')


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path, obj, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


# ---------- 告警 ----------

def notify(text):
    log('ALERT: ' + text)
    tg = CFG.get('telegram_bot_token'), CFG.get('telegram_chat_id')
    if tg[0] and tg[1]:
        try:
            body = json.dumps({'chat_id': tg[1], 'text': '[CodexPool] ' + text}).encode()
            req = urllib.request.Request(
                f'https://api.telegram.org/bot{tg[0]}/sendMessage',
                data=body, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f'telegram 推送失败: {e}')
    if CFG.get('webhook_url'):
        try:
            body = json.dumps({'text': '[CodexPool] ' + text}).encode()
            req = urllib.request.Request(CFG['webhook_url'], data=body,
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f'webhook 推送失败: {e}')


# ---------- 轮询 ----------

def fmt_ts(ts):
    return time.strftime('%m-%d %H:%M', time.localtime(ts)) if ts else '-'


def poll_once(kick=False):
    accounts = load_json(ACCOUNTS_FILE, [])
    state = load_json(STATE_FILE, {'accounts': {}, 'events': [], 'notified': {}})
    prev, events = state['accounts'], state['events']
    now = time.time()
    reached_any_valid = False
    all_valid_reached = bool(accounts)
    for acc in accounts:
        slot = str(acc.get('slot'))
        fake_auth = {'tokens': {'access_token': acc.get('access_token') or '',
                                'account_id': acc.get('account_id') or ''}}
        rec = dict(prev.get(slot) or {})
        rec.update({'email': acc.get('email'), 'plan': acc.get('plan'),
                    'sub_until': acc.get('sub_until'), 'checked_at': now})
        try:
            u = pool.fetch_usage(fake_auth)
            rec.update({'primary': u['primary'], 'secondary': u['secondary'],
                        'primary_reset_at': u['primary_reset_at'],
                        'secondary_reset_at': u['secondary_reset_at'],
                        'reached': u['reached'], 'cards': u['cards'], 'err': ''})
            was = (prev.get(slot) or {}).get('reached')
            if u['reached'] and not was:
                ev = f'[{slot}] {rec.get("email")} 打满 (5h={u["primary"]} 周={u["secondary"]})'
                events.append({'ts': now, 'ev': ev})
                notify(ev)
            if not u['reached'] and was:
                events.append({'ts': now,
                               'ev': f'[{slot}] {rec.get("email")} 已恢复可用'})
            if u['reached']:
                all_valid_reached = all_valid_reached and True
            else:
                all_valid_reached = False
                reached_any_valid = True
        except Exception as e:
            err = pool.http_err_short(e)
            rec.update({'err': err})
            key = f'err{slot}'
            if '401' in err:
                ev = f'[{slot}] {rec.get("email")} 凭证失效(401) — 可能被封或需重新同步'
                if not state['notified'].get(key):
                    events.append({'ts': now, 'ev': ev})
                    notify(ev)
                    state['notified'][key] = now
            elif not state['notified'].get(key):
                events.append({'ts': now, 'ev': f'[{slot}] 查询失败: {err}'})
            all_valid_reached = False
        state['accounts'][slot] = rec
    if accounts and all_valid_reached:
        if not state['notified'].get('allmax'):
            ev = '全部账号均打满, 无可用号'
            events.append({'ts': now, 'ev': ev})
            notify(ev)
            state['notified']['allmax'] = now
    elif reached_any_valid:
        state['notified'].pop('allmax', None)
    state['events'] = events[-200:]
    save_json(STATE_FILE, state)
    log(f'轮询完成: {len(accounts)} 个账号' + (' (同步触发)' if kick else ''))


def poll_loop(kick_event):
    while True:
        kick_event.wait(timeout=max(60, int(CFG['poll_minutes'] * 60)))
        kick_event.clear()
        try:
            poll_once()
        except Exception as e:
            log('轮询异常: ' + repr(e))


# ---------- HTTP ----------

DASH = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60"><title>Codex Pool Monitor</title>
<style>body{font-family:system-ui,Segoe UI,sans-serif;margin:24px;background:#f6f7f9}
table{border-collapse:collapse;background:#fff}td,th{border:1px solid #ddd;padding:6px 12px;font-size:14px}
.reached{color:#c00;font-weight:600}.err{color:#b60}h1{font-size:20px}
small{color:#888}li{font-size:13px;margin:2px 0}</style></head><body>
<h1>Codex Pool Monitor</h1><small>每 __POLL__ 分钟自动刷新页面与轮询 | 服务器时间 __NOW__</small>
<p><table><tr><th>槽位</th><th>邮箱</th><th>套餐</th><th>订阅至</th><th>5h</th><th>5h重置</th>
<th>周</th><th>周重置</th><th>重置卡</th><th>状态</th><th>检查于</th></tr>
__ROWS__</table></p>
<h3>事件</h3><ul>__EVENTS__</ul></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        key = self.headers.get('X-Auth-Key') or ''
        if not key:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            key = (q.get('key') or [''])[0]
        if key != CFG['auth_key']:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return False
        return True

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if not self._authed():
            return
        if self.path.split('?')[0] != '/sync':
            self.send_response(404)
            self.end_headers()
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n).decode('utf-8'))
            accounts = body.get('accounts') or []
            save_json(ACCOUNTS_FILE, accounts, private=True)
            state = load_json(STATE_FILE, {'accounts': {}, 'events': [], 'notified': {}})
            for slot, u in (body.get('usage') or {}).items():
                rec = state['accounts'].setdefault(str(slot), {})
                rec.update(u)
                rec['checked_at'] = time.time()
            save_json(STATE_FILE, state)
            threading.Thread(target=poll_once, kwargs={'kick': True}, daemon=True).start()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'accounts': len(accounts)}).encode())
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f'{{"error":{json.dumps(str(e))}}}'.encode())

    def do_GET(self):
        if not self._authed():
            return
        path = self.path.split('?')[0]
        if path == '/health':
            body = b'{"ok":true}'
        elif path == '/api/status':
            body = json.dumps(load_json(STATE_FILE, {}),
                              ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
            return
        elif path == '/':
            state = load_json(STATE_FILE, {'accounts': {}, 'events': []})
            rows = []
            for slot in sorted(state.get('accounts', {}), key=lambda x: (x.isdigit(), int(x) if x.isdigit() else 0, x)):
                a = state['accounts'][slot]
                cls = 'reached' if a.get('reached') else ('err' if a.get('err') else '')
                status = ('打满 MAXED' if a.get('reached')
                          else (a.get('err') or 'OK'))
                rows.append(
                    f"<tr class={cls!r}><td>{slot}</td><td>{a.get('email') or '?'}</td>"
                    f"<td>{a.get('plan') or ''}</td><td>{a.get('sub_until') or ''}</td>"
                    f"<td>{a.get('primary') if a.get('primary') is not None else 'n/a'}</td>"
                    f"<td>{fmt_ts(a.get('primary_reset_at'))}</td>"
                    f"<td>{a.get('secondary') if a.get('secondary') is not None else 'n/a'}</td>"
                    f"<td>{fmt_ts(a.get('secondary_reset_at'))}</td>"
                    f"<td>{a.get('cards') if a.get('cards') is not None else ''}</td>"
                    f"<td>{status}</td><td>{fmt_ts(a.get('checked_at'))}</td></tr>"
                    .replace("'", '"'))
            evs = ''.join(
                f"<li>{fmt_ts(e.get('ts'))} — {e.get('ev')}</li>"
                for e in reversed(state.get('events', [])[-40:]))
            body = (DASH
                    .replace('__POLL__', str(CFG['poll_minutes']))
                    .replace('__NOW__', time.strftime('%Y-%m-%d %H:%M:%S'))
                    .replace('__ROWS__', ''.join(rows))
                    .replace('__EVENTS__', evs or '<li>(none)</li>')
                    ).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            body = b'{"error":"not found"}'
        self.send_response(200 if path == '/health' else 404)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


def main():
    DATA.mkdir(exist_ok=True)
    kick = threading.Event()
    threading.Thread(target=poll_loop, args=(kick,), daemon=True).start()
    if load_json(ACCOUNTS_FILE, []):
        threading.Thread(target=poll_once, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', int(CFG['port'])), Handler)
    log(f'监控服务已启动: http://0.0.0.0:{CFG["port"]}/  (轮询间隔 {CFG["poll_minutes"]} 分钟)')
    log('提醒: 明文 HTTP 下 token 同步可被窃听, 生产请套 TLS 或走 SSH 隧道。')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
