# codex-pool — Codex 多账号号池管理器

[English](README.en.md) | 简体中文

Windows 下为 Codex Desktop / CLI 管理多个 ChatGPT 账号：把各账号的登录凭证
（`~/.codex/auth.json`）快照到命名槽位，一键切换、查询额度、账号报废回收。

> ⚠️ **风险声明**：多账号轮换可能违反 OpenAI 使用条款（规避速率限额）。
> 本工具仅供学习研究本地凭证管理机制，使用产生的任何后果（含账号封禁）自负。

![GUI 预览](docs/gui-preview.png)

## 功能

- **号池管理**：`add`（隔离当前登录，登录新号）/ `get`（收取并验证新号）/
  `use`（切换）/ `rm`（移入回收站）完整生命周期
- **GUI 控制台**（Tkinter，无第三方依赖）：操作台 + 可滚动账号台账，
  双击"密码/附加"列直接编辑，选中行一键切换
- **额度监控**：官方接口查询各账号 5h / 周窗口用量、重置时间、重置卡余量；
  自动识别新版"仅周额度"账号与旧版双窗口账号
- **账号幂等**：一号一槽，重复收取自动刷新原槽位，绝不产生重复槽位
- **切换防护**：45 秒冷却防连切、GUI/CLI 跨进程文件锁、
  切换前自动退出 Codex、原子写入 + 写后回读校验、改动前时间戳备份
- **回收站**：删除的账号进 `~/.codex-pool/trash/`，可随时恢复或彻底清空
- **代理自适应**：自动探测系统代理 / 本机常见代理端口（Clash/V2Ray 等），
  可手动指定

## 安装

要求：Windows + Python 3.8+（自带 tkinter，无第三方依赖）。

把 `codex-pool.py` 和 `codex-pool-gui.py` 放在**同一目录**，然后：

```bat
:: 命令行版
python codex-pool.py status

:: GUI 版
pythonw codex-pool-gui.py
```

数据目录固定在 `~/.codex-pool/`（槽位/密码/备份/回收站都在这里）。

## 快速开始

```bat
codex-pool capture 1        :: 把当前已登录的账号收进槽位 1
codex-pool add 2            :: 隔离当前登录, 之后在 Codex 里登录新号
                             :: (登录完成后 GUI 点 get / CLI capture 2)
codex-pool use 2            :: 切换到槽位 2 (需先完全退出 Codex)
codex-pool status           :: 查看池子
```

GUI 流程更简单：`add` → 打开 Codex 登录新号 → `get` → 完成入池；
日常点 `Status` 看额度，选中行点 `use`（或选行后确认）切换。

## 命令一览（CLI）

| 命令 | 作用 |
|---|---|
| `add <slot>` | 在隔离目录发起 `codex login`，不动当前登录 |
| `capture <slot>` | 把当前登录收进槽位（幂等，一号一槽） |
| `use <slot>` | 切换槽位（带冷却/锁/校验/备份全套防护） |
| `rm <slot>` | 移入回收站（当前登录中的号受保护拒删） |
| `status` | 查看登录状态与所有槽位 |

## 安全须知

- `~/.codex-pool/` 内含账号 OAuth 凭证与**明文密码**，等同账号本体：
  不要提交到任何仓库（本项目 `.gitignore` 已排除）、不要同步网盘、不要外发。
- 每次改动 `auth.json` 前都会自动备份到 `~/.codex-pool/backups/`，
  误操作可从备份恢复。
- 切换账号请先完全退出 Codex Desktop，工具会自动处理并校验。

## 已知边界

- 仅支持 Windows（进程管理用 taskkill/tasklist，代理探测读注册表）。
- 额度接口为非公开接口（`chatgpt.com/backend-api/codex/usage`），
  字段变动可能导致额度列显示异常（不影响号池核心功能）。

## License

[MIT](LICENSE)
