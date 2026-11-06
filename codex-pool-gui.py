# -*- coding: utf-8 -*-
"""Codex 号池控制台 GUI

上侧操作台: add(退出Codex并隔离当前凭证) / get(收取并验证刚登录的号) /
            Status(查询各账号 5h 与周额度、订阅期) / use(切换) / rm(删除槽位)
下侧账号台: 可滚动表格, 记录邮箱/密码/套餐/订阅期/额度, 鼠标选中后可在操作台操作。

启动: pythonw codex-pool-gui.py   (日志: .codex-pool/gui.log)
"""
import datetime
import glob  # noqa: F401  打包时随 GUI 收集, 供内嵌的 codex-pool.py 使用
import importlib.util
import json
import os
import pathlib
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

BASE = pathlib.Path.home() / '.codex-pool'
SLOTS = BASE / 'slots'
CURRENT = BASE / 'current'
PENDING = BASE / 'pending'
META = BASE / 'pool.json'
BACKUPS = BASE / 'backups'
GUILOG = BASE / 'gui.log'
CODEX_HOME = pathlib.Path.home() / '.codex'
LIVE = CODEX_HOME / 'auth.json'

# 加载共享核心脚本: PyInstaller 打包环境从打包目录取, 源码环境取同目录/数据目录
if getattr(sys, 'frozen', False):
    _core = pathlib.Path(getattr(sys, '_MEIPASS', '.')) / 'codex-pool.py'
else:
    _here = pathlib.Path(__file__).resolve().parent
    _core = _here / 'codex-pool.py'
    if not _core.exists():
        _core = BASE / 'codex-pool.py'

AUTH_TOKEN_URL = 'https://auth.openai.com/oauth/token'
RATE_URL = 'https://chatgpt.com/backend-api/codex/usage'
DEFAULT_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
CODEX_UA = {
    'User-Agent': 'codex_cli_rs/0.42.0 (Windows 10; x86_64) WindowsTerminal',
    'originator': 'codex_cli_rs',
}

# ---------- 代理(自动探测本机代理端口, 可手动覆盖) ----------
PROXY_FILE = BASE / 'proxy.json'
PROXY_CANDIDATES = [7897, 7890, 7891, 7899, 10808, 10809, 1080, 2080, 8888, 8889]
_opener = None
_opener_proxy = '?'


def _port_listening(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def detect_proxy():
    """返回 'http://127.0.0.1:port' 或 None(直连)。proxy.json 可手动指定/禁用。"""
    try:
        conf = json.loads(PROXY_FILE.read_text(encoding='utf-8'))
        p = (conf.get('proxy') or '').strip()
        if p.lower() == 'none':
            return None
        if p.lower() not in ('', 'auto'):
            return p
    except Exception:
        pass
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        server = ''
        try:
            server, _ = winreg.QueryValueEx(k, 'ProxyServer')
        except Exception:
            pass
        # ProxyEnable=0 但地址还在且端口在听: 客户端没开系统代理开关, 照样用它
        if server and '=' not in server:
            host, _, port = server.rpartition(':')
            if port.isdigit() and _port_listening(int(port)):
                return f'http://{host or "127.0.0.1"}:{port}'
    except Exception:
        pass
    for port in PROXY_CANDIDATES:
        if _port_listening(port):
            return f'http://127.0.0.1:{port}'
    return None


def get_opener():
    global _opener, _opener_proxy
    proxy = detect_proxy()
    if _opener is None or _opener_proxy != proxy:
        handler = urllib.request.ProxyHandler(
            {'https': proxy, 'http': proxy} if proxy else {})
        _opener = urllib.request.build_opener(handler)
        _opener_proxy = proxy
    return _opener

# 复用核心脚本里的槽位/解析逻辑, 单一数据源 (加载位置见文件头 _core)
_spec = importlib.util.spec_from_file_location('poolcore', _core)
pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pool)


def log(msg):
    line = f'[{datetime.datetime.now():%m-%d %H:%M:%S}] {msg}\n'
    try:
        with open(GUILOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass
    return line


# ---------- 凭证与额度 ----------

def exp_epoch(auth):
    try:
        return int(pool.jwt_payload(auth['tokens']['access_token'])['exp'])
    except Exception:
        return 0


def save_auth(path, auth):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def refresh_auth(auth):
    """用 refresh_token 换新 access_token, 就地更新并返回。失败抛异常。"""
    tok = auth['tokens']
    body = {
        'grant_type': 'refresh_token',
        'refresh_token': tok['refresh_token'],
        'client_id': tok.get('oauth_provenance', {}).get('client_id', DEFAULT_CLIENT_ID),
        'scope': 'openid profile email offline_access',
    }
    req = urllib.request.Request(
        AUTH_TOKEN_URL, data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with get_opener().open(req, timeout=25) as r:
        out = json.loads(r.read().decode('utf-8'))
    tok['access_token'] = out['access_token']
    if out.get('id_token'):
        tok['id_token'] = out['id_token']
    if out.get('refresh_token'):
        tok['refresh_token'] = out['refresh_token']
    auth['last_refresh'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return auth


def fetch_usage(auth):
    """返回 dict: primary/secondary 用量%, 各自重置秒数, 是否打满。失败抛异常。"""
    hdr = {
        'Authorization': 'Bearer ' + auth['tokens']['access_token'],
        'ChatGPT-Account-Id': auth['tokens'].get('account_id') or pool.account_id(auth) or '',
        **CODEX_UA,
    }
    req = urllib.request.Request(RATE_URL, headers=hdr)
    with get_opener().open(req, timeout=25) as r:
        data = json.loads(r.read().decode('utf-8'))
    rl = data.get('rate_limit') or {}
    # 按窗口时长识别(18000s=5h, 604800s=周), 兼容新版"仅周额度"账号
    win5 = winw = None
    for w in (rl.get('primary_window'), rl.get('secondary_window')):
        if not w:
            continue
        secs = w.get('limit_window_seconds')
        if secs == 18000 and win5 is None:
            win5 = w
        elif secs == 604800 and winw is None:
            winw = w
    return {
        'primary': win5.get('used_percent') if win5 else None,
        'secondary': winw.get('used_percent') if winw else None,
        'primary_reset': win5.get('reset_after_seconds') if win5 else None,
        'secondary_reset': winw.get('reset_after_seconds') if winw else None,
        'primary_reset_at': win5.get('reset_at') if win5 else None,
        'secondary_reset_at': winw.get('reset_at') if winw else None,
        'reached': bool(rl.get('limit_reached')),
        'cards': (data.get('rate_limit_reset_credits') or {}).get('available_count'),
    }


def fmt_dur(sec):
    if sec is None:
        return '?'
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f'{d}天{h}h'
    if h:
        return f'{h}h{m:02d}m'
    return f'{m}m'


_WDAY = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


def fmt_reset(ts):
    """绝对重置时间: 今天 22:47 / 周四 14:20 / 11-12 14:20"""
    if not ts:
        return ''
    try:
        dt = datetime.datetime.fromtimestamp(int(ts))
    except Exception:
        return ''
    now = datetime.datetime.now()
    if dt.date() == now.date():
        return f'今天 {dt:%H:%M}'
    if 0 < (dt.date() - now.date()).days <= 7:
        return f'{_WDAY[dt.weekday()]} {dt:%H:%M}'
    return f'{dt:%m-%d %H:%M}'


def http_err_short(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return '凭证失效(401, 需重新登录该号)'
        if e.code == 403:
            return '被拒(403): 多半是没走代理/地区拦截, 凭证本身不一定有问题'
        if e.code == 429:
            return '请求过频(429)'
        return f'HTTP {e.code}'
    if isinstance(e, urllib.error.URLError):
        return f'连不上({e.reason})'
    return f'{type(e).__name__}: {e}'[:80]


# ---------- 进程与槽位辅助 ----------

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
KILL_ERROR = ''


def _taskkill(*extra):
    return subprocess.run(['taskkill', '/IM', 'codex.exe', *extra],
                          capture_output=True, creationflags=CREATE_NO_WINDOW)


def _codex_pids():
    """列出 codex.exe 的 PID 列表(无窗口)。"""
    r = subprocess.run(
        ['tasklist', '/FI', 'IMAGENAME eq codex.exe', '/FO', 'CSV', '/NH'],
        capture_output=True, creationflags=CREATE_NO_WINDOW)
    pids = []
    for line in r.stdout.decode('gbk', errors='replace').splitlines():
        if line.lower().startswith('"codex.exe"'):
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) > 1 and parts[1].isdigit():
                pids.append(int(parts[1]))
    return pids


def kill_err_hint():
    e = KILL_ERROR or ''
    if '拒绝访问' in e or 'denied' in e.lower() or 'access' in e.lower():
        return ('(taskkill 被拒绝: Codex 可能以管理员身份在运行。'
                '请手动退出 Codex 后再点切换, 或以管理员身份运行本 GUI。)')
    if e:
        return f'(taskkill: {e[:80]})'
    return ''


def kill_codex():
    """退出 Codex Desktop: 先优雅关闭(2s), 再强杀补刀(约10s)。返回是否全部退出。"""
    global KILL_ERROR
    KILL_ERROR = ''
    _taskkill()  # 不带 /F: 发送关闭消息, 让它优雅退出
    for _ in range(4):
        time.sleep(0.5)
        if not _codex_pids():
            return True
    for i in range(9):
        r = _taskkill('/F', '/T')
        if i == 0:
            KILL_ERROR = (r.stderr or b'').decode('gbk', errors='replace').strip()
        time.sleep(1.0)
        if not _codex_pids():
            return True
    return False


def read_live_retry(tries=3):
    last = None
    for _ in range(tries):
        try:
            return pool.load_auth(LIVE)
        except Exception as e:
            last = e
            time.sleep(0.6)
    raise last


def slot_names():
    if not SLOTS.exists():
        return []
    names = [p.name for p in SLOTS.iterdir() if p.is_dir()]
    return sorted(names, key=lambda n: (0, int(n)) if n.isdigit() else (1, n))


def pick_target_slot(selected):
    if selected:
        if not (SLOTS / selected / 'auth.json').exists():
            return selected
    existing = set(slot_names())
    nums = sorted(int(n) for n in existing if n.isdigit())
    for i in range(1, (nums[-1] if nums else 0) + 2):
        if str(i) not in existing:
            return str(i)
    return str(len(existing) + 1)


def load_meta():
    try:
        return json.loads(META.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_meta(meta):
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')


# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Codex 号池控制台')
        self.geometry('1240x640')
        self.minsize(1040, 520)
        self.q = queue.Queue()
        self.quota = {}          # slot -> ('23%', '41%') 或错误串
        self.reached = set()     # 当前窗口已打满的槽位
        self.busy = False
        self._build()
        self.refresh_table()
        self.show_live_summary()
        self.after(150, self._poll)
        self._emit('就绪。流程: [add] 隔离当前号 → 启动 Codex 登录新号 → [get] 收取入池。')

    # -- 界面 --
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill='x')
        self.btn_add = ttk.Button(top, text='add  登录新号(隔离当前)',
                                  command=self.on_add, width=24)
        self.btn_get = ttk.Button(top, text='get  收取刚登录的号',
                                  command=self.on_get, width=24)
        self.btn_status = ttk.Button(top, text='Status  查额度(5h/周)+订阅',
                                     command=self.on_status, width=24)
        self.btn_use = ttk.Button(top, text='use  切换到该账号',
                                  command=self.on_use, width=20)
        self.btn_rm = ttk.Button(top, text='rm  删除(入回收站)', command=self.on_rm,
                                 width=20)
        self.btn_trash = ttk.Button(top, text='trash  回收站', command=self.on_trash,
                                    width=14)
        self.btn_refresh = ttk.Button(top, text='Refresh  刷新', command=self.on_refresh,
                                      width=14)
        self.btn_proxy = ttk.Button(top, text='proxy  代理', command=self.on_proxy,
                                    width=12)
        for i, b in enumerate((self.btn_add, self.btn_get, self.btn_status,
                               self.btn_use)):
            b.grid(row=0, column=i, padx=(0, 8), pady=(0, 4))
        for i, b in enumerate((self.btn_rm, self.btn_trash, self.btn_refresh,
                               self.btn_proxy)):
            b.grid(row=1, column=i, padx=(0, 8))
        top.columnconfigure(4, weight=1)

        self.lbl_live = ttk.Label(top, text='', foreground='#0a5')
        self.lbl_live.grid(row=2, column=0, columnspan=5, sticky='w', pady=(8, 0))
        self.lbl_pending = ttk.Label(top, text='', foreground='#b60')
        self.lbl_pending.grid(row=3, column=0, columnspan=5, sticky='w')

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill='x')
        ttk.Label(mid, text='操作日志:').pack(anchor='w')
        self.txt = tk.Text(mid, height=7, wrap='word', state='disabled',
                           background='#fafafa')
        self.txt.pack(fill='x')

        ttk.Label(self, text='账号台(双击"密码/附加"列直接编辑; 选中行点 use 切换, Delete 键删除):',
                  padding=(10, 6, 0, 2)).pack(anchor='w')
        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill='both', expand=True)
        cols = ('slot', 'email', 'pwd', 'note', 'plan', 'until', 'q5', 'q5r', 'qw', 'qwr', 'card', 'state')
        self.tree = ttk.Treeview(bottom, columns=cols, show='headings')
        heads = ('槽位', '邮箱', '密码', '附加', '套餐', '订阅至',
                 '5h额度', '5h重置于', '周额度', '周重置于', '重置卡', '凭证状态')
        widths = (44, 190, 76, 130, 50, 86, 58, 92, 58, 92, 56, 126)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor='w',
                             stretch=(c in ('email', 'state')))
        self._pwd_col = f'#{cols.index("pwd") + 1}'
        self._note_col = f'#{cols.index("note") + 1}'
        sb = ttk.Scrollbar(bottom, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure('full', foreground='#c00')
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self.on_select)
        self.tree.bind('<Double-1>', self.on_row_double)
        self.tree.bind('<Delete>', lambda _e: self.on_rm())

    # -- 线程通信 --
    def _emit(self, msg, kind='log'):
        self.q.put((kind, msg))

    def _poll(self):
        try:
            while True:
                kind, msg = self.q.get_nowait()
                if kind == 'log':
                    self.txt.configure(state='normal')
                    self.txt.insert('end', log(msg))
                    self.txt.see('end')
                    self.txt.configure(state='disabled')
                elif kind == 'refresh':
                    self.refresh_table()
                    self.show_live_summary()
                elif kind == 'done':
                    self.set_busy(False)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def set_busy(self, flag):
        self.busy = flag
        state = 'disabled' if flag else 'normal'
        for b in (self.btn_add, self.btn_get, self.btn_status,
                  self.btn_use, self.btn_rm, self.btn_trash,
                  self.btn_refresh, self.btn_proxy):
            b.configure(state=state)

    def selected_slot(self):
        sel = self.tree.selection()
        return self.tree.item(sel[0], 'values')[0] if sel else None

    # -- 表格 --
    def refresh_table(self):
        meta = load_meta()
        self.tree.delete(*self.tree.get_children())
        for name in slot_names():
            f = SLOTS / name / 'auth.json'
            if not f.exists():
                self.tree.insert('', 'end', iid=name,
                                 values=(name, '(空槽位)', '', '', '', '', '', ''))
                continue
            try:
                auth = pool.load_auth(f)
                email, plan, until, exp = pool.describe(auth)
                now = time.time()
                if exp_epoch(auth) == 0:
                    state = '损坏'
                elif exp_epoch(auth) < now:
                    state = 'token过期(点Status可刷新)'
                else:
                    state = f'有效至 {exp[5:16]}'
                q = self.quota.get(name)
                if isinstance(q, dict):
                    p, s = q.get('p', ''), q.get('s', '')
                    p5r, swr = fmt_reset(q.get('pr')), fmt_reset(q.get('sr'))
                    card = str(q['cards']) if q.get('cards') is not None and not q.get('err') else ''
                    if q.get('err'):
                        p = q['err'][:12]
                else:
                    p, s, p5r, swr, card = '', '', '', '', ''
                m = meta.get(name, {})
                pwd, note = m.get('password', ''), m.get('note', '')
                self.tree.insert('', 'end', iid=name, tags=('full',) if name in self.reached else (), values=(
                    name, email, pwd, note, plan, until, p, p5r, s, swr, card, state))
            except Exception as e:
                self.tree.insert('', 'end', iid=name,
                                 values=(name, '(文件损坏)', '', '', '', '', '', str(e)[:30]))

    def show_live_summary(self):
        if LIVE.exists():
            try:
                auth = pool.load_auth(LIVE)
                email, plan, until, exp = pool.describe(auth)
                slot = pool.find_slot_by_account(pool.account_id(auth))
                if slot and pool.read_current() != slot:
                    CURRENT.write_text(slot, encoding='utf-8')  # 标记自愈
                if slot:
                    self.lbl_live.configure(
                        text=f'当前登录: {email} ({plan}, 订阅至 {until})  [槽位 {slot}]',
                        foreground='#0a5')
                else:
                    self.lbl_live.configure(
                        text=f'当前登录: {email} ({plan}, 订阅至 {until})  '
                             f'[未入池 → 建议点 get 收取]',
                        foreground='#b60')
            except Exception:
                self.lbl_live.configure(text='当前登录: auth.json 无法解析',
                                        foreground='#c00')
        else:
            self.lbl_live.configure(text='当前登录: 未登录(可启动 Codex 登录新号)',
                                    foreground='#b60')
        try:
            pend = json.loads(PENDING.read_text(encoding='utf-8'))
            self.lbl_pending.configure(text=f"待收取 → 槽位 [{pend.get('slot')}]"
                                            f" (于 {pend.get('time','')})", foreground='#b60')
        except Exception:
            self.lbl_pending.configure(text='', foreground='#b60')

    def on_select(self, _evt=None):
        name = self.selected_slot()
        if not name:
            return
        f = SLOTS / name / 'auth.json'
        if not f.exists():
            self._emit(f'槽位 [{name}] 是空槽位, add 时会自动用作目标。')
            return
        try:
            email, plan, until, exp = pool.describe(pool.load_auth(f))
            meta = load_meta().get(name, {})
            msg = (f'选中槽位 [{name}]: {email} | {plan} | 订阅 {until} | '
                   f'token 到期 {exp} | 密码: {meta.get("password", "(未填)")}'
                   f' | 附加: {meta.get("note", "(无)")}')
            q = self.quota.get(name)
            if isinstance(q, dict) and not q.get('err'):
                msg += (f' | 5h重置于 {fmt_reset(q.get("pr")) or "?"}'
                        f' | 周重置于 {fmt_reset(q.get("sr")) or "?"}'
                        + (f' | 重置卡 {q.get("cards")}张(5h/周通用)'
                           if q.get('cards') is not None else ''))
            self._emit(msg)
        except Exception:
            self._emit(f'槽位 [{name}] 凭证文件损坏。')

    def on_row_double(self, event):
        """双击 密码列/附加列 直接编辑对应字段; 其他列无动作(切换用 use)。"""
        if self.tree.identify('region', event.x, event.y) != 'cell':
            return
        col = self.tree.identify_column(event.x)
        if not self.selected_slot():
            return
        if col == self._pwd_col:
            self.on_edit_pwd()
        elif col == self._note_col:
            self.on_edit_note()

    def on_edit_pwd(self, _evt=None):
        name = self.selected_slot()
        if not name:
            return
        meta = load_meta()
        cur = meta.get(name, {}).get('password', '')
        pwd = simpledialog.askstring(
            '设置密码', f'槽位 [{name}] 的账号密码(留空清除):', initialvalue=cur,
            parent=self)
        if pwd is None:
            return
        meta.setdefault(name, {})['password'] = pwd.strip()
        if not pwd.strip():
            meta[name].pop('password', None)
        save_meta(meta)
        self.refresh_table()
        self._emit(f'槽位 [{name}] 密码已{"清除" if not pwd.strip() else "保存"}(明文存在 pool.json)。')

    def on_edit_note(self):
        name = self.selected_slot()
        if not name:
            return
        meta = load_meta()
        cur = meta.get(name, {}).get('note', '')
        note = simpledialog.askstring(
            '附加信息', f'槽位 [{name}] 的自定义备注\n(卖家/购买日期/用途等, 留空清除):',
            initialvalue=cur, parent=self)
        if note is None:
            return
        meta.setdefault(name, {})['note'] = note.strip()
        if not note.strip():
            meta[name].pop('note', None)
        save_meta(meta)
        self.refresh_table()
        self._emit(f'槽位 [{name}] 附加信息已{"清除" if not note.strip() else "保存"}。')

    # -- 操作: 后台线程执行, 结果经队列回主线程 --
    def run_bg(self, fn):
        if self.busy:
            messagebox.showinfo('忙', '有操作进行中, 请稍候。', parent=self)
            return
        self.set_busy(True)

        def wrap():
            try:
                fn()
            except Exception as e:
                log('未捕获异常: ' + traceback.format_exc())
                self._emit(f'操作失败: {http_err_short(e) if isinstance(e, urllib.error.URLError) else e}')
            finally:
                self._emit(None, 'done')

        threading.Thread(target=wrap, daemon=True).start()

    def on_add(self):
        target = pick_target_slot(self.selected_slot())
        if not messagebox.askyesno(
                'add', f'将:\n1. 强制退出 Codex Desktop\n'
                       f'2. 把当前登录凭证隔离保存(槽位不变)\n'
                       f'3. 等你在 Codex 里登录新号 → 槽位 [{target}]\n\n'
                        f'继续?', parent=self):
            return

        def work():
            cd = pool.cooldown_remaining()
            if cd > 0:
                self._emit(f'冷却中: 距上次切换不足 {pool.COOLDOWN_SEC} 秒, 请等 {int(cd) + 1} 秒(防连切)。')
                return
            if not pool.acquire_lock():
                self._emit('另一个切换操作正在进行(GUI 或 CLI), 已中止。')
                return
            try:
                self._emit('正在退出 Codex Desktop...')
                if not kill_codex():
                    self._emit('Codex 进程未能全部退出, 已中止。' + kill_err_hint())
                    return
                if LIVE.exists():
                    pool.sync_live_to_current_slot()
                    bak = pool.backup_file(LIVE, f'live-isolated-slot{pool.read_current() or "x"}')
                    if not bak:
                        self._emit('备份失败, 为安全起见已中止(未动 auth.json)。')
                        return
                    LIVE.unlink(missing_ok=True)
                    self._emit(f'当前凭证已隔离, 备份: {bak.name}')
                PENDING.write_text(json.dumps(
                    {'slot': target, 'time': datetime.datetime.now().strftime('%m-%d %H:%M')},
                    ensure_ascii=False), encoding='utf-8')
                pool.note_switch()
                self._emit(f'现在启动 Codex Desktop 登录新号, 登录完成后回来点 [get] → 槽位 [{target}]。')
                self._emit(None, 'refresh')
            finally:
                pool.release_lock()

        self.run_bg(work)

    def on_get(self):
        def work():
            try:
                pend = json.loads(PENDING.read_text(encoding='utf-8'))
                want = pend.get('slot')
            except Exception:
                pend = None
                want = self.selected_slot()
            if not want:
                want = pick_target_slot(None)
                self._emit(f'未指定槽位, 自动选择空槽位 [{want}]。')
            try:
                auth = read_live_retry()
            except Exception:
                self._emit('auth.json 不存在或不可读 — 新号登录了吗?')
                return
            new_acc = pool.account_id(auth)
            email, plan, until, _ = pool.describe(auth)
            # 幂等: 一号一槽 — 已入池的号刷新原槽位, 不新建重复槽位
            existing = pool.find_slot_by_account(new_acc)
            if existing and existing != want:
                self._emit(f'{email} 已在槽位 [{existing}], 本次将刷新该槽位而不是新建 [{want}]。')
                target = existing
            else:
                target = want
                # 防护: 目标槽位住着别的号时不覆盖, 自动改投空槽位
                tf = SLOTS / want / 'auth.json'
                if tf.exists():
                    try:
                        if pool.account_id(pool.load_auth(tf)) != new_acc:
                            free = pick_target_slot(None)
                            self._emit(f'槽位 [{want}] 已有其他账号, 不覆盖, 自动改收空槽位 [{free}]。')
                            target = free
                    except Exception:
                        pass
            if not pool.acquire_lock():
                self._emit('另一个切换操作正在进行(GUI 或 CLI), 已中止。')
                return
            try:
                sd = SLOTS / target
                sd.mkdir(parents=True, exist_ok=True)
                if (sd / 'auth.json').exists():
                    pool.backup_file(sd / 'auth.json', f'slot{target}-before-refresh')
                save_auth(sd / 'auth.json', auth)
                CURRENT.write_text(target, encoding='utf-8')
                if pend:
                    PENDING.unlink(missing_ok=True)
                meta = load_meta()
                meta.setdefault(target, {})['email'] = email
                save_meta(meta)
                action = '刷新' if existing else '收取'
                self._emit(f'已{action} → 槽位 [{target}]: {email} ({plan}, 订阅至 {until})。'
                           f'当前 Desktop 登录就是它, 无需重启。')
                self._emit(None, 'refresh')
            finally:
                pool.release_lock()

        self.run_bg(work)

    def on_use(self):
        name = self.selected_slot()
        if not name or not (SLOTS / name / 'auth.json').exists():
            messagebox.showinfo('use', '请先在下方选中一个有效槽位。', parent=self)
            return
        try:
            live_acc = pool.account_id(pool.load_auth(LIVE)) if LIVE.exists() else None
        except Exception:
            live_acc = None
        uncaptured = bool(live_acc) and not pool.find_slot_by_account(live_acc)
        msg = f'切换到槽位 [{name}]:\n将退出 Codex → 保存当前号 → 换入该号。'
        if uncaptured:
            msg += ('\n\n⚠ 当前登录的号还不在池子里!\n'
                    '[是]   = 先自动收进池再切换(推荐)\n'
                    '[否]   = 直接切换(当前号只留在 backups/ 备份里)\n'
                    '[取消] = 什么都不做')
        ans = messagebox.askyesnocancel('use', msg, parent=self)
        if ans is None:
            return
        if not uncaptured and not ans:
            return

        def work():
            cd = pool.cooldown_remaining()
            if cd > 0:
                self._emit(f'冷却中: 距上次切换不足 {pool.COOLDOWN_SEC} 秒, 请等 {int(cd) + 1} 秒(防连切)。')
                return
            if not pool.acquire_lock():
                self._emit('另一个切换操作正在进行(GUI 或 CLI), 已中止。')
                return
            try:
                self._emit('正在退出 Codex Desktop...')
                if not kill_codex():
                    self._emit('Codex 进程未能全部退出, 已中止。' + kill_err_hint())
                    return
                # 防护: 切走前, 把未入池的当前号先收进池
                if uncaptured and ans:
                    try:
                        pend_slot = None
                        try:
                            pend_slot = json.loads(PENDING.read_text(encoding='utf-8')).get('slot')
                        except Exception:
                            pass
                        cap_slot = pend_slot or pick_target_slot(None)
                        live_auth = read_live_retry()
                        sd = SLOTS / cap_slot
                        sd.mkdir(parents=True, exist_ok=True)
                        if (sd / 'auth.json').exists():
                            pool.backup_file(sd / 'auth.json', f'slot{cap_slot}-before-capture')
                        save_auth(sd / 'auth.json', live_auth)
                        CURRENT.write_text(cap_slot, encoding='utf-8')
                        PENDING.unlink(missing_ok=True)
                        meta = load_meta()
                        meta.setdefault(cap_slot, {})['email'] = pool.describe(live_auth)[0]
                        save_meta(meta)
                        self._emit(f'防护: 当前号已先收进槽位 [{cap_slot}]。')
                    except Exception as e:
                        self._emit(f'自动收取失败({e}), 已中止切换 — 请先手动点 get。')
                        return
                target_auth = pool.load_auth(SLOTS / name / 'auth.json')
                target_acc = pool.account_id(target_auth)
                if LIVE.exists():
                    try:
                        if pool.account_id(pool.load_auth(LIVE)) == target_acc:
                            CURRENT.write_text(name, encoding='utf-8')
                            self._emit(f'槽位 [{name}] 已是当前登录, 无需切换(幂等)。')
                            return
                    except Exception:
                        pass
                    pool.backup_file(LIVE, f'live-before-use{name}')
                    pool.sync_live_to_current_slot()
                pool.write_auth_atomic(target_auth, LIVE)
                if not pool.verify_live(target_acc):
                    self._emit('校验失败: live auth.json 与目标账号不符, 已中止'
                               '(改动前的文件已备份在 backups/)。')
                    return
                CURRENT.write_text(name, encoding='utf-8')
                pool.note_switch()
                self._emit(f'已切换到槽位 [{name}]。重新打开 Codex Desktop 生效。')
                self._emit(None, 'refresh')
            finally:
                pool.release_lock()

        self.run_bg(work)

    def on_rm(self):
        name = self.selected_slot()
        if not name or not (SLOTS / name).exists():
            messagebox.showinfo('rm', '请先在下方选中要删除的槽位。', parent=self)
            return
        try:
            live_same = LIVE.exists() and pool.account_id(pool.load_auth(LIVE)) == \
                pool.account_id(pool.load_auth(SLOTS / name / 'auth.json'))
        except Exception:
            live_same = False
        if live_same:
            messagebox.showwarning(
                'rm  受保护',
                f'槽位 [{name}] 是当前正在登录的号, 受保护不可删除。\n\n'
                f'先 use 切换到其他槽位, 或点 add 隔离当前登录后再删。',
                parent=self)
            return
        if not messagebox.askyesno(
                'rm  删除', f'把槽位 [{name}] 移入回收站?\n(回收站可随时恢复, 不直接删除)',
                parent=self):
            return

        def work():
            if not pool.acquire_lock():
                self._emit('另一个操作进行中, 已中止。')
                return
            try:
                sd = SLOTS / name
                meta_entry = load_meta().get(name, {})
                try:
                    (sd / 'meta.json').write_text(
                        json.dumps(meta_entry, ensure_ascii=False), encoding='utf-8')
                except Exception:
                    pass
                pool.TRASH.mkdir(parents=True, exist_ok=True)
                ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                shutil.move(str(sd), str(pool.TRASH / f'{name}__{ts}'))
                m = load_meta()
                m.pop(name, None)
                save_meta(m)
                self.quota.pop(name, None)
                self.reached.discard(name)
                self._emit(f'槽位 [{name}] 已移入回收站, 可点 [trash 回收站] 恢复。')
                self._emit(None, 'refresh')
            finally:
                pool.release_lock()

        self.run_bg(work)

    def on_refresh(self):
        self.refresh_table()
        self.show_live_summary()
        n = len([s for s in slot_names() if (SLOTS / s / 'auth.json').exists()])
        self._emit(f'已刷新: 池中 {n} 个账号。额度数字为上次查询结果, 联网更新请点 [Status]。')

    def on_trash(self):
        win = tk.Toplevel(self)
        win.title('回收站 (删除的账号)')
        win.geometry('580x330')
        frm = ttk.Frame(win, padding=8)
        frm.pack(fill='both', expand=True)
        lb = tk.Listbox(frm, activestyle='dotbox')
        sb = ttk.Scrollbar(frm, orient='vertical', command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        btns = ttk.Frame(win, padding=(8, 4, 8, 8))
        btns.pack(fill='x')
        items = []

        def load():
            nonlocal items
            items = []
            lb.delete(0, 'end')
            if not pool.TRASH.exists():
                return
            for d in sorted(pool.TRASH.iterdir(), reverse=True):
                if not d.is_dir():
                    continue
                email = '?'
                try:
                    email = pool.describe(pool.load_auth(d / 'auth.json'))[0]
                except Exception:
                    pass
                items.append(d)
                lb.insert('end', f'{d.name}   |   {email}')
            if not items:
                lb.insert('end', '(回收站为空)')

        def restore():
            sel = lb.curselection()
            if not sel or not items:
                return
            d = items[sel[0]]
            name = d.name.split('__')[0]
            target = name if not (SLOTS / name).exists() else pick_target_slot(None)
            if not pool.acquire_lock():
                messagebox.showwarning('回收站', '有切换操作进行中, 稍后再试。', parent=win)
                return
            try:
                sd = SLOTS / target
                sd.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d / 'auth.json'), str(sd / 'auth.json'))
                try:
                    m = json.loads((d / 'meta.json').read_text(encoding='utf-8'))
                    meta = load_meta()
                    meta.setdefault(target, {}).update(m)
                    save_meta(meta)
                except Exception:
                    pass
                try:
                    d.rmdir()
                except OSError:
                    pass
                self.quota.pop(target, None)
                self.reached.discard(target)
            finally:
                pool.release_lock()
            self._emit(f'已从回收站恢复 → 槽位 [{target}]。')
            self._emit(None, 'refresh')
            load()

        def purge():
            if not messagebox.askyesno('回收站', '彻底清空回收站? 此操作不可恢复!',
                                       parent=win):
                return
            if pool.TRASH.exists():
                for d in list(pool.TRASH.iterdir()):
                    if d.is_dir():
                        shutil.rmtree(d, ignore_errors=True)
                    else:
                        d.unlink(missing_ok=True)
            self._emit('回收站已清空(彻底删除)。')
            load()

        ttk.Button(btns, text='恢复到池中', command=restore).pack(side='left', padx=(0, 6))
        ttk.Button(btns, text='清空回收站', command=purge).pack(side='left', padx=(0, 6))
        ttk.Button(btns, text='关闭', command=win.destroy).pack(side='right')
        load()

    def on_proxy(self):
        try:
            cur = json.loads(PROXY_FILE.read_text(encoding='utf-8')).get('proxy', 'auto')
        except Exception:
            cur = 'auto'
        val = simpledialog.askstring(
            '代理设置',
            '代理地址, 例如 http://127.0.0.1:7897\nauto=自动探测   none=强制直连',
            initialvalue=cur, parent=self)
        if val is None:
            return
        val = val.strip() or 'auto'
        PROXY_FILE.write_text(json.dumps({'proxy': val}, ensure_ascii=False),
                              encoding='utf-8')
        global _opener
        _opener = None
        found = detect_proxy()
        self._emit(f'代理设置: {val} → 实际使用: {found or "直连"}')

    def on_status(self):
        names = slot_names()
        targets = [n for n in names if (SLOTS / n / 'auth.json').exists()]
        if not targets:
            self._emit('池子里还没有账号, 先 add/get。')
            return

        def work():
            proxy = detect_proxy()
            self._emit('出口代理: ' + (f'{proxy} (自动探测)' if proxy else
                                      '直连(未发现本机代理端口, 被拦时开系统代理/TUN或在proxy里手填)'))
            self._emit(f'查询 {len(targets)} 个账号的额度(5h/周)...')
            for name in targets:
                f = SLOTS / name / 'auth.json'
                try:
                    auth = pool.load_auth(f)
                    email, plan, until, _ = pool.describe(auth)
                    if exp_epoch(auth) < time.time() + 120:
                        self._emit(f'槽位 [{name}] {email}: token 过期, 正在刷新...')
                        auth = refresh_auth(auth)
                        save_auth(f, auth)
                        self._emit(f'槽位 [{name}]: token 已刷新。')
                    u = fetch_usage(auth)
                    p_txt = f"{u['primary']:.0f}%" if u['primary'] is not None else '无'
                    s_txt = f"{u['secondary']:.0f}%" if u['secondary'] is not None else '无'
                    self.quota[name] = {'p': p_txt, 's': s_txt,
                                        'pr': u['primary_reset_at'],
                                        'sr': u['secondary_reset_at'],
                                        'cards': u.get('cards')}
                    if u['reached']:
                        self.reached.add(name)
                    else:
                        self.reached.discard(name)
                    full = '  <-- 当前窗口已打满' if u['reached'] else ''
                    cards = f" | 重置卡 {u.get('cards')}张(通用)" if u.get('cards') is not None else ''
                    p_part = (f"5h {p_txt} (重置 {fmt_dur(u['primary_reset'])})"
                              if u['primary'] is not None else '5h 无此窗口(仅周额度)')
                    s_part = (f"周 {s_txt} (重置 {fmt_dur(u['secondary_reset'])})"
                              if u['secondary'] is not None else '周 无此窗口')
                    self._emit(f'槽位 [{name}] {email}: {p_part} | {s_part}{cards} | 订阅至 {until}{full}')
                except Exception as e:
                    self.quota[name] = {'err': http_err_short(e)}
                    self._emit(f'槽位 [{name}] 查询失败: {http_err_short(e)}')
                self._emit(None, 'refresh')
            self._emit('额度查询完成。')

        self.run_bg(work)


def main():
    if '--smoke' in sys.argv:
        app = App()
        app.after(800, app.destroy)
        app.mainloop()
        print('SMOKE_OK')
        return
    try:
        App().mainloop()
    except Exception:
        log('GUI 崩溃: ' + traceback.format_exc())
        raise


if __name__ == '__main__':
    main()
