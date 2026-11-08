#!/usr/bin/env python3
"""codex-pool - Codex 多账号号池管理器

通过把 ~/.codex/auth.json 快照到命名槽位(slot)并在槽位间切换,
实现多个 ChatGPT Plus 账号轮换使用 Codex Desktop / CLI。

用法:
  codex-pool add <slot>       在隔离目录里发起新登录, 直接收进槽位(不动当前登录)
  codex-pool capture <slot>   把当前登录的账号收进槽位
  codex-pool use <slot>       切换到槽位(先把当前号的最新 token 回存它的槽位)
  codex-pool rm <slot>        删除槽位(移入回收站, 当前登录中的号受保护)
  codex-pool status           查看当前登录账号和所有槽位

示例(组三个号的池):
  codex-pool capture 1        # 收第一个号
  # 在 Codex Desktop 里退出登录 -> 登录第二个号
  codex-pool capture 2
  # 再登录第三个号
  codex-pool capture 3
  # 之后任意切换:
  codex-pool use 2

注意: 切换后需重启 Codex Desktop(或新开 CLI 会话)才会用新账号。
"""
import base64
import datetime
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

CODEX_HOME = pathlib.Path.home() / '.codex'
LIVE = CODEX_HOME / 'auth.json'
POOL = pathlib.Path.home() / '.codex-pool'
SLOTS = POOL / 'slots'
CURRENT = POOL / 'current'
LOCKF = POOL / 'switch.lock'
STATEF = POOL / 'state.json'
BACKUP_DIR = POOL / 'backups'
TRASH = POOL / 'trash'
COOLDOWN_SEC = 45

# 首次运行自动创建数据目录骨架(全新机器没有 ~/.codex-pool, 写锁/状态文件前必须存在)
try:
    POOL.mkdir(parents=True, exist_ok=True)
    SLOTS.mkdir(exist_ok=True)
except Exception:
    pass  # 目录不可写时留给具体操作报出明确错误


def mask(email):
    if not isinstance(email, str) or '@' not in email:
        return str(email)
    user, domain = email.split('@', 1)
    head = user[:2] if len(user) > 2 else user[:1]
    return f'{head}***@{domain}'


def jwt_payload(token):
    seg = token.split('.')[1]
    seg += '=' * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def load_auth(path):
    auth = json.loads(path.read_text(encoding='utf-8'))
    if 'tokens' not in auth:
        raise ValueError('not a codex auth file')
    return auth


def account_id(auth):
    try:
        claims = jwt_payload(auth['tokens']['id_token'])
        info = claims.get('https://api.openai.com/auth', {})
        return info.get('chatgpt_account_id') or claims.get('chatgpt_account_id')
    except Exception:
        return None


def describe(auth):
    email = plan = until = 'unknown'
    exp_s = '?'
    try:
        claims = jwt_payload(auth['tokens']['id_token'])
        email = mask(claims.get('email'))
        info = claims.get('https://api.openai.com/auth', {})
        plan = info.get('chatgpt_plan_type') or 'unknown'
        until = (info.get('chatgpt_subscription_active_until') or '?')[:10]
    except Exception:
        pass
    try:
        exp = jwt_payload(auth['tokens']['access_token']).get('exp')
        if exp:
            exp_s = datetime.datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M')
    except Exception:
        pass
    return email, plan, until, exp_s


def load_live():
    if not LIVE.exists():
        sys.exit(f'错误: {LIVE} 不存在, 请先在 Codex Desktop 登录。')
    try:
        return load_auth(LIVE)
    except Exception:
        sys.exit(f'错误: {LIVE} 不是有效的登录文件。')


def slot_dir(name):
    if not name or any(ch in name for ch in '\\/: .'):
        sys.exit("错误: 槽位名只能是简单字符, 例如 1 / 2 / 3")
    return SLOTS / name


def read_current():
    try:
        return CURRENT.read_text(encoding='utf-8').strip()
    except Exception:
        return None


def sync_live_to_current_slot():
    """把线上 auth.json 里刷新过的 token 回存到当前槽位(仅当账号一致时)。"""
    cur = read_current()
    if not cur:
        return
    target = slot_dir(cur) / 'auth.json'
    if not target.exists() or not LIVE.exists():
        return
    try:
        if account_id(load_auth(LIVE)) == account_id(load_auth(target)):
            write_auth_atomic(load_auth(LIVE), target)
    except Exception:
        pass  # 读不出来就不动, 宁可少同步也不覆盖错账号


# ---------- 幂等 / 冷却 / 覆盖防护 (GUI 与 CLI 共用) ----------

def write_auth_atomic(auth, dest):
    """先写临时文件再原子替换, 避免写一半被读走。"""
    dest = pathlib.Path(dest)
    tmp = dest.with_name(dest.name + '.tmp')
    tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, dest)


def backup_file(src, tag):
    """把凭证文件备份到 backups/, 返回备份路径(失败返回 None)。"""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        dst = BACKUP_DIR / f'{tag}-{ts}.json'
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None


def find_slot_by_account(acc):
    """按 account_id 找到已存在的槽位名, 没有则 None。保证一号一槽(幂等)。"""
    if not acc:
        return None
    for name in sorted(os.listdir(SLOTS)) if SLOTS.exists() else []:
        f = SLOTS / name / 'auth.json'
        if f.exists():
            try:
                if account_id(load_auth(f)) == acc:
                    return name
            except Exception:
                continue
    return None


def acquire_lock(wait=10):
    """跨进程切换锁(GUI 与 CLI 互斥)。超60秒的锁视为残留自动清除。"""
    LOCKF.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(LOCKF, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - LOCKF.stat().st_mtime > 60:
                    LOCKF.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                return False
            time.sleep(0.4)


def release_lock():
    try:
        LOCKF.unlink()
    except FileNotFoundError:
        pass


def cooldown_remaining():
    """距上次切换的冷却剩余秒数。"""
    try:
        st = json.loads(STATEF.read_text(encoding='utf-8'))
        last = datetime.datetime.fromisoformat(st['last_switch'])
        return max(0.0, COOLDOWN_SEC - (datetime.datetime.now() - last).total_seconds())
    except Exception:
        return 0.0


def note_switch():
    STATEF.write_text(json.dumps(
        {'last_switch': datetime.datetime.now().isoformat(timespec='seconds')},
        ensure_ascii=False), encoding='utf-8')


def codex_running():
    # Codex Desktop(MSIX)宿主是 ChatGPT.exe, 会孵化/复活 codex.exe, 两个都要查
    r = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'],
                       capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    out = r.stdout.lower()
    return b'codex.exe' in out or b'chatgpt.exe' in out


def verify_live(expect_acc, tries=3):
    """回读校验 live auth.json 确实是目标账号(防覆盖竞争)。"""
    for _ in range(tries):
        try:
            if account_id(load_auth(LIVE)) == expect_acc:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def find_codex_exe():
    p = shutil.which('codex') or shutil.which('codex.exe')
    if p:
        return p
    hits = glob.glob(str(pathlib.Path(os.environ.get('LOCALAPPDATA', str(pathlib.Path.home() / 'AppData' / 'Local'))) / 'OpenAI' / 'Codex' / 'bin' / '*' / 'codex.exe'))
    return max(hits, key=os.path.getmtime) if hits else None


def cmd_add(slot):
    """在槽位目录里以独立 CODEX_HOME 发起 codex login, 登录结果直接落进槽位。

    优点: 不影响 Desktop 当前的登录, 无需退出当前号。
    """
    sd = slot_dir(slot)
    sd.mkdir(parents=True, exist_ok=True)
    if (sd / 'auth.json').exists():
        try:
            who = describe(load_auth(sd / 'auth.json'))[0]
            sys.exit(f'错误: 槽位 [{slot}] 已有账号 {who}, 先手动删除 '
                     f'{sd / "auth.json"} 才能重新 add。')
        except Exception:
            pass
    exe = find_codex_exe()
    if not exe:
        sys.exit('错误: 找不到 codex 可执行文件(不在 PATH, 也没找到 Desktop 自带的)。')
    print(f'槽位 [{slot}] 发起登录: {exe}')
    print('浏览器会打开 OpenAI 登录页, 请登录要入池的那个号(可能要邮箱码/动态码)。')
    print('这一步每个号只需要做一次, 以后切号不再需要任何验证码。')
    env = dict(os.environ, CODEX_HOME=str(sd))
    ret = subprocess.run([exe, 'login'], env=env).returncode
    f = sd / 'auth.json'
    if ret != 0 or not f.exists():
        sys.exit('登录未完成, 槽位未写入(可重试)。')
    email, plan, until, _ = describe(load_auth(f))
    print(f'已收进槽位 [{slot}]: {email} ({plan}, 订阅至 {until})')
    print('当前 Desktop 登录未受影响。')


def cmd_capture(slot):
    sd = slot_dir(slot)
    sd.mkdir(parents=True, exist_ok=True)
    auth = load_live()
    dup = find_slot_by_account(account_id(auth))
    if dup and dup != slot:
        sys.exit(f'错误: 这个号已经在槽位 [{dup}] 里了(一号一槽)。要刷新它就 capture {dup}, '
                 f'或先 rm {dup}。')
    old = ''
    if (sd / 'auth.json').exists():
        try:
            old = ' (覆盖原 ' + describe(load_auth(sd / 'auth.json'))[0] + ')'
        except Exception:
            old = ' (覆盖无法识别的旧文件)'
    shutil.copy2(LIVE, sd / 'auth.json')
    CURRENT.write_text(slot, encoding='utf-8')
    email, plan, until, _ = describe(auth)
    print(f'已收进槽位 [{slot}]: {email} ({plan}, 订阅至 {until}){old}')


def cmd_use(slot):
    sd = slot_dir(slot)
    if not (sd / 'auth.json').exists():
        sys.exit(f'错误: 槽位 [{slot}] 是空的, 先执行: codex-pool capture {slot}')
    if codex_running():
        sys.exit('错误: Codex Desktop 正在运行, 先完全退出再切换(避免它回写覆盖 auth.json)。')
    cd = cooldown_remaining()
    if cd > 0:
        sys.exit(f'冷却中: 距上次切换不足 {COOLDOWN_SEC} 秒, 请等 {int(cd) + 1} 秒。')
    if LIVE.exists():
        try:
            live_acc = account_id(load_auth(LIVE))
            if live_acc and not find_slot_by_account(live_acc):
                sys.exit('保护: 当前登录的号还不在池子里, 直接切走它将只存在于备份中。\n'
                         '先执行 codex-pool capture <空槽位> 把它收进池, 再 use 其他槽位。')
        except Exception:
            pass
    if not acquire_lock():
        sys.exit('错误: 另一个切换操作正在进行(GUI 或 CLI), 请稍后重试。')
    try:
        target_auth = load_auth(sd / 'auth.json')
        target_acc = account_id(target_auth)
        if LIVE.exists():
            try:
                if account_id(load_auth(LIVE)) == target_acc:
                    CURRENT.write_text(slot, encoding='utf-8')
                    print(f'槽位 [{slot}] 已是当前登录, 无需切换(幂等)。')
                    return
            except Exception:
                pass
            backup_file(LIVE, f'live-before-use{slot}')
            sync_live_to_current_slot()
        write_auth_atomic(target_auth, LIVE)
        if not verify_live(target_acc):
            sys.exit('校验失败: live auth.json 与目标账号不符, 原文件已备份在 backups/, 请重试。')
        CURRENT.write_text(slot, encoding='utf-8')
        note_switch()
        email, plan, until, _ = describe(load_auth(LIVE))
        print(f'已切换到槽位 [{slot}]: {email} ({plan}, 订阅至 {until})')
        print('提示: 重启 Codex Desktop 或新开 CLI 会话后生效。')
    finally:
        release_lock()


def cmd_status():
    if LIVE.exists():
        try:
            email, plan, until, exp = describe(load_auth(LIVE))
            print(f'当前登录: {email} ({plan}, 订阅至 {until}, access_token 过期 {exp})')
        except Exception:
            print('当前登录: <auth.json 无法解析>')
    else:
        print('当前登录: <未登录>')
    print(f'当前槽位: {read_current() or "(未标记)"}')
    if not SLOTS.exists():
        print('槽位: 无 (先用 capture 收号)')
        return
    for sd in sorted(SLOTS.iterdir()):
        f = sd / 'auth.json'
        if not f.exists():
            print(f'  [{sd.name}] 空')
            continue
        try:
            email, plan, until, exp = describe(load_auth(f))
            mark = '  <== 当前' if sd.name == read_current() else ''
            print(f'  [{sd.name}] {email} ({plan}, 订阅至 {until}){mark}')
        except Exception:
            print(f'  [{sd.name}] <文件损坏>')


def cmd_rm(slot):
    sd = slot_dir(slot)
    if not sd.exists():
        sys.exit(f'错误: 槽位 [{slot}] 不存在。')
    try:
        acc = account_id(load_auth(sd / 'auth.json'))
    except Exception:
        acc = None
    if acc and LIVE.exists():
        try:
            if account_id(load_auth(LIVE)) == acc:
                sys.exit('保护: 该号当前正在登录中, 不能直接删除。\n'
                         '先 use 其他槽位切换走, 或用 add 隔离当前登录后再删。')
        except SystemExit:
            raise
        except Exception:
            pass
    TRASH.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    dst = TRASH / f'{slot}__{ts}'
    shutil.move(str(sd), str(dst))
    note = ' (原为当前槽位标记, 切换其他槽位时会自动忽略)' if read_current() == slot else ''
    print(f'槽位 [{slot}] 已移入回收站: {dst.name}{note}')
    print(f'恢复: 将 {dst.name} 目录移回 slots/{slot} 即可; 彻底删除: 清空 trash/ 目录。')


def _ensure_cjk_stdout():
    """控制台编码编不了中文(如西文 Windows 的 cp1252)时切换 UTF-8, 避免打印即崩溃。"""
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if not stream or not hasattr(stream, 'reconfigure'):
            continue
        try:
            '\u4e2d'.encode(stream.encoding or 'ascii')
        except (UnicodeEncodeError, LookupError):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


def main():
    _ensure_cjk_stdout()
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__)
        return
    cmd, *rest = argv
    if cmd == 'add' and rest:
        cmd_add(rest[0])
    elif cmd == 'rm' and rest:
        cmd_rm(rest[0])
    elif cmd == 'capture' and rest:
        cmd_capture(rest[0])
    elif cmd == 'use' and rest:
        cmd_use(rest[0])
    elif cmd in ('status', 'list'):
        cmd_status()
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
