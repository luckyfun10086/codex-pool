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
from tkinter import filedialog, messagebox, simpledialog, ttk

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

# 复用核心脚本里的槽位/额度/代理逻辑, 单一数据源 (加载位置见文件头 _core)
_spec = importlib.util.spec_from_file_location('poolcore', _core)
pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pool)

# 额度查询/token刷新/代理探测已下沉到核心库, 这里引用保持调用点不变
refresh_auth = pool.refresh_auth
fetch_usage = pool.fetch_usage
http_err_short = pool.http_err_short
detect_proxy = pool.detect_proxy
get_opener = pool.get_opener
PROXY_FILE = pool.PROXY_FILE


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


def fmt_dur(sec):
    if sec is None:
        return '?'
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return T(f'{d}天{h}h', f'{d}d{h}h')
    if h:
        return f'{h}h{m:02d}m'
    return f'{m}m'


_WDAY = {'zh': ('周一', '周二', '周三', '周四', '周五', '周六', '周日'),
         'en': ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')}


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
        return T(f'今天 {dt:%H:%M}', f'Today {dt:%H:%M}')
    if 0 < (dt.date() - now.date()).days <= 7:
        return f'{_WDAY[_lang["v"]][dt.weekday()]} {dt:%H:%M}'
    return f'{dt:%m-%d %H:%M}'




# ---------- 进程与槽位辅助 ----------

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
KILL_ERROR = ''

# Codex Desktop(MSIX 版)的宿主进程是 ChatGPT.exe, 它负责孵化/复活 codex.exe,
# 只杀 codex.exe 会被宿主秒复活, 必须两个一起杀。
PROC_NAMES = ('ChatGPT.exe', 'codex.exe')


def _taskkill(*extra):
    args = ['taskkill']
    for n in PROC_NAMES:
        args += ['/IM', n]
    try:
        return subprocess.run([*args, *extra], capture_output=True,
                              creationflags=CREATE_NO_WINDOW, timeout=15)
    except subprocess.TimeoutExpired:
        return None  # 系统命令挂起, 由调用方中止


def _codex_pids():
    """列出 Codex 相关进程 {名字小写: [pid...]}; 空字典=全部退出; None=查询挂起。"""
    try:
        r = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'],
                           capture_output=True, creationflags=CREATE_NO_WINDOW,
                           timeout=15)
    except subprocess.TimeoutExpired:
        return None
    want = {n.lower() for n in PROC_NAMES}
    found = {}
    for line in r.stdout.decode('gbk', errors='replace').splitlines():
        if not line.startswith('"'):
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0].lower() in want and len(parts) > 1 and parts[1].isdigit():
            found.setdefault(parts[0].lower(), []).append(int(parts[1]))
    return found


def kill_err_hint():
    e = KILL_ERROR or ''
    if '拒绝访问' in e or 'denied' in e.lower() or 'access' in e.lower():
        return T('(taskkill 被拒绝: Codex 可能以管理员身份在运行。'
                 '请手动退出 Codex 后再点切换, 或以管理员身份运行本 GUI。)',
                 '(taskkill denied: Codex may be running elevated. '
                 'Quit Codex manually before switching, or run this GUI as administrator.)')
    if e:
        return T(f'(taskkill: {e[:80]})', f'(taskkill: {e[:80]})')
    return ''


def kill_codex():
    """退出 Codex Desktop(宿主+子进程): 先优雅关闭(2s), 再强杀补刀(约10s)。"""
    global KILL_ERROR
    KILL_ERROR = ''
    _taskkill()  # 不带 /F: 发送关闭消息, 让它优雅退出
    for _ in range(4):
        time.sleep(0.5)
        pids = _codex_pids()
        if pids is None:
            KILL_ERROR = 'tasklist 无响应(系统进程枚举挂起, 建议重启电脑) / tasklist hung (reboot recommended)'
            return False
        if not pids:
            return True
    for i in range(9):
        r = _taskkill('/F', '/T')
        if r is None:
            KILL_ERROR = 'taskkill 无响应(系统进程枚举挂起, 建议重启电脑) / taskkill hung (reboot recommended)'
            return False
        if i == 0:
            KILL_ERROR = (r.stderr or b'').decode('gbk', errors='replace').strip()
        time.sleep(1.0)
        pids = _codex_pids()
        if pids is None:
            KILL_ERROR = 'tasklist 无响应(系统进程枚举挂起, 建议重启电脑) / tasklist hung (reboot recommended)'
            return False
        if not pids:
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


# ---------- 自动切换 (思路参考 CodexPool: 动态间隔 + least_used + 阈值触发) ----------

AUTO_DEFAULTS = {'mode': 'off', 'threshold': 100, 'max_per_hour': 3, 'relaunch': True}


def load_auto_cfg():
    cfg = dict(AUTO_DEFAULTS)
    try:
        cfg.update((load_meta().get('auto') or {}))
    except Exception:
        pass
    cfg['threshold'] = max(80, min(100, int(cfg['threshold'])))
    cfg['max_per_hour'] = max(1, min(6, int(cfg['max_per_hour'])))
    cfg['relaunch'] = bool(cfg['relaunch'])
    cfg['mode'] = cfg['mode'] if cfg['mode'] in ('off', 'shadow', 'exec') else 'off'
    return cfg


def save_auto_cfg(cfg):
    meta = load_meta()
    meta['auto'] = cfg
    save_meta(meta)


def load_vps_cfg():
    try:
        v = load_meta().get('vps') or {}
    except Exception:
        v = {}
    return {'url': (v.get('url') or '').strip().rstrip('/'),
            'key': (v.get('key') or '').strip()}


def save_vps_cfg(url, key):
    meta = load_meta()
    meta['vps'] = {'url': url.strip().rstrip('/'), 'key': key.strip()}
    save_meta(meta)


def auto_interval_sec(primary):
    """动态检查间隔: 用量越高查得越频 (CodexPool 的三档策略)。"""
    if primary is None:
        return 30 * 60
    if primary >= 80:
        return 5 * 60
    if primary >= 50:
        return 10 * 60
    return 30 * 60


def decide_auto_switch(usage_map, current, threshold=100):
    """纯决策: usage_map = {槽位: fetch_usage结果 或 None}。

    触发: 当前号 limit_reached 或 5h 用量 >= threshold(默认100=真打满才切)。
    选号: least_used —— 周额度优先(真正的稀缺资源), 5h 用量次之。
    返回 (action, target, reason), action ∈ switch / wait / blocked_all。
    """
    cur = usage_map.get(current)
    if cur is None:
        return 'wait', None, T('当前号用量查询失败, 本轮跳过',
                               'usage query failed for the current account; skipping this round')
    pri = cur.get('primary')
    blocked = bool(cur.get('reached')) or (pri is not None and pri >= threshold)
    if not blocked:
        p_txt = f'{pri:.0f}%' if pri is not None else T('无5h窗口', 'no 5h window')
        return 'wait', None, T(f'当前号 5h {p_txt}, 未达切换条件',
                               f'current account 5h {p_txt}; switch condition not met')
    best, best_key = None, None
    for name, u in sorted(usage_map.items()):
        if name == current or u is None or u.get('reached'):
            continue
        p = u.get('primary')
        if p is not None and p >= threshold:
            continue
        w = u.get('secondary')
        if w is not None and w >= 95:
            continue  # 周额度将触顶, 不作为候选
        key = (0 if w is not None else 1, w if w is not None else 0,
               p if p is not None else 0)
        if best_key is None or key < best_key:
            best, best_key = name, key
    if best is None:
        return 'blocked_all', None, T('全部账号均不可用(打满/凭证失效)',
                                      'no account available (maxed out / invalid credentials)')
    why = (T('已打满', 'maxed out') if cur.get('reached')
           else T(f'达{threshold}%阈值', f'hit {threshold}% threshold'))
    return 'switch', best, T(f'当前号{why}', f'current account {why}')


def find_codex_appid():
    """查 MSIX Codex Desktop 的启动 AppID(版本无关), 供切换后自动重启。"""
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-StartApps | Where-Object {$_.Name -like '*Codex*'} "
             "| Select-Object -First 1 -ExpandProperty AppID"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
        return r.stdout.decode('utf-8', errors='replace').strip() or None
    except Exception:
        return None


# ---------- 界面语言 (中/英) ----------
_lang = {'v': 'zh'}


def T(zh, en):
    """内联双语: T('中文', 'English'), 由 _lang 决定取值。"""
    return zh if _lang['v'] == 'zh' else en


def load_lang():
    try:
        v = load_meta().get('lang') or 'zh'
        _lang['v'] = v if v in ('zh', 'en') else 'zh'
    except Exception:
        _lang['v'] = 'zh'


def save_lang(v):
    v = v if v in ('zh', 'en') else 'zh'
    meta = load_meta()
    meta['lang'] = v
    save_meta(meta)
    _lang['v'] = v


load_lang()


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
        self._auto_times = []       # 自动切换时间戳(每小时上限用)
        self._auto_next_check = 0.0
        self._auto_last_note = ''
        self._closing = False
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        threading.Thread(target=self._auto_loop, daemon=True).start()
        self.after(3000, lambda: self._vps_push())
        self._emit(T('就绪。流程: [add] 隔离当前号 → 启动 Codex 登录新号 → [get] 收取入池。',
                 'Ready. Flow: [add] isolate current login -> log a new account in Codex -> [get] capture.'))

    # -- 界面 --
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill='x')
        self.btn_add = ttk.Button(top, text=T('add  登录新号(隔离当前)',
                                              'add  Login new (isolate)'),
                                  command=self.on_add, )
        self.btn_get = ttk.Button(top, text=T('get  收取刚登录的号',
                                              'get  Capture new login'),
                                  command=self.on_get, )
        self.btn_status = ttk.Button(top, text=T('Status  查额度(5h/周)+订阅',
                                                 'Status  Quota (5h/wk)'),
                                     command=self.on_status, )
        self.btn_use = ttk.Button(top, text=T('use  切换到该账号',
                                              'use  Switch to it'),
                                  command=self.on_use, )
        self.btn_import = ttk.Button(top, text=T('import  导入JSON',
                                                 'import  Import JSON'),
                                     command=self.on_import, )
        self.btn_rm = ttk.Button(top, text=T('rm  删除(入回收站)',
                                             'rm  Delete (trash)'),
                                 command=self.on_rm, )
        self.btn_trash = ttk.Button(top, text=T('trash  回收站',
                                                'trash  Recycle bin'),
                                    command=self.on_trash, )
        self.btn_refresh = ttk.Button(top, text=T('Refresh  刷新',
                                                  'Refresh'),
                                      command=self.on_refresh, )
        self.btn_proxy = ttk.Button(top, text=T('proxy  代理', 'proxy'),
                                    command=self.on_proxy, )
        self.btn_auto = ttk.Button(top, text=T('auto  自动切换',
                                               'auto  Auto-switch'),
                                   command=self.on_auto, )
        self.btn_vps = ttk.Button(top, text=T('vps  监控', 'vps  Monitor'),
                                   command=self.on_vps)
        self.btn_lang = ttk.Button(top, text=T('中/EN', '中/EN'),
                                   command=self.on_lang, )
        for i, b in enumerate((self.btn_add, self.btn_get, self.btn_status,
                               self.btn_use)):
            b.grid(row=0, column=i, padx=(0, 8), pady=(0, 4))
        for i, b in enumerate((self.btn_import, self.btn_rm, self.btn_trash,
                               self.btn_refresh, self.btn_auto, self.btn_vps,
                               self.btn_proxy, self.btn_lang)):
            b.grid(row=1, column=i, padx=(0, 8))
        top.columnconfigure(4, weight=1)

        self.lbl_live = ttk.Label(top, text='', foreground='#0a5')
        self.lbl_live.grid(row=2, column=0, columnspan=5, sticky='w', pady=(8, 0))
        self.lbl_pending = ttk.Label(top, text='', foreground='#b60')
        self.lbl_pending.grid(row=3, column=0, columnspan=6, sticky='w')
        self.lbl_auto = ttk.Label(top, text='', foreground='#56c')
        self.lbl_auto.grid(row=4, column=0, columnspan=6, sticky='w')

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill='x')
        self.lbl_log_title = ttk.Label(mid, text=T('操作日志:', 'Activity log:'))
        self.lbl_log_title.pack(anchor='w')
        self.txt = tk.Text(mid, height=7, wrap='word', state='disabled',
                           background='#fafafa')
        self.txt.pack(fill='x')

        self.lbl_hint = ttk.Label(
            self, padding=(10, 6, 0, 2),
            text=T('账号台(双击"密码/附加"列直接编辑; 选中行点 use 切换, Delete 键删除):',
                   'Accounts (double-click Password/Note cells to edit; '
                   'select a row then use to switch, Del to remove):'))
        self.lbl_hint.pack(anchor='w')
        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill='both', expand=True)
        cols = ('slot', 'email', 'pwd', 'note', 'plan', 'until', 'q5', 'q5r', 'qw', 'qwr', 'card', 'state')
        self.tree = ttk.Treeview(bottom, columns=cols, show='headings')
        widths = (44, 190, 76, 130, 50, 86, 58, 92, 58, 92, 56, 126)
        for c, h, w in zip(cols, self._head_texts(), widths):
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

    @staticmethod
    def _head_texts():
        return (T('槽位', 'Slot'), T('邮箱', 'Email'), T('密码', 'Password'),
                T('附加', 'Note'), T('套餐', 'Plan'), T('订阅至', 'Sub until'),
                T('5h额度', '5h used'), T('5h重置于', '5h resets'),
                T('周额度', 'Wk used'), T('周重置于', 'Wk resets'),
                T('重置卡', 'Reset credits'), T('凭证状态', 'Token status'))

    def _apply_lang(self):
        """切换语言后重绘全部静态文本。"""
        self.title(T('Codex 号池控制台', 'Codex Pool Console'))
        self.btn_add.configure(text=T('add  登录新号(隔离当前)', 'add  Login new (isolate)'))
        self.btn_get.configure(text=T('get  收取刚登录的号', 'get  Capture new login'))
        self.btn_status.configure(text=T('Status  查额度(5h/周)+订阅', 'Status  Quota (5h/wk)'))
        self.btn_use.configure(text=T('use  切换到该账号', 'use  Switch to it'))
        self.btn_import.configure(text=T('import  导入JSON', 'import  Import JSON'))
        self.btn_rm.configure(text=T('rm  删除(入回收站)', 'rm  Delete (trash)'))
        self.btn_trash.configure(text=T('trash  回收站', 'trash  Recycle bin'))
        self.btn_refresh.configure(text=T('Refresh  刷新', 'Refresh'))
        self.btn_proxy.configure(text=T('proxy  代理', 'proxy'))
        self.btn_auto.configure(text=T('auto  自动切换', 'auto  Auto-switch'))
        self.btn_vps.configure(text=T('vps  监控', 'vps  Monitor'))
        cols = ('slot', 'email', 'pwd', 'note', 'plan', 'until', 'q5', 'q5r',
                'qw', 'qwr', 'card', 'state')
        for c, h in zip(cols, self._head_texts()):
            self.tree.heading(c, text=h)
        self.lbl_log_title.configure(text=T('操作日志:', 'Activity log:'))
        self.lbl_hint.configure(
            text=T('账号台(双击"密码/附加"列直接编辑; 选中行点 use 切换, Delete 键删除):',
                   'Accounts (double-click Password/Note cells to edit; '
                   'select a row then use to switch, Del to remove):'))
        self.refresh_table()
        self.show_live_summary()

    def on_lang(self):
        save_lang('en' if _lang['v'] == 'zh' else 'zh')
        self._apply_lang()
        self._emit(T(f'界面语言已切换为中文。', 'UI language switched to English.'))


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
                elif kind == 'auto_label':
                    self.lbl_auto.configure(text=msg or '')
                elif kind == 'busy':
                    self.set_busy(True)
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
                  self.btn_refresh, self.btn_import, self.btn_auto,
                  self.btn_vps, self.btn_proxy, self.btn_lang):
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
                                 values=(name, T('(空槽位)', '(empty)'), '', '', '', '', '', ''))
                continue
            try:
                auth = pool.load_auth(f)
                email, plan, until, exp = pool.describe(auth)
                now = time.time()
                if exp_epoch(auth) == 0:
                    state = T('损坏', 'corrupt')
                elif exp_epoch(auth) < now:
                    state = T('token过期(点Status可刷新)', 'token expired (click Status to refresh)')
                else:
                    state = T(f'有效至 {exp[5:16]}', f'valid till {exp[5:16]}')
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
                        text=T(f'当前登录: {email} ({plan}, 订阅至 {until})  [槽位 {slot}]',
                               f'Logged in: {email} ({plan}, sub until {until})  [slot {slot}]'),
                        foreground='#0a5')
                else:
                    self.lbl_live.configure(
                        text=T(f'当前登录: {email} ({plan}, 订阅至 {until})  [未入池 → 建议点 get 收取]',
                               f'Logged in: {email} ({plan}, sub until {until})  [NOT in pool -> press get]'),
                        foreground='#b60')
            except Exception:
                self.lbl_live.configure(text=T('当前登录: auth.json 无法解析', 'Logged in: auth.json unreadable'),
                                        foreground='#c00')
        else:
            self.lbl_live.configure(text=T('当前登录: 未登录(可启动 Codex 登录新号)',
                                          'Not logged in (start Codex to log a new account in)'),
                                    foreground='#b60')
        try:
            pend = json.loads(PENDING.read_text(encoding='utf-8'))
            self.lbl_pending.configure(text=T(f"待收取 → 槽位 [{pend.get('slot')}]"
                                            f" (于 {pend.get('time','')})",
                                            f"Pending capture -> slot [{pend.get('slot')}]"
                                            f" (at {pend.get('time','')})"), foreground='#b60')
        except Exception:
            self.lbl_pending.configure(text='', foreground='#b60')

    def on_select(self, _evt=None):
        name = self.selected_slot()
        if not name:
            return
        f = SLOTS / name / 'auth.json'
        if not f.exists():
            self._emit(T(f'槽位 [{name}] 是空槽位, add 时会自动用作目标。',
                       f'Slot [{name}] is empty; add will use it as the target.'))
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
            self._emit(T(f'槽位 [{name}] 凭证文件损坏。', f'Slot [{name}]: credential file corrupt.'))

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
            T('设置密码', 'Set password'),
            T(f'槽位 [{name}] 的账号密码(留空清除):', f'Password for slot [{name}] (empty to clear):'),
            initialvalue=cur,
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
                self._vps_push()
                self._emit(None, 'refresh')
            finally:
                pool.release_lock()

        self.run_bg(work)

    def on_import(self):
        """导入买来的 auth.json 凭证文件入池, 不触碰当前登录。"""
        path = filedialog.askopenfilename(
            title='选择要导入的 auth.json 凭证文件',
            filetypes=[('JSON 文件', '*.json'), ('所有文件', '*.*')],
            parent=self)
        if not path:
            return
        try:
            auth = pool.load_auth(pathlib.Path(path))
            email, plan, until, exp = pool.describe(auth)
        except Exception as e:
            messagebox.showerror('导入 JSON', f'不是有效的 Codex 凭证文件:\n{e}',
                                 parent=self)
            return
        acc = pool.account_id(auth)
        existing = pool.find_slot_by_account(acc) if acc else None
        if existing:
            target, action = existing, f'该账号已在槽位 [{existing}], 导入将刷新其凭证'
        else:
            sel = self.selected_slot()
            target = (sel if sel and not (SLOTS / sel / 'auth.json').exists()
                      else pick_target_slot(None))
            action = T(f'导入到空槽位 [{target}]', f'Import into empty slot [{target}]')
        if not messagebox.askyesno(
                T('导入 JSON', 'Import JSON'),
                T(f'{action}\n\n{email} ({plan}, 订阅至 {until})\n'
                  f'token 到期 {exp}\n当前登录不受影响。继续?',
                  f'{action}\n\n{email} ({plan}, sub until {until})\n'
                  f'token expires {exp}\nCurrent login is NOT affected. Continue?'),
                parent=self):
            return

        def work():
            if not pool.acquire_lock():
                self._emit('另一个操作进行中, 已中止。')
                return
            try:
                sd = SLOTS / target
                sd.mkdir(parents=True, exist_ok=True)
                if (sd / 'auth.json').exists():
                    pool.backup_file(sd / 'auth.json', f'slot{target}-before-import')
                save_auth(sd / 'auth.json', auth)
                meta = load_meta()
                meta.setdefault(target, {})['email'] = email
                save_meta(meta)
                self.quota.pop(target, None)
                self.reached.discard(target)
                verb = T('刷新', 'refreshed') if existing else T('导入', 'imported')
                self._emit(T(f'已{verb} → 槽位 [{target}]: {email} ({plan}, 订阅至 {until})。'
                             f'当前登录未变, 点 Status 可验证凭证活性与额度。',
                             f'{verb} -> slot [{target}]: {email} ({plan}, sub until {until}). '
                             f'Current login unchanged; press Status to verify the credential.'))
                self._vps_push()
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
                action = T('刷新', 'refreshed') if existing else T('收取', 'captured')
                self._emit(T(f'已{action} → 槽位 [{target}]: {email} ({plan}, 订阅至 {until})。'
                             f'当前 Desktop 登录就是它, 无需重启。',
                             f'{action} -> slot [{target}]: {email} ({plan}, sub until {until}). '
                             f'It is the current Desktop login; no restart needed.'))
                self._vps_push()
                self._emit(None, 'refresh')
            finally:
                pool.release_lock()

        self.run_bg(work)

    def on_use(self):
        name = self.selected_slot()
        if not name or not (SLOTS / name / 'auth.json').exists():
            messagebox.showinfo('use', T('请先在下方选中一个有效槽位。',
                                        'Select a valid slot in the table first.'), parent=self)
            return
        q = self.quota.get(name) or {}
        wk = str(q.get('s', '')).replace('%', '')
        if name in self.reached or (wk.isdigit() and int(wk) >= 95):
            if not messagebox.askyesno(
                    'use',
                    T('警告: 该账号当前已打满或周额度≥95% (红行), 切过去 Codex 大概率仍不可用!\n'
                      '(依据上次 Status 查询) 仍要切换吗?',
                      'Warning: this account is maxed out or weekly >= 95% (red row); '
                      'Codex will likely stay blocked.\n(based on the last Status query) '
                      'Switch anyway?'), parent=self):
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
            if uncaptured and ans:
                # 防护: 切走前, 把未入池的当前号先收进池
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
                    self._emit(T(f'防护: 当前号已先收进槽位 [{cap_slot}]。',
                                 f'Guard: current login captured into slot [{cap_slot}] first.'))
                except Exception as e:
                    self._emit(T(f'自动收取失败({e}), 已中止切换 — 请先手动点 get。',
                                 f'Auto-capture failed ({e}); switch aborted - press get manually.'))
                    return
            ok, msg = self._perform_switch(name, '手动')
            self._emit(msg)

        self.run_bg(work)

    def _on_close(self):
        self._closing = True
        self.destroy()

    # ---------- 自动切换 watcher ----------
    def _auto_note(self, text):
        self._emit(text)
        self._emit(text, 'auto_label')

    def _auto_loop(self):
        while not self._closing:
            try:
                cfg = load_auto_cfg()
                if cfg['mode'] != 'off' and not self.busy \
                        and time.time() >= self._auto_next_check:
                    self._auto_tick(cfg)
                time.sleep(15)
            except Exception:
                log('auto 循环异常: ' + traceback.format_exc())
                time.sleep(60)

    def _auto_tick(self, cfg):
        label = T('自动切换: ', 'Auto-switch: ') + (
            T('影子模式(只告警)', 'SHADOW (alert only)') if cfg['mode'] == 'shadow'
            else T('执行模式', 'EXEC'))
        try:
            if PENDING.exists():
                self._auto_note(label + T(' · 暂停: 存在待收标记(add 进行中)',
                                         ' · paused: pending capture marker exists (add in progress)'))
                self._auto_next_check = time.time() + 60
                return
            cur = pool.read_current()
            if not cur or not (SLOTS / cur / 'auth.json').exists():
                self._auto_note(label + T(' · 暂停: 当前槽位无效',
                                         ' · paused: current slot invalid'))
                self._auto_next_check = time.time() + 60
                return
            if pool.cooldown_remaining() > 0:
                self._auto_next_check = time.time() + pool.cooldown_remaining() + 5
                return
            if LIVE.exists():
                try:
                    live_acc = pool.account_id(pool.load_auth(LIVE))
                    if live_acc and not pool.find_slot_by_account(live_acc):
                        self._auto_note(label + T(' · 暂停: 当前登录未入池(先点 get)',
                                                 ' · paused: current login not in pool (press get)'))
                        self._auto_next_check = time.time() + 120
                        return
                except Exception:
                    return
            usage_map, cur_pri = {}, None
            for name in slot_names():
                f = SLOTS / name / 'auth.json'
                if not f.exists():
                    continue
                try:
                    auth = pool.load_auth(f)
                    if exp_epoch(auth) < time.time() + 120:
                        auth = refresh_auth(auth)
                        save_auth(f, auth)
                    u = fetch_usage(auth)
                    usage_map[name] = u
                    _na = T('无', 'n/a')
                    p_txt = f"{u['primary']:.0f}%" if u['primary'] is not None else _na
                    s_txt = f"{u['secondary']:.0f}%" if u['secondary'] is not None else _na
                    self.quota[name] = {'p': p_txt, 's': s_txt,
                                        'pr': u['primary_reset_at'],
                                        'sr': u['secondary_reset_at'],
                                        'cards': u.get('cards')}
                    if u['reached']:
                        self.reached.add(name)
                    else:
                        self.reached.discard(name)
                    if name == cur:
                        cur_pri = u['primary']
                except Exception:
                    usage_map[name] = None
            self._emit(None, 'refresh')
            self._auto_next_check = time.time() + auto_interval_sec(cur_pri)
            action, target, reason = decide_auto_switch(usage_map, cur, cfg['threshold'])
            if action == 'switch':
                now = time.time()
                self._auto_times = [t for t in self._auto_times if now - t < 3600]
                if len(self._auto_times) >= cfg['max_per_hour']:
                    self._auto_note(f'{label}' + T(f' · 已达每小时上限({cfg["max_per_hour"]}), '
                                                   f'本轮不切: {reason}',
                                                   f' · hourly cap reached ({cfg["max_per_hour"]}); '
                                                   f'not switching this round: {reason}'))
                    return
                if cfg['mode'] == 'shadow':
                    self._auto_note(f'{label}' + T(f' · [影子] {reason}, '
                                                   f'拟切换 [{cur}] → [{target}] (仅告警不执行)',
                                                   f' · [shadow] {reason}; would switch '
                                                   f'[{cur}] -> [{target}] (alert only)'))
                else:
                    self._auto_note(f'{label}' + T(f' · {reason}, 正在切换 [{cur}] → [{target}] ...',
                                                   f' · {reason}; switching [{cur}] -> [{target}] ...'))
                    self.busy = True
                    self._emit(None, 'busy')
                    try:
                        ok, msg = self._perform_switch(target, '自动')
                        self._auto_times.append(time.time())
                        self._auto_note(label + ' · ' + msg)
                        if ok and cfg['relaunch']:
                            self._relaunch_codex()
                    finally:
                        self.busy = False
                        self._emit(None, 'done')
            elif action == 'blocked_all':
                resets = [u.get('primary_reset_at') or u.get('secondary_reset_at')
                          for u in usage_map.values() if u]
                resets = [r for r in resets if r]
                when = fmt_reset(min(resets)) if resets else '?'
                self._auto_note(f'{label}' + T(f' · 全部账号不可用(打满/失效), 最早重置于 {when}, 等待中。',
                                               f' · all accounts unavailable (maxed/invalid); '
                                               f'earliest reset at {when}; waiting.'))
        except Exception:
            log('auto tick 异常: ' + traceback.format_exc())
            self._auto_next_check = time.time() + 300

    def _relaunch_codex(self):
        appid = find_codex_appid()
        if appid:
            try:
                subprocess.Popen(['explorer.exe', f'shell:AppsFolder\\{appid}'])
                self._emit(T('[自动] 已尝试重新启动 Codex Desktop。',
                             '[auto] Attempted to relaunch Codex Desktop.'))
                return
            except Exception:
                pass
        self._emit(T('[自动] 未能自动重启 Codex Desktop, 请手动打开。',
                     '[auto] Could not relaunch Codex Desktop automatically; open it manually.'))

    def on_auto(self):
        cfg = load_auto_cfg()
        win = tk.Toplevel(self)
        win.title(T('自动切换设置', 'Auto-switch settings'))
        win.geometry('440x330')
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill='both', expand=True)
        mode = tk.StringVar(value=cfg['mode'])
        ttk.Label(frm, text=T('模式:', 'Mode:')).pack(anchor='w')
        for val, txt in (('off', T('关闭 — 不自动切换', 'Off — no auto switching')),
                         ('shadow', T('影子 — 只检测告警, 不执行(建议先跑几天)',
                                      'Shadow — detect & alert only (recommended first)')),
                         ('exec', T('执行 — 打满自动切换, 并尝试重启 Codex',
                                    'Exec — auto-switch when maxed, relaunch Codex'))):
            ttk.Radiobutton(frm, value=val, text=txt, variable=mode).pack(anchor='w', padx=(10, 0))
        row = ttk.Frame(frm)
        row.pack(fill='x', pady=(10, 0))
        ttk.Label(row, text=T('触发阈值(5h用量%):', 'Trigger threshold (5h %):')).pack(side='left')
        thr = tk.IntVar(value=cfg['threshold'])
        ttk.Spinbox(row, from_=80, to=100, textvariable=thr, width=6).pack(side='left', padx=(6, 0))
        ttk.Label(row, text=T('每小时最多切换:', 'Max switches/hour:')).pack(side='left', padx=(12, 0))
        mph = tk.IntVar(value=cfg['max_per_hour'])
        ttk.Spinbox(row, from_=1, to=6, textvariable=mph, width=6).pack(side='left', padx=(6, 0))
        relaunch = tk.BooleanVar(value=cfg['relaunch'])
        ttk.Checkbutton(frm, text=T('切换后尝试自动重启 Codex Desktop',
                                    'Try to relaunch Codex Desktop after switching'),
                        variable=relaunch).pack(anchor='w', pady=(8, 0))
        ttk.Label(frm, foreground='#777', wraplength=400, text=(
            T('检查间隔自动分层: 5h<50% 每30分 / 50-80% 每10分 / >80% 每5分。\n'
              '选号策略: 周额度最低者优先(排除周>95%与失效号); 触发默认100%=真打满才切,\n'
              '调低可提前切换但可能打断进行中的任务。',
              'Check interval is tiered: 5h<50% every 30min / 50-80% every 10min / >80% every 5min.\n'
              'Selection: lowest weekly usage first (excludes weekly>95% and invalid).\n'
              'Default trigger 100% = switch only when truly maxed; lower it to switch '
              'earlier at the risk of interrupting work.'))
        ).pack(anchor='w', pady=(8, 0))

        def save():
            save_auto_cfg({'mode': mode.get(), 'threshold': int(thr.get()),
                           'max_per_hour': int(mph.get()),
                           'relaunch': bool(relaunch.get())})
            self._auto_next_check = 0.0
            m = {'off': T('已关闭', 'off'), 'shadow': T('影子模式(只告警)', 'shadow (alert only)'),
                 'exec': T('执行模式', 'exec')}[mode.get()]
            self._emit(T(f'自动切换设置已保存: {m}, 阈值 {thr.get()}%, 每小时上限 {mph.get()}。',
                         f'Auto-switch settings saved: {m}, threshold {thr.get()}%, '
                         f'max {mph.get()}/hour.'))
            self._auto_note(T(f'自动切换: {m}', f'Auto-switch: {m}') if mode.get() != 'off' else '')
            win.destroy()

        ttk.Button(frm, text=T('保存', 'Save'), command=save).pack(pady=(10, 0))

    def _perform_switch(self, name, source='manual'):
        """一次完整受保护切换: 冷却→锁→退Codex→备份→换入→校验。
        须在后台线程调用(on_use 与自动切换共用)。返回 (ok, msg)。"""
        cd = pool.cooldown_remaining()
        if cd > 0:
            return False, T(f'冷却中: 请等 {int(cd) + 1} 秒再切换(防连切)。',
                            f'Cooldown: wait {int(cd) + 1}s before switching.')
        if not pool.acquire_lock():
            return False, '另一个切换操作正在进行(GUI 或 CLI), 已中止。'
        try:
            self._emit('正在退出 Codex Desktop...')
            if not kill_codex():
                return False, 'Codex 进程未能全部退出, 已中止。' + kill_err_hint()
            target_auth = pool.load_auth(SLOTS / name / 'auth.json')
            target_acc = pool.account_id(target_auth)
            if LIVE.exists():
                try:
                    if pool.account_id(pool.load_auth(LIVE)) == target_acc:
                        CURRENT.write_text(name, encoding='utf-8')
                        return True, T(f'槽位 [{name}] 已是当前登录, 无需切换(幂等)。',
                                       f'Slot [{name}] is already the current login; no switch needed.')
                except Exception:
                    pass
                pool.backup_file(LIVE, f'live-before-use{name}')
                pool.sync_live_to_current_slot()
            pool.write_auth_atomic(target_auth, LIVE)
            if not pool.verify_live(target_acc):
                return False, T('校验失败: live auth.json 与目标账号不符, 已中止'
                                '(改动前的文件已备份在 backups/)。',
                                'Verification failed: live auth.json does not match the target '
                                'account; aborted (previous file backed up in backups/).')
            CURRENT.write_text(name, encoding='utf-8')
            pool.note_switch()
            self._vps_push()
            self._emit(None, 'refresh')
            return True, T(f'已切换到槽位 [{name}]({source})。重新打开 Codex Desktop 生效。',
                           f'Switched to slot [{name}] ({source}). Reopen Codex Desktop to take effect.')
        finally:
            pool.release_lock()

    def on_rm(self):
        name = self.selected_slot()
        if not name or not (SLOTS / name).exists():
            messagebox.showinfo('rm', T('请先在下方选中要删除的槽位。',
                                       'Select a slot to delete first.'), parent=self)
            return
        try:
            live_same = LIVE.exists() and pool.account_id(pool.load_auth(LIVE)) == \
                pool.account_id(pool.load_auth(SLOTS / name / 'auth.json'))
        except Exception:
            live_same = False
        if live_same:
            messagebox.showwarning(
                T('rm  受保护', 'rm  protected'),
                T(f'槽位 [{name}] 是当前正在登录的号, 受保护不可删除。\n\n'
                  f'先 use 切换到其他槽位, 或点 add 隔离当前登录后再删。',
                  f'Slot [{name}] is the currently logged-in account and is protected.\n\n'
                  f'use another slot first, or add to isolate the current login, then delete.'),
                parent=self)
            return
        if not messagebox.askyesno(
                T('rm  删除', 'rm  delete'),
                T(f'把槽位 [{name}] 移入回收站?\n(回收站可随时恢复, 不直接删除)',
                  f'Move slot [{name}] to the recycle bin?\n(restorable at any time, not deleted)'),
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
                self._emit(T(f'槽位 [{name}] 已移入回收站, 可点 [trash 回收站] 恢复。',
                             f'Slot [{name}] moved to the recycle bin; restore via [trash].'))
                self._vps_push()
                self._emit(None, 'refresh')
            finally:
                pool.release_lock()

        self.run_bg(work)

    def on_refresh(self):
        self.refresh_table()
        self.show_live_summary()
        n = len([s for s in slot_names() if (SLOTS / s / 'auth.json').exists()])
        self._emit(T(f'已刷新: 池中 {n} 个账号。额度数字为上次查询结果, 联网更新请点 [Status]。',
                     f'Refreshed: {n} account(s) in pool. Quota numbers are from the last query; '
                     f'press [Status] to update online.'))

    def on_trash(self):
        win = tk.Toplevel(self)
        win.title(T('回收站 (删除的账号)', 'Recycle bin (deleted accounts)'))
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
                lb.insert('end', T('(回收站为空)', '(recycle bin is empty)'))

        def restore():
            sel = lb.curselection()
            if not sel or not items:
                return
            d = items[sel[0]]
            name = d.name.split('__')[0]
            target = name if not (SLOTS / name).exists() else pick_target_slot(None)
            if not pool.acquire_lock():
                messagebox.showwarning(T('回收站', 'Recycle bin'),
                                       T('有切换操作进行中, 稍后再试。',
                                         'A switch is in progress; try again later.'), parent=win)
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
            self._emit(T(f'已从回收站恢复 → 槽位 [{target}]。',
                         f'Restored from recycle bin -> slot [{target}].'))
            self._emit(None, 'refresh')
            load()

        def purge():
            if not messagebox.askyesno(T('回收站', 'Recycle bin'),
                                       T('彻底清空回收站? 此操作不可恢复!',
                                         'Purge the recycle bin completely? This cannot be undone!'),
                                       parent=win):
                return
            if pool.TRASH.exists():
                for d in list(pool.TRASH.iterdir()):
                    if d.is_dir():
                        shutil.rmtree(d, ignore_errors=True)
                    else:
                        d.unlink(missing_ok=True)
            self._emit(T('回收站已清空(彻底删除)。', 'Recycle bin purged (permanently deleted).'))
            load()

        ttk.Button(btns, text=T('恢复到池中', 'Restore to pool'), command=restore).pack(side='left', padx=(0, 6))
        ttk.Button(btns, text=T('清空回收站', 'Purge'), command=purge).pack(side='left', padx=(0, 6))
        ttk.Button(btns, text=T('关闭', 'Close'), command=win.destroy).pack(side='right')
        load()

    def _vps_snapshot(self):
        """收集全部槽位的只读快照(access_token 级, 不含 refresh_token)。"""
        accounts = []
        for name in slot_names():
            f = SLOTS / name / 'auth.json'
            if not f.exists():
                continue
            try:
                auth = pool.load_auth(f)
                email, plan, until, _ = pool.describe(auth)
                accounts.append({
                    'slot': name, 'email': email, 'plan': plan, 'sub_until': until,
                    'access_token': auth['tokens'].get('access_token', ''),
                    'account_id': auth['tokens'].get('account_id')
                                  or pool.account_id(auth) or '',
                })
            except Exception:
                continue
        return accounts

    def _vps_push(self, manual=False):
        """把账号快照+最新额度推给 VPS 监控(未配置则静默跳过; 后台线程)。"""
        cfg = load_vps_cfg()
        if not cfg['url']:
            if manual:
                self._emit(T('未配置 VPS 监控地址。', 'VPS monitor URL not configured.'))
            return

        def work():
            try:
                body = json.dumps({
                    'accounts': self._vps_snapshot(),
                    'usage': {k: {'p': v.get('p'), 's': v.get('s'),
                                  'reached': k in self.reached}
                              for k, v in self.quota.items() if isinstance(v, dict)},
                }, ensure_ascii=False).encode('utf-8')
                req = urllib.request.Request(
                    cfg['url'] + '/sync', data=body,
                    headers={'Content-Type': 'application/json',
                             'X-Auth-Key': cfg['key']})
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
                if manual:
                    self._emit(T('VPS 同步成功。', 'VPS sync OK.'))
            except Exception as e:
                self._emit(T(f'VPS 同步失败: {e}', f'VPS sync failed: {e}'))

        threading.Thread(target=work, daemon=True).start()

    def on_vps(self):
        cfg = load_vps_cfg()
        win = tk.Toplevel(self)
        win.title(T('VPS 远程监控', 'VPS remote monitor'))
        win.geometry('460x260')
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=T('监控服务地址 (如 http://1.2.3.4:8787)',
                              'Monitor URL (e.g. http://1.2.3.4:8787)')).pack(anchor='w')
        url = tk.StringVar(value=cfg['url'])
        ttk.Entry(frm, textvariable=url, width=52).pack(fill='x', pady=(2, 8))
        ttk.Label(frm, text=T('Auth Key (与服务器 monitor-config.json 一致)',
                              'Auth key (must match the server config)')).pack(anchor='w')
        key = tk.StringVar(value=cfg['key'])
        ttk.Entry(frm, textvariable=key, width=52, show='*').pack(fill='x', pady=(2, 8))
        ttk.Label(frm, foreground='#777', wraplength=430,
                  text=T('保存后: 切换/收号/查额度时自动推送快照(仅 access_token, 只读监控)。',
                          'After saving: snapshots (access_token only, read-only) '
                          'are pushed on switch/capture/status.')).pack(anchor='w')

        def save():
            save_vps_cfg(url.get(), key.get())
            m = T('已保存并立即同步。', 'Saved; syncing now.')
            self._emit(T(f'VPS 监控配置已保存: {url.get() or "(未配置)"}。{m}',
                         f'VPS monitor config saved: {url.get() or "(none)"}. {m}'))
            self._vps_push(manual=True)
            win.destroy()

        ttk.Button(frm, text=T('保存并测试', 'Save & test'), command=save).pack(pady=(8, 0))

    def on_proxy(self):
        try:
            cur = json.loads(PROXY_FILE.read_text(encoding='utf-8')).get('proxy', 'auto')
        except Exception:
            cur = 'auto'
        val = simpledialog.askstring(
            T('代理设置', 'Proxy'),
            T('代理地址, 例如 http://127.0.0.1:7897\nauto=自动探测   none=强制直连',
              'Proxy URL, e.g. http://127.0.0.1:7897\nauto=auto-detect   none=force direct'),
            initialvalue=cur, parent=self)
        if val is None:
            return
        val = val.strip() or 'auto'
        PROXY_FILE.write_text(json.dumps({'proxy': val}, ensure_ascii=False),
                              encoding='utf-8')
        global _opener
        _opener = None
        found = detect_proxy()
        self._emit(T(f'代理设置: {val} → 实际使用: {found or "直连"}',
                     f'Proxy setting: {val} -> using: {found or "direct"}'))

    def on_status(self):
        names = slot_names()
        targets = [n for n in names if (SLOTS / n / 'auth.json').exists()]
        if not targets:
            self._emit(T('池子里还没有账号, 先 add/get。',
                         'The pool is empty; add/get an account first.'))
            return

        def work():
            proxy = detect_proxy()
            self._emit(T('出口代理: ', 'Egress proxy: ') +
                       (f'{proxy} ' + T('(自动探测)', '(auto-detected)') if proxy else
                        T('直连(未发现本机代理端口, 被拦时开系统代理/TUN或在proxy里手填)',
                          'direct (no local proxy port found; enable system proxy/TUN or set it in proxy)')))
            self._emit(T(f'查询 {len(targets)} 个账号的额度(5h/周)...',
                         f'Querying 5h/weekly quota for {len(targets)} account(s)...'))
            for name in targets:
                f = SLOTS / name / 'auth.json'
                try:
                    auth = pool.load_auth(f)
                    email, plan, until, _ = pool.describe(auth)
                    if exp_epoch(auth) < time.time() + 120:
                        self._emit(T(f'槽位 [{name}] {email}: token 过期, 正在刷新...',
                                     f'Slot [{name}] {email}: token expired, refreshing...'))
                        auth = refresh_auth(auth)
                        save_auth(f, auth)
                        self._emit(T(f'槽位 [{name}]: token 已刷新。',
                                     f'Slot [{name}]: token refreshed.'))
                    u = fetch_usage(auth)
                    _na = T('无', 'n/a')
                    p_txt = f"{u['primary']:.0f}%" if u['primary'] is not None else _na
                    s_txt = f"{u['secondary']:.0f}%" if u['secondary'] is not None else _na
                    self.quota[name] = {'p': p_txt, 's': s_txt,
                                        'pr': u['primary_reset_at'],
                                        'sr': u['secondary_reset_at'],
                                        'cards': u.get('cards')}
                    if u['reached']:
                        self.reached.add(name)
                    else:
                        self.reached.discard(name)
                    full = T('  <-- 当前窗口已打满', '  <-- MAXED OUT') if u['reached'] else ''
                    cards = (T(f" | 重置卡 {u.get('cards')}张(通用)",
                               f" | reset credits: {u.get('cards')} (shared)")
                             if u.get('cards') is not None else '')
                    p_part = (T(f"5h {p_txt} (重置 {fmt_dur(u['primary_reset'])})",
                                f"5h {p_txt} (resets in {fmt_dur(u['primary_reset'])})")
                              if u['primary'] is not None else
                              T('5h 无此窗口(仅周额度)', '5h no such window (weekly-only plan)'))
                    s_part = (T(f"周 {s_txt} (重置 {fmt_dur(u['secondary_reset'])})",
                                f"wk {s_txt} (resets in {fmt_dur(u['secondary_reset'])})")
                              if u['secondary'] is not None else
                              T('周 无此窗口', 'wk no such window'))
                    self._emit(T(f'槽位 [{name}] {email}: {p_part} | {s_part}{cards} | 订阅至 {until}{full}',
                                 f'Slot [{name}] {email}: {p_part} | {s_part}{cards} | sub until {until}{full}'))
                except Exception as e:
                    self.quota[name] = {'err': http_err_short(e)}
                    self._emit(T(f'槽位 [{name}] 查询失败: {http_err_short(e)}',
                                 f'Slot [{name}] query failed: {http_err_short(e)}'))
                self._emit(None, 'refresh')
            self._emit(T('额度查询完成。', 'Quota query finished.'))
            self._vps_push()

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
