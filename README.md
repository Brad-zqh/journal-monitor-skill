# Journal Monitor + Codex Skill

Track journal submissions in **Editorial Manager** and **ScholarOne / Manuscript Central**, with email and ServerChan notifications. Includes an installable Codex skill for setup and maintenance.

期刊投稿状态追踪工具，附可安装的 Codex skill。支持定时检查、状态变化识别、决定信提取，以及邮件和 Server酱通知。

## 能做什么

- 通过 Playwright 登录作者面板，提取稿件号、题名、提交日期、状态日期和当前状态。
- 扫描处理中、修回中、退回作者和已有决定等队列；适用页面可提取决定信。
- 将实际状态、状态日期或正式决定的变化列入通知；决定信的 Cookie 和排版变化不算状态变化。
- 用 `ignore_ids` 隐藏结束的稿件，用 `status_overrides` 保存人工确认的状态更正。
- 登录信息支持环境变量、GitHub Secrets 或本地忽略文件；按期刊独立配置。
- 已有离线解析和状态分类测试。验证码、多因素认证和不同期刊页面变体可能需要适配，程序不绕过人工验证。

## 安装 Codex skill

把下面这句话发给 Codex：

```text
使用 $skill-installer 从 https://github.com/Brad-zqh/journal-monitor-skill/tree/main/skills/journal-monitor 安装 journal-monitor skill。
```

也可以把仓库中的 `skills/journal-monitor` 文件夹复制到自己的 `$CODEX_HOME/skills/`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills/`。已存在同名 skill 时先检查其内容，避免覆盖自己的配置。

安装后可这样使用：

```text
使用 $journal-monitor 为我配置 Journal of Rural Studies 投稿监控，使用独立账号，每天北京时间 08:17 和 20:17 检查。
```

```text
使用 $journal-monitor 检查最近一次 GitHub Actions 运行，诊断为什么没有收到通知。
```

skill 会指导 Codex 操作工具仓库；仅安装 skill 不会启动监控、创建仓库或发送通知。

## 本地运行

需要 Python 3.10+，CI 使用 Python 3.12。

```bash
git clone https://github.com/Brad-zqh/journal-monitor-skill.git
cd journal-monitor-skill
python -m pip install -r requirements.txt
python -m playwright install chromium
```

编辑 `journals_config.json`，将目标期刊的 `enabled` 改为 `true`，核实投稿入口。示例提供 Cities、Journal of Rural Studies 和 IJCHM，初始均停用。

为示例 Cities 设置登录环境变量，然后运行：

```bash
export JOURNAL_USERNAME="your-login-name"
read -s -p "Journal password: " JOURNAL_PASSWORD; echo
export JOURNAL_PASSWORD
export JOURNAL_DISABLE_NOTIFICATIONS=1
python check_journals.py
```

PowerShell 可这样输入密码，避免把密码写在命令历史里：

```powershell
$env:JOURNAL_USERNAME = Read-Host 'Journal username'
$credential = Get-Credential -UserName $env:JOURNAL_USERNAME -Message 'Journal login'
$env:JOURNAL_PASSWORD = $credential.GetNetworkCredential().Password
$env:JOURNAL_DISABLE_NOTIFICATIONS = '1'
python check_journals.py
```

`.env.example` 列出支持的变量名；程序不自动加载 `.env`。本地也可以复制 `journals_private.example.json` 为 `journals_private.json` 后填入信息，该文件已忽略。缓存默认保存于 `.journal-monitor/article_status_cache.json`，可通过 `JOURNAL_CACHE_FILE` 指定其他位置。

## 邮件和微信通知

邮件需要同时设置 `EMAIL_SENDER`、`EMAIL_PASSWORD` 和 `EMAIL_RECEIVER`；SMTP 默认使用 `smtp.gmail.com:587`，可通过 `SMTP_SERVER`、`SMTP_PORT` 改为自己的服务。`EMAIL_PASSWORD` 使用服务商支持的 SMTP 凭据，例如应用专用密码。

设置 `SERVERCHAN_KEY` 可启用 Server酱。两个渠道独立尝试，发送失败在重试后会使进程报错。

`JOURNAL_DISABLE_NOTIFICATIONS=1` 会关闭通知，适合先验证登录。正式启用通知前，移除此变量或设为 `0`。默认每次运行发送报告；将配置中的 `notify_only_on_change` 设为 `true` 可只在检测到已有稿件状态变化时通知。

## GitHub Actions 定时运行

公开仓库用于分发代码。运行日志可能包含稿件号、状态和决定信，因此随附监控工作流在**私有运行仓库**中启用；公共仓库仅运行不使用账号的测试。

1. 点击 **Use this template**，创建自己的 **Private** 仓库。
2. 在 `journals_config.json` 启用目标期刊。
3. 在 **Settings → Secrets and variables → Actions → Secrets** 添加对应账号密码，以及需要的通知凭据。
4. 添加仓库 Variable `JOURNAL_MONITOR_ENABLED=true`。可选 `JOURNALS_ENABLED` 是逗号分隔的期刊白名单，设置后会覆盖配置中的 `enabled`。
5. 在 Actions 中手动运行 **Check journal status**，检查真实抓取结果和需要的通知渠道日志。

| 示例期刊 | 账号 Secret | 密码 Secret |
| --- | --- | --- |
| Cities | `JOURNAL_USERNAME` | `JOURNAL_PASSWORD` |
| Journal of Rural Studies | `RURAL_USERNAME` | `RURAL_PASSWORD` |
| IJCHM | `IJCHM_USERNAME` | `IJCHM_PASSWORD` |

随附工作流每天北京时间 **08:17、20:17**（UTC **00:17、12:17**）检查。GitHub 定时触发可能延迟。

状态缓存在私有仓库的 Actions Cache 中保存，**不提交回 Git**。缓存可能被 GitHub 清理；缓存丢失时，下一次成功抓取会重新建立基线，不应当作完整的历史档案。公共仓库及其副本均应保持真实缓存、决定信、截图和账号文件不进入 Git。

新增其他期刊时，可复制一个相同平台的条目，核实 `url`，并添加独立的 `username_env` / `password_env`；使用新的 Secret 名称时，也要把对应环境变量接入 `.github/workflows/check.yml`。

## 开发与验证

```bash
python skills/journal-monitor/scripts/audit_repo.py .
python -m unittest discover -v
python -m py_compile check_journals.py
```

测试使用合成 HTML 和虚构稿件，不访问真实投稿系统或发送通知。提交问题时请先去除账号、稿件信息和决定信。

## 许可

[MIT License](LICENSE)。本项目为独立工具，与期刊、出版商、Editorial Manager、ScholarOne 或 OpenAI 无隶属关系。
