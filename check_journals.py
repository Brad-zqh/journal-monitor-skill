#!/usr/bin/env python3
"""
期刊投稿状态追踪器：Editorial Manager 与 ScholarOne。
账号密码等敏感信息从环境变量（GitHub Secrets）读取，不写在代码里。
"""

import asyncio
import json
import os
import re
import smtplib
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 中国标准时间（UTC+8）——云端 GitHub Actions 默认 UTC，统一换算成北京时间
CN_TZ = timezone(timedelta(hours=8))


def now_cn():
    return datetime.now(CN_TZ)
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.async_api import async_playwright


def env(key, default=''):
    return os.environ.get(key, default)


SCRIPT_DIR = Path(__file__).parent
CACHE_FILE = Path(env('JOURNAL_CACHE_FILE') or str(SCRIPT_DIR / '.journal-monitor' / 'article_status_cache.json'))
JOURNALS_CONFIG_FILE = SCRIPT_DIR / 'journals_config.json'
JOURNALS_PRIVATE_CONFIG_FILE = SCRIPT_DIR / 'journals_private.json'


CONFIG = {
    # 期刊条目由 journals_config.json 提供；示例默认停用。
    'journals': [],
    # 可选：用环境变量/仓库 Variable 临时指定只监控哪些期刊（逗号分隔，如 "JAPG,JRCS"）。
    # 设置后会覆盖上面各期刊的 enabled；留空则按 enabled 字段。
    'journals_enabled': env('JOURNALS_ENABLED'),
    'username': env('JOURNAL_USERNAME'),
    'password': env('JOURNAL_PASSWORD'),

    # 邮件
    'enable_email': bool(env('EMAIL_SENDER') and env('EMAIL_PASSWORD') and env('EMAIL_RECEIVER')),
    'smtp_server': env('SMTP_SERVER', 'smtp.gmail.com'),
    'smtp_port': int(env('SMTP_PORT', '587')),
    'email_sender': env('EMAIL_SENDER'),
    'email_password': env('EMAIL_PASSWORD'),
    'email_receiver': env('EMAIL_RECEIVER'),

    # Server酱（微信）
    'enable_serverchan': bool(env('SERVERCHAN_KEY')),
    'serverchan_key': env('SERVERCHAN_KEY'),

    'notify_only_on_change': False,
    'ignore_ids': [],
}


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [x.strip() for x in value.split(',') if x.strip()]
    return []


def _load_json_file(path):
    try:
        return json.loads(path.read_text('utf-8'))
    except Exception as e:
        print(f'[{now_cn():%Y-%m-%d %H:%M:%S}] 读取 {path.name} 失败，跳过该配置: {e}', flush=True)
        return {}


def _merge_journals(base, incoming, replace=False):
    if not isinstance(incoming, list):
        return list(base)
    if replace:
        return list(incoming)
    merged = list(base)
    by_name = {j.get('name'): i for i, j in enumerate(merged) if isinstance(j, dict) and j.get('name')}
    for journal in incoming:
        if not isinstance(journal, dict):
            continue
        name = journal.get('name')
        if name in by_name:
            merged[by_name[name]] = {**merged[by_name[name]], **journal}
        else:
            by_name[name] = len(merged)
            merged.append(journal)
    return merged


def load_external_config(config):
    """Merge editable JSON configs so journals and ignored manuscripts can be maintained without code."""
    merged = dict(config)

    public_data = _load_json_file(JOURNALS_CONFIG_FILE) if JOURNALS_CONFIG_FILE.exists() else {}
    private_data = _load_json_file(JOURNALS_PRIVATE_CONFIG_FILE) if JOURNALS_PRIVATE_CONFIG_FILE.exists() else {}

    if isinstance(public_data.get('journals'), list):
        merged['journals'] = _merge_journals(merged.get('journals', []), public_data['journals'], replace=True)
    if isinstance(private_data.get('journals'), list):
        merged['journals'] = _merge_journals(merged.get('journals', []), private_data['journals'])

    ignored = []
    ignored.extend(_as_list(config.get('ignore_ids')))
    for data in (public_data, private_data):
        ignored.extend(_as_list(data.get('ignore_ids')))
        ignored.extend(_as_list(data.get('ignored_manuscripts')))
    merged['ignore_ids'] = sorted({x for x in ignored if x})

    status_overrides = {}
    if isinstance(config.get('status_overrides'), dict):
        status_overrides.update(config['status_overrides'])
    for data in (public_data, private_data):
        if isinstance(data.get('status_overrides'), dict):
            status_overrides.update(data['status_overrides'])
    merged['status_overrides'] = status_overrides

    for data in (public_data, private_data):
        for key in ('journals_enabled', 'notify_only_on_change'):
            if key in data:
                merged[key] = data[key]

    for key in (
        'email_sender',
        'email_password',
        'email_receiver',
        'serverchan_key',
        'smtp_server',
        'smtp_port',
    ):
        if key in private_data:
            merged[key] = private_data[key]
    if private_data.get('serverchan_key'):
        merged['enable_serverchan'] = True
    merged['enable_email'] = bool(
        merged.get('email_sender') and merged.get('email_password') and merged.get('email_receiver')
    )

    return merged


CONFIG = load_external_config(CONFIG)

# Allow an authenticated read-only check without sending notifications.
if env('JOURNAL_DISABLE_NOTIFICATIONS').strip().lower() in {'1', 'true', 'yes', 'on'}:
    CONFIG['enable_email'] = False
    CONFIG['enable_serverchan'] = False

# 期刊代号 -> 全名
JOURNAL_FULL = {j['name']: j.get('full', j['name']) for j in CONFIG['journals']}


def journal_label(name):
    """显示用：'JCIT · Cities'"""
    full = JOURNAL_FULL.get(name, '')
    return f'{name} · {full}' if full and full != name else name


def enabled_journals(config):
    """按 journals_enabled（白名单）或各期刊 enabled 字段，筛选要监控的期刊。"""
    whitelist = config.get('journals_enabled') or ''
    names = {x.strip().upper() for x in whitelist.split(',') if x.strip()}
    out = []
    for j in config['journals']:
        on = (j['name'].upper() in names) if names else j.get('enabled', True)
        if on:
            out.append(j)
    return out


def resolve_credential(journal_cfg, config, field):
    """Resolve per-journal credentials from env-var names, literal values, then global defaults."""
    env_key = journal_cfg.get(f'{field}_env')
    if env_key:
        value = env(env_key)
        if value:
            return value
        return journal_cfg.get(field, '')
    if journal_cfg.get(field):
        return journal_cfg[field]
    return config.get(field, '')


def credential_source_label(journal_cfg, config, field):
    """Describe credential source for notifications without exposing secrets."""
    env_key = journal_cfg.get(f'{field}_env')
    if env_key and env(env_key):
        return f'env:{env_key}'
    if journal_cfg.get(field):
        if field == 'password':
            return 'private:******'
        return f'private:{mask_secret(journal_cfg[field])}'
    if env_key:
        return f'env:{env_key}（未设置）'
    if config.get(field):
        if field == 'password':
            return 'global:******'
        return f'global:{mask_secret(config[field])}'
    return '未设置'


def is_ignored_manuscript(config, journal_name, manuscript_id):
    ignored = {x.upper() for x in _as_list(config.get('ignore_ids'))}
    candidates = {
        (manuscript_id or '').upper(),
        f'{journal_name}::{manuscript_id}'.upper(),
    }
    return bool(ignored & candidates)


def apply_status_overrides(config, articles):
    """Apply persistent, user-confirmed status corrections after portal scraping."""
    overrides = config.get('status_overrides') or {}
    if not isinstance(overrides, dict):
        return articles
    for article in articles:
        key = f'{article.get("journal", "")}::{article.get("id", "")}'
        override = overrides.get(key, overrides.get(article.get('id', '')))
        if isinstance(override, str):
            article['status'] = override
            continue
        if not isinstance(override, dict):
            continue
        for field in ('status', 'status_date', 'disposition'):
            if field in override:
                article[field] = override[field]
        if 'active' in override:
            article['manual_active'] = bool(override['active'])
    return articles


def is_closed(a):
    """稿件是否已有最终决定（被拒/接收/撤回等）→ 排到最后。"""
    if a.get('manual_active') is not None:
        return not bool(a['manual_active'])
    disposition = (a.get('disposition') or '').strip().lower()
    # ScholarOne exposes the presence of a decision letter as metadata.  A
    # revision can also have such a letter, so that flag alone is not final.
    if disposition and disposition != 'decision letter available':
        return True
    status = (a.get('status') or '').strip().lower()
    return bool(re.search(r'\b(?:reject(?:ed|ion)?|accept(?:ed|ance)?|withdraw(?:n|al)?)\b', status))

# 这些分类里可能有决定信（Major/Minor revision、Reject 等），尝试抓取 View Decision Letter 正文
LETTER_CATEGORIES = {
    'Submissions Being Processed',
    'Revisions Being Processed',
    'Submissions with a Decision',
}

MS_RE = re.compile(r'[A-Za-z]{2,}-D-\d[\w-]*')


def log(msg):
    print(f'[{now_cn():%Y-%m-%d %H:%M:%S}] {msg}', flush=True)


def mask_secret(value):
    """Mask usernames/password-like values in logs while keeping them recognizable."""
    value = value or ''
    if len(value) <= 2:
        return '*' * len(value)
    if len(value) <= 6:
        return value[0] + '*' * (len(value) - 2) + value[-1]
    return value[:2] + '*' * (len(value) - 4) + value[-2:]


# ============================================================
# 页面解析 JS
# ============================================================

# 稳健解析：表头与数据行可能在不同 <table>（决定页就是这种布局），
# 因此扫描整页 <tr>，先按列名找表头，再按稿件号正则找数据行，按列位置取值。
PARSE_ROWS_JS = r"""
() => {
  const norm = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
  const all = [...document.querySelectorAll('tr')];
  let headers = null;
  for (const tr of all) {
    const txt = norm(tr.innerText);
    if (txt.includes('Manuscript Number') && txt.includes('Title')) {
      headers = [...tr.querySelectorAll('th,td')].map(c => norm(c.innerText));
      break;
    }
  }
  if (!headers) return [];
  const idx = n => headers.findIndex(h => h.includes(n));
  const iId = idx('Manuscript Number'), iTitle = idx('Title'),
        iSub = idx('Initial Date Submitted'), iSdate = idx('Status Date'),
        iStat = idx('Current Status');
  // Final Disposition 列要排除 "Date Final Disposition Set"
  const iDisp = headers.findIndex(h => h.includes('Final Disposition') && !h.includes('Date'));
  const re = /[A-Za-z]{2,}-D-\d[\w-]*/;
  const provisionalId = title => {
    let hash = 2166136261;
    for (const ch of title.toLowerCase()) {
      hash ^= ch.charCodeAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return `PENDING-${(hash >>> 0).toString(16).toUpperCase().padStart(8, '0')}`;
  };
  const out = []; const seen = new Set();
  for (const tr of all) {
    const cells = [...tr.querySelectorAll('td')].map(c => norm(c.innerText));
    if (!cells.length) continue;
    if (cells.some(c => c.includes('Manuscript Number'))) continue;  // 跳过表头行
    const rawId = (iId >= 0 && iId < cells.length) ? cells[iId] : '';
    const title = iTitle >= 0 && iTitle < cells.length ? cells[iTitle] : '';
    const status = iStat >= 0 && iStat < cells.length ? cells[iStat] : '';
    const idMatch = rawId.match(re);
    // Editorial Manager's ingest pipeline can display a newly received
    // submission before assigning its formal manuscript number. Track it by
    // a deterministic title hash until the real number appears.
    const id = idMatch ? idMatch[0] : (title && status ? provisionalId(title) : '');
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push({
      id,
      title,
      submitted: iSub >= 0 && iSub < cells.length ? cells[iSub] : '',
      status_date: iSdate >= 0 && iSdate < cells.length ? cells[iSdate] : '',
      status,
      disposition: iDisp >= 0 && iDisp < cells.length ? cells[iDisp] : '',
    });
  }
  return out;
}
"""

# 收集本页所有「View Decision Letter」链接的 popupLetter(...) 调用串
LETTER_LINKS_JS = r"""
() => {
  const norm = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
  const calls = [];
  for (const a of document.querySelectorAll('a')) {
    const href = a.getAttribute('href') || '';
    if (/view decision letter/i.test(norm(a.textContent)) && /popupLetter/i.test(href)) {
      const call = href.replace(/^javascript:/i, '').trim().replace(/;\s*$/, '');
      if (call) calls.push(call);
    }
  }
  return [...new Set(calls)];
}
"""

SCHOLARONE_ROWS_JS = r"""
() => {
  const norm = s => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const dateRe = /\b\d{1,2}-[A-Za-z]{3}-\d{4}\b/;
  const rows = [];
  const idRe = /[A-Z]{2,}[A-Z0-9-]*-\d{2,}-\d+/i;
  for (const tr of document.querySelectorAll('tr')) {
    const cells = [...tr.querySelectorAll('td')];
    if (cells.length < 3) continue;
    const texts = cells.map(c => norm(c.innerText));
    const idIndex = texts.findIndex(t => idRe.test(t));
    if (idIndex < 0) continue;
    const idMatch = texts[idIndex].match(idRe);
    const id = idMatch ? idMatch[0] : texts[idIndex];
    if (!id) continue;

    const table = tr.closest('table');
    let headers = [];
    if (table) {
      const headerRows = [...table.querySelectorAll('tr')].filter(row => row.querySelector('th'));
      if (headerRows.length) {
        headers = [...headerRows[headerRows.length - 1].querySelectorAll('th,td')]
          .map(cell => norm(cell.innerText).toUpperCase());
      }
    }
    const headerIndex = (...names) => headers.findIndex(header =>
      names.some(name => header === name || header.includes(name))
    );

    const rowText = texts.join('\n');
    const aeMatch = rowText.match(
      /\bAE:\s*(.*?)(?=\bEIC:|[\n\r\u2022\u00b7]|$)/i
    );
    const eicMatch = rowText.match(
      /\bEIC:\s*(.*?)(?=[\n\r\u2022\u00b7]|$)/i
    );
    const ae = aeMatch ? norm(aeMatch[1]) : '';
    let eic = eicMatch ? norm(eicMatch[1]) : '';

    const idText = texts[idIndex];
    const returned = idText.match(
      /\b(Returned by .+?)\s+on\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\b/i
    );
    const decision = rowText.match(
      /\b(Approval To Transfer requested|Reject(?:ed)?|Accept(?:ed)?|Major Revision|Minor Revision|Revision Required)\s+on\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\b/i
    );

    const iStatus = headerIndex('CURRENT STATUS', 'STATUS');
    let statusRaw = iStatus >= 0 && iStatus < texts.length
      ? texts[iStatus]
      : texts.slice(0, idIndex).join('\n');
    statusRaw = statusRaw
      .replace(/\bContact Journal\b/ig, '\n')
      .replace(/\bAE:\s*[^•\n]+/ig, '\n')
      .replace(/\bEIC:\s*[^•\n]+/ig, '\n')
      .replace(/[•·]/g, '\n');
    const statusLines = statusRaw.split(/\n+/).map(norm).filter(Boolean)
      .filter(line => !/^contact journal$/i.test(line))
      .filter(line => !/^(AE|EIC)\s*:/i.test(line))
      .filter(line => !/^(continue|delete|view submission)$/i.test(line));
    const decisionStatus = decision ? norm(decision[1]) : '';
    let status = decision
      ? (/^Approval To Transfer requested$/i.test(decisionStatus)
          ? 'Rejected - Transfer approval requested'
          : decisionStatus)
      : (returned ? norm(returned[1]) : (statusLines[statusLines.length - 1] || ''));
    status = status.replace(/^[-•]\s*/, '').trim();

    if (status && eic.toLowerCase().endsWith(status.toLowerCase())) {
      eic = eic.slice(0, -status.length).trim();
    }
    if (decision) {
      const decisionIndex = eic.toLowerCase().indexOf(norm(decision[0]).toLowerCase());
      if (decisionIndex >= 0) eic = eic.slice(0, decisionIndex).trim();
    }
    eic = eic.replace(/\bview decision letter\b.*$/i, '').trim();

    const iTitle = headerIndex('TITLE');
    const iCreated = headerIndex('CREATED');
    const iSubmitted = headerIndex('SUBMITTED', 'DATE SUBMITTED');
    const iStatusDate = headerIndex('STATUS DATE');
    let title = texts[iTitle >= 0 ? iTitle : idIndex + 1] || '';
    if (/^(Awaiting |Submitted Manuscripts|Approval To Transfer|Reject(?:ed)?|Accept(?:ed)?|Major Revision|Minor Revision|Revision Required)/i.test(title)) {
      title = '';
    }
    const created = iCreated >= 0 ? (texts[iCreated].match(dateRe)?.[0] || '') : '';
    const submitted = iSubmitted >= 0 ? (texts[iSubmitted].match(dateRe)?.[0] || '') : '';
    const explicitStatusDate = iStatusDate >= 0
      ? (texts[iStatusDate].match(dateRe)?.[0] || '')
      : '';
    const statusDate = decision?.[2] || returned?.[2] || explicitStatusDate || submitted || created;
    const hasDecisionLetter = /\bview decision letter\b/i.test(rowText);

    rows.push({
      id,
      title: title.replace(/\bView Submission\b/ig, '').trim(),
      submitted,
      status_date: statusDate,
      status: status || (/\bDraft\b/i.test(idText) ? 'Draft' : 'Submitted Manuscripts'),
      disposition: hasDecisionLetter ? 'Decision letter available' : '',
      created,
      ae,
      eic,
    });
  }
  return rows;
}
"""


# ============================================================
# 抓取
# ============================================================

async def grab_letters(page, cf, calls):
    """触发每个 popupLetter(...) 弹窗，抓取决定信正文，按信中的稿件号归属。返回 {id: letter}。"""
    ctx = page.context
    result = {}
    for call in calls:
        try:
            async with ctx.expect_page(timeout=10000) as pop:
                await cf.evaluate(call)
            newp = await pop.value
            await newp.wait_for_load_state('networkidle', timeout=15000)
            text = await newp.evaluate("() => document.body.innerText")
            await newp.close()
            text = '\n'.join(ln.rstrip() for ln in text.splitlines()).strip()
            m = re.search(r'Manuscript Number[:\s]*([A-Za-z]{2,}-D-\d[\w-]*)', text)
            mid = m.group(1) if m else None
            if mid:
                result[mid] = text[:6000]
            else:
                log(f'    决定信未能解析稿件号（长度 {len(text)}）')
        except Exception as e:
            log(f'    决定信抓取失败: {repr(e)[:120]}')
    return result


async def parse_with_wait(page, tries=12, interval=700):
    """点进分类页后，列表可能延迟加载——轮询解析直到出现稿件，或确认是空列表。"""
    rows = []
    for _ in range(tries):
        cf = page.frame(name='content') or page
        try:
            rows = await cf.evaluate(PARSE_ROWS_JS)
        except Exception:
            rows = []
        if rows:
            return rows
        txt = ((await cf.evaluate("() => document.body ? document.body.innerText : ''")) or '').lower()
        if 'no submissions' in txt or 'there are no' in txt or '0 total' in txt:
            return []
        await page.wait_for_timeout(interval)
    return rows


async def fetch_journal(journal_cfg, config):
    articles = []
    error = None
    name = journal_cfg['name']
    username = resolve_credential(journal_cfg, config, 'username')
    password = resolve_credential(journal_cfg, config, 'password')

    if not username or not password:
        return [], f'RuntimeError: 未设置 {name} 登录账号或密码'

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        page = await (await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )).new_page()

        try:
            async def dismiss_cookies():
                # Cookie 同意条可能挡住后续点击；多种选择器都试，短超时、永不阻塞
                for sel in ('#onetrust-accept-btn-handler',
                            'button:has-text("Accept all cookies")',
                            'button:has-text("Accept All")',
                            'button:has-text("I Accept")'):
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=4000)
                            await page.wait_for_timeout(300)
                            return
                    except Exception:
                        pass

            log(f'[{name}] 访问投稿系统...')
            # EM 页面常因分析脚本长连接而永不 networkidle，改用 domcontentloaded 更稳
            await page.goto(journal_cfg['url'], wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(800)
            await dismiss_cookies()

            base_url = journal_cfg['url'].rstrip('/').rsplit('/', 1)[0]
            await page.goto(base_url + '/login.ashx', wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(1200)
            await dismiss_cookies()

            login_frame = page.frame(name='content') or page
            async def fill_login_field(selector, value):
                locator = login_frame.locator(selector)
                await locator.wait_for(state='attached', timeout=30000)
                try:
                    await locator.fill(value, timeout=10000)
                except Exception:
                    # Some EM deployments leave a transparent loading layer
                    # over an otherwise attached input. Assigning through the
                    # DOM is safe here and avoids losing the whole report to a
                    # transient actionability timeout.
                    await locator.evaluate(
                        """(element, fieldValue) => {
                            element.value = fieldValue;
                            element.dispatchEvent(new Event('input', {bubbles: true}));
                            element.dispatchEvent(new Event('change', {bubbles: true}));
                        }""",
                        value,
                    )

            await fill_login_field('#username', username)
            await fill_login_field('#passwordTextbox', password)
            # 优先调用 EM 自带的 doLogin('author')（绕过按钮被遮挡/不可点的问题），不行再点按钮
            logged = False
            try:
                if await login_frame.evaluate("typeof doLogin === 'function'"):
                    async with page.expect_navigation(timeout=15000):
                        await login_frame.evaluate("doLogin('author')")
                    logged = True
            except Exception as e:
                log(f'[{name}] doLogin 方式失败，改用点击: {repr(e)[:80]}')
            if not logged:
                btn = login_frame.locator(
                    '#btnAuthorLogin, input[name="authorLogin"], input[value="Author Login"]')
                async with page.expect_navigation(timeout=15000):
                    await btn.first.click(timeout=15000)
            await page.wait_for_timeout(2500)
            log(f'[{name}] 登录成功（{mask_secret(username)}）')

            await dismiss_cookies()

            async def wait_categories(tries=12):
                cf = page.frame(name='content') or page
                for _ in range(tries):
                    if await cf.locator('a:has-text("Submissions")').count() > 0:
                        return cf
                    await page.wait_for_timeout(700)
                    cf = page.frame(name='content') or page
                return cf

            # 登录后通常已在 AuthorMainMenu。先等分类链接出现；没有再 best-effort 点 Main Menu。
            cf0 = await wait_categories()
            if await cf0.locator('a:has-text("Submissions")').count() == 0:
                try:
                    mm = page.locator('a:has-text("Main Menu")')
                    if await mm.count() > 0:
                        await mm.first.click(timeout=8000)
                except Exception:
                    pass
                cf0 = await wait_categories()

            author_menu_url = (page.frame(name='content') or page).url

            categories = [
                'Submissions Being Processed',
                'Revisions Being Processed',
                'Submissions Sent Back to Author',
                'Submissions with a Decision',
            ]

            for cat in categories:
                cf = page.frame(name='content') or page
                # 读取分类链接真实 href，直接导航（云端点击常被同意条遮挡，goto 更稳）
                href = await cf.evaluate(
                    """(cat) => { const a=[...document.querySelectorAll('a')]
                        .find(a => (a.textContent||'').replace(/\\s+/g,' ').includes(cat));
                        return a ? a.href : ''; }""", cat)
                if not href:
                    continue
                log(f'[{name}] 进入: {cat}')
                await cf.goto(href, wait_until='domcontentloaded')
                await page.wait_for_timeout(800)

                rows = await parse_with_wait(page)
                cf = page.frame(name='content') or page

                # 抓决定信（如果该分类可能有）
                letters = {}
                if cat in LETTER_CATEGORIES:
                    calls = await cf.evaluate(LETTER_LINKS_JS)
                    if calls:
                        log(f'[{name}] {cat}: 发现 {len(calls)} 封决定信，抓取中...')
                        letters = await grab_letters(page, cf, calls)

                for r in rows:
                    if is_ignored_manuscript(config, name, r['id']):
                        continue
                    status = r['status'] or cat
                    if r.get('disposition'):
                        status = f"{status}（最终决定: {r['disposition']}）"
                    articles.append({
                        'journal': name, 'id': r['id'], 'title': r['title'],
                        'status': status, 'status_date': r['status_date'],
                        'submitted': r['submitted'],
                        'disposition': r.get('disposition', ''),
                        'category': cat,
                        'letter': letters.get(r['id'], ''),
                    })

                await cf.goto(author_menu_url, wait_until='domcontentloaded')
                cf = page.frame(name='content') or page
                for _ in range(8):
                    if await cf.locator('a:has-text("Submissions")').count() > 0:
                        break
                    await page.wait_for_timeout(600)
                    cf = page.frame(name='content') or page

            if not articles:
                log(f'[{name}] 未找到稿件')

        except Exception as e:
            error = f'{type(e).__name__}: {str(e)[:160]}'
            log(f'[{name}] 出错: {e}')
        finally:
            await browser.close()

    return articles, error


async def fetch_scholarone_journal(journal_cfg, config):
    articles = []
    error = None
    name = journal_cfg['name']
    username = resolve_credential(journal_cfg, config, 'username')
    password = resolve_credential(journal_cfg, config, 'password')

    if not username or not password:
        return [], f'RuntimeError: 未设置 {name} 登录账号或密码'

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        page = await (await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )).new_page()

        try:
            log(f'[{name}] 访问 ScholarOne 投稿系统...')
            await page.goto(journal_cfg['url'], wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(1200)

            async def first_visible(selectors):
                for sel in selectors:
                    loc = page.locator(sel)
                    try:
                        if await loc.count() > 0:
                            return loc.first
                    except Exception:
                        pass
                return None

            async def dismiss_popups():
                for sel in (
                    '#onetrust-accept-btn-handler',
                    'button:has-text("Accept all cookies")',
                    'button:has-text("Accept All")',
                    'button:has-text("I Accept")',
                    'button:has-text("Close")',
                ):
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=2500)
                            await page.wait_for_timeout(300)
                    except Exception:
                        pass

            await dismiss_popups()

            # ScholarOne deployments vary: some show login fields immediately, some need Login first.
            user_input = await first_visible([
                'input[name="USERID"]',
                'input[name="userId"]',
                'input[name="username"]',
                'input[id*="USERID" i]',
                'input[id*="user" i]',
                'input[type="text"]',
                'input[type="email"]',
            ])
            if not user_input:
                login_link = await first_visible([
                    'a:has-text("Log In")',
                    'a:has-text("Login")',
                    'button:has-text("Log In")',
                    'button:has-text("Login")',
                ])
                if login_link:
                    await login_link.click(timeout=10000)
                    await page.wait_for_load_state('domcontentloaded', timeout=20000)
                    await page.wait_for_timeout(1000)
                    await dismiss_popups()
                    user_input = await first_visible([
                        'input[name="USERID"]',
                        'input[name="userId"]',
                        'input[name="username"]',
                        'input[id*="USERID" i]',
                        'input[id*="user" i]',
                        'input[type="text"]',
                        'input[type="email"]',
                    ])

            if user_input:
                await user_input.fill(username)
                pass_input = await first_visible([
                    'input[name="PASSWORD"]',
                    'input[name="password"]',
                    'input[id*="PASSWORD" i]',
                    'input[type="password"]',
                ])
                if not pass_input:
                    raise RuntimeError('未找到密码输入框')
                await pass_input.fill(password)
                submit = await first_visible([
                    '#logInButton',
                    'a#logInButton',
                    'a.btn:has-text("Log In")',
                    'input[type="submit"]',
                    'button[type="submit"]',
                    'input[value*="Log In" i]',
                    'a:has-text("Log In")',
                    'button:has-text("Log In")',
                    'button:has-text("Login")',
                ])
                if not submit:
                    raise RuntimeError('未找到登录按钮')
                await submit.click(timeout=15000)
                await page.wait_for_load_state('domcontentloaded', timeout=30000)
                await page.wait_for_timeout(2500)

            login_text = (await page.evaluate("() => document.body ? document.body.innerText : ''") or '').lower()
            if 'incorrect user id or password' in login_text or 'bad username or password' in login_text:
                raise RuntimeError('ScholarOne 登录失败：账号或密码不正确')

            log(f'[{name}] 登录/会话确认完成（{mask_secret(username)}）')
            await dismiss_popups()

            author = await first_visible([
                'a:has-text("Author")',
                'button:has-text("Author")',
                '#Author',
            ])
            if author:
                await author.click(timeout=10000)
                await page.wait_for_load_state('domcontentloaded', timeout=20000)
                await page.wait_for_timeout(1200)

            # The dashboard can move a returned manuscript out of the Submitted
            # queue and into the Draft queue. Capture the currently displayed
            # queue first, then also visit Submitted Manuscripts when available.
            rows_by_id = {}
            for _ in range(10):
                dashboard_rows = await page.evaluate(SCHOLARONE_ROWS_JS)
                if dashboard_rows:
                    rows_by_id.update({r['id']: r for r in dashboard_rows})
                    break
                await page.wait_for_timeout(800)

            submitted_link = await first_visible([
                'a:has-text("Submitted Manuscripts")',
                'button:has-text("Submitted Manuscripts")',
            ])
            if submitted_link:
                await submitted_link.click(timeout=10000)
                await page.wait_for_load_state('domcontentloaded', timeout=20000)
                await page.wait_for_timeout(1200)

            for _ in range(10):
                submitted_rows = await page.evaluate(SCHOLARONE_ROWS_JS)
                if submitted_rows:
                    rows_by_id.update({r['id']: r for r in submitted_rows})
                    break
                await page.wait_for_timeout(800)
            rows = list(rows_by_id.values())

            for r in rows:
                if is_ignored_manuscript(config, name, r['id']):
                    continue
                if (r.get('status') or '').strip().lower() in {'continue', 'delete', 'view submission'}:
                    raise RuntimeError(
                        f'ScholarOne 页面结构已变化：拒绝把操作按钮当成状态 ({r.get("status")})'
                    )
                articles.append({
                    'journal': name,
                    'id': r['id'],
                    'title': r.get('title', ''),
                    'status': r.get('status') or 'Submitted Manuscripts',
                    'status_date': r.get('status_date', ''),
                    'submitted': r.get('submitted', ''),
                    'created': r.get('created', ''),
                    'ae': r.get('ae', ''),
                    'eic': r.get('eic', ''),
                    'disposition': r.get('disposition', ''),
                    'category': 'Submitted Manuscripts',
                    'letter': '',
                })

            if not articles:
                screenshot_path = SCRIPT_DIR / f'debug_{name}_{now_cn():%Y%m%d_%H%M%S}.png'
                await page.screenshot(path=str(screenshot_path), full_page=True)
                log(f'[{name}] 未找到稿件，截图: {screenshot_path}')

        except Exception as e:
            error = f'{type(e).__name__}: {str(e)[:160]}'
            try:
                screenshot_path = SCRIPT_DIR / f'error_{name}_{now_cn():%Y%m%d_%H%M%S}.png'
                await page.screenshot(path=str(screenshot_path), full_page=True)
                log(f'[{name}] 出错: {e}  截图: {screenshot_path}')
            except Exception:
                log(f'[{name}] 出错: {e}')
        finally:
            await browser.close()

    return articles, error


async def fetch_all(config):
    all_articles = []
    errors = {}
    for j in enabled_journals(config):
        platform = (j.get('platform') or 'editorialmanager').lower()
        if platform == 'scholarone':
            arts, err = await fetch_scholarone_journal(j, config)
        else:
            arts, err = await fetch_journal(j, config)
        all_articles.extend(arts)
        if err:
            errors[j['name']] = err
    return all_articles, errors


# ============================================================
# 通知
# ============================================================

NOTIFICATION_RETRY_DELAYS = (0, 5, 15)


def _send_with_retry(channel, operation):
    """Retry transient notification failures and surface the final failure to CI."""
    last_error = None
    total = len(NOTIFICATION_RETRY_DELAYS)
    for attempt, delay in enumerate(NOTIFICATION_RETRY_DELAYS, start=1):
        if delay:
            log(f'{channel}将在 {delay} 秒后重试（第 {attempt}/{total} 次）')
            time.sleep(delay)
        try:
            operation()
            return
        except Exception as e:
            last_error = e
            log(f'{channel}发送失败（第 {attempt}/{total} 次）: {e}')
    raise RuntimeError(f'{channel}连续 {total} 次发送失败: {last_error}') from last_error


def send_email(config, subject, body_html):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = config['email_sender']
    msg['To'] = config['email_receiver']
    msg.attach(MIMEText(body_html, 'html', 'utf-8'))

    def send_once():
        with smtplib.SMTP(config['smtp_server'], config['smtp_port']) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(config['email_sender'], config['email_password'])
            s.sendmail(config['email_sender'], config['email_receiver'], msg.as_string())

    _send_with_retry('邮件', send_once)
    log(f'邮件已发送 -> {config["email_receiver"]}')


def send_serverchan(config, title, content):
    def send_once():
        try:
            resp = requests.post(
                f'https://sctapi.ftqq.com/{config["serverchan_key"]}.send',
                data={'title': title[:32], 'desp': content}, timeout=15
            )
        except requests.RequestException as e:
            # Do not include the request URL in logs because it contains the SendKey.
            raise RuntimeError(f'{type(e).__name__}: Server酱请求未完成') from e

        if not resp.ok:
            raise RuntimeError(f'HTTP {resp.status_code}')
        try:
            result = resp.json()
        except ValueError as e:
            raise RuntimeError('Server酱返回了无法解析的响应') from e
        if result.get('code') != 0:
            raise RuntimeError(result.get('message') or f'返回代码 {result.get("code")}')

    _send_with_retry('Server酱', send_once)
    log('Server酱推送成功')


def _dispatch_notifications(config, title, email_body, wx_body):
    """Try every configured channel, then fail loudly if any channel exhausted retries."""
    failures = []
    if config.get('enable_email'):
        try:
            send_email(config, title, email_body)
        except Exception as e:
            failures.append(f'邮件: {e}')
    if config.get('enable_serverchan'):
        try:
            send_serverchan(config, title, wx_body)
        except Exception as e:
            failures.append(f'Server酱: {e}')
    if failures:
        raise RuntimeError('通知发送失败；' + '；'.join(failures))


def _esc(s):
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def detect_status_change(journal, manuscript_id, old, current):
    """Return a reportable status change, ignoring volatile display metadata.

    Decision-letter text can gain or lose cookie notices and other page chrome on
    otherwise identical fetches.  Keep the latest text in the cache/report, but
    do not present those formatting differences as a manuscript status change.
    """
    status_changed = old.get('status') != current['status']
    date_changed = old.get('status_date') != current.get('status_date', '')
    disposition_changed = old.get('disposition', '') != current.get('disposition', '')
    if not (status_changed or date_changed or disposition_changed):
        return None

    return {
        'journal': journal,
        'id': manuscript_id,
        'old_status': old.get('status', ''),
        'new_status': current['status'],
        'old_date': old.get('status_date', ''),
        'new_date': current.get('status_date', ''),
        'old_disposition': old.get('disposition', ''),
        'new_disposition': current.get('disposition', ''),
    }


def notify(config, articles, changes):
    if config['notify_only_on_change'] and not changes:
        log('状态无变化，跳过通知')
        return

    now = now_cn().strftime('%Y-%m-%d %H:%M（北京时间）')

    def fmt_change(c):
        parts = []
        if c['old_status'] != c['new_status']:
            parts.append(f'状态: {c["old_status"]} → {c["new_status"]}')
        if c['old_date'] != c['new_date']:
            parts.append(f'日期: {c["old_date"]} → {c["new_date"]}')
        if c.get('old_disposition', '') != c.get('new_disposition', ''):
            parts.append(
                f'决定: {c.get("old_disposition", "") or "无"} → '
                f'{c.get("new_disposition", "") or "无"}'
            )
        return '；'.join(parts) if parts else '有更新'

    if changes:
        title = f'[期刊追踪] 状态变化! {now}'
        change_html = ''.join(
            f'<li><b>{journal_label(c["journal"])} | {c["id"]}</b><br>'
            f'<span style="color:red">{fmt_change(c)}</span></li>' for c in changes)
        change_md = '\n'.join(
            f'- **{journal_label(c["journal"])} | {c["id"]}**\n  {fmt_change(c)}' for c in changes)
    else:
        title = f'[期刊追踪] 定期报告 {now}'
        change_html = '<li>无状态变化</li>'
        change_md = '无状态变化'

    def render_config_summary():
        journals = enabled_journals(config)
        rows = []
        md_lines = []
        for j in journals:
            platform = j.get('platform', 'editorialmanager')
            url = j.get('url', '')
            rows.append(
                '<tr>'
                f'<td>{_esc(journal_label(j["name"]))}</td>'
                f'<td>{_esc(platform)}</td>'
                f'<td><a href="{_esc(url)}">{_esc(url)}</a></td>'
                f'<td>{_esc(credential_source_label(j, config, "username"))}</td>'
                '</tr>'
            )
            md_lines.append(
                f'- **{journal_label(j["name"])}** ({platform})\n'
                f'  {url}\n'
                f'  账号: {credential_source_label(j, config, "username")}'
            )

        active_names = {j['name'].upper() for j in journals}
        ignored = []
        for item in _as_list(config.get('ignore_ids')):
            journal_prefix, separator, _ = item.partition('::')
            if separator and journal_prefix.upper() not in active_names:
                continue
            ignored.append(item)
        ignored_html = ''.join(f'<li><code>{_esc(x)}</code></li>' for x in ignored) or '<li>无</li>'
        ignored_md = '\n'.join(f'- `{x}`' for x in ignored) if ignored else '- 无'

        template = """方案 A：手动配置模板

1) 新增网站/期刊
打开 journals_config.json，在 journals 里复制一段并修改：
{
  "name": "期刊代号",
  "full": "期刊全名",
  "enabled": true,
  "platform": "editorialmanager 或 scholarone",
  "url": "投稿系统网址",
  "username_env": "期刊代号_USERNAME",
  "password_env": "期刊代号_PASSWORD"
}

然后到 GitHub -> Settings -> Secrets and variables -> Actions -> New repository secret：
Name: 期刊代号_USERNAME
Secret: 登录账号

Name: 期刊代号_PASSWORD
Secret: 登录密码

2) 修改账号/密码
到 GitHub Actions Secrets，点对应 secret 右侧铅笔：
JOURNAL_USERNAME / JOURNAL_PASSWORD
IJCHM_USERNAME / IJCHM_PASSWORD
或新增期刊自己的 *_USERNAME / *_PASSWORD

3) 删除/隐藏某篇稿件
打开 journals_config.json，在 ignore_ids 中加入：
"期刊代号::稿件号"
例："TEST::TEST-D-30-00001"

4) 停用/启用某个期刊
打开 journals_config.json：
"enabled": false  停用
"enabled": true   启用

5) 只临时监控部分期刊
到 GitHub -> Settings -> Secrets and variables -> Actions -> Variables：
JOURNALS_ENABLED = IJCHM,JCIT
留空则按 journals_config.json 的 enabled 设置监控。"""

        html = (
            '<hr><h4>⚙️ 当前监控配置</h4>'
            '<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px">'
            '<tr><th>期刊</th><th>平台</th><th>网址</th><th>账号来源</th></tr>'
            + ''.join(rows) +
            '</table>'
            '<h4>🙈 已隐藏稿件</h4><ul>' + ignored_html + '</ul>'
            '<h4>🛠 修改模板</h4>'
            f'<pre style="white-space:pre-wrap;background:#f6f8fa;border:1px solid #ddd;'
            f'padding:10px;border-radius:6px;font-family:Consolas,monospace;font-size:12px">'
            f'{_esc(template)}</pre>'
        )
        md = (
            '\n### ⚙️ 当前监控配置\n'
            + '\n'.join(md_lines) +
            '\n\n### 🙈 已隐藏稿件\n'
            + ignored_md +
            '\n\n### 🛠 修改模板\n'
            '新增网站：改 `journals_config.json` 的 `journals`，并到 GitHub Actions Secrets 新增 `*_USERNAME` / `*_PASSWORD`。\n'
            '修改账号密码：到 GitHub Actions Secrets 点对应 secret 右侧铅笔。\n'
            '隐藏稿件：把 `期刊代号::稿件号` 加入 `ignore_ids`。\n'
            '停用/启用期刊：把 `enabled` 改成 `false` / `true`。\n'
            '临时白名单：设置 GitHub Actions Variable `JOURNALS_ENABLED`，如 `IJCHM,JCIT`。\n'
        )
        return html, md

    def render_item_html(a):
        sdate = a.get('status_date', '')
        h = (f'<li><b>{a["id"]}</b> — {a["status"]}<br>'
             f'<small>状态日期: {sdate}</small><br>'
             f'<small>{a["title"]}</small>')
        dates = []
        if a.get('created'):
            dates.append(f'Created: {_esc(a["created"])}')
        if a.get('submitted'):
            dates.append(f'Submitted: {_esc(a["submitted"])}')
        if dates:
            h += f'<br><small>{"；".join(dates)}</small>'
        editors = []
        if a.get('ae'):
            editors.append(f'AE: {_esc(a["ae"])}')
        if a.get('eic'):
            editors.append(f'EIC: {_esc(a["eic"])}')
        if editors:
            h += f'<br><small>{"；".join(editors)}</small>'
        if a.get('letter'):
            h += ('<br><details style="margin-top:6px">'
                  '<summary style="cursor:pointer;display:inline-block;'
                  'background:#0a58ca;color:#fff;padding:5px 12px;border-radius:6px;'
                  'font-size:13px;font-weight:bold;list-style:none;user-select:none">'
                  '📨 决定信 Decision Letter（点击展开 / 收起）</summary>'
                  f'<pre style="white-space:pre-wrap;background:#f6f8fa;'
                  f'border:1px solid #ddd;padding:10px;border-radius:6px;margin-top:6px;'
                  f'font-family:Arial,sans-serif;font-size:13px">'
                  f'{_esc(a["letter"])}</pre></details>')
        return h + '</li>'

    def render_item_md(a):
        # 微信/方糖是静态 Markdown，无法做交互折叠按钮，故只放简短摘要，全文在邮件里
        sdate = a.get('status_date', '')
        m = f'- `{a["id"]}` **{a["status"]}**\n  状态日期: {sdate}\n'
        if a.get('letter'):
            snippet = ' '.join(a['letter'].split())[:300]
            m += f'  📨 决定信摘要：{snippet}…\n  （完整决定信见邮件）\n'
        return m

    def render_section(items):
        """按期刊分组渲染一组稿件，返回 (html, md)。"""
        groups = {}
        for a in items:
            groups.setdefault(a['journal'], []).append(a)
        html = md = ''
        for jname, arts in groups.items():
            label = journal_label(jname)
            html += f'<h4>{label}</h4><ul>' + ''.join(render_item_html(a) for a in arts) + '</ul>'
            md += f'\n**{label}**\n' + ''.join(render_item_md(a) for a in arts)
        return html, md

    # 进行中的稿件在前；已有最终决定（被拒/接收等）的稿件统一放到整封报告最后
    active = [a for a in articles if not is_closed(a)]
    closed = [a for a in articles if is_closed(a)]
    active_html, active_md = render_section(active)
    closed_html, closed_md = render_section(closed)

    body_html = active_html or ('<p>无进行中的稿件</p>' if closed_html else '<p>未找到稿件</p>')
    if closed_html:
        body_html += ('<hr><h4>✅ 已有最终决定（已结束）</h4>' + closed_html)
    body_md = active_md or ('（无进行中的稿件）\n' if closed_md else '未找到稿件')
    if closed_md:
        body_md += '\n### ✅ 已有最终决定（已结束）\n' + closed_md

    config_html, config_md = render_config_summary()

    email_body = f"""<html><body style="font-family:Arial,sans-serif;max-width:680px">
<h3>📋 期刊投稿状态报告</h3>
<p style="color:#666">查询时间：{now}（云端自动运行）</p><hr>
<h4>⚡ 状态变化</h4><ul>{change_html}</ul><hr>
<h4>📄 进行中的稿件</h4>{body_html}
{config_html}
</body></html>"""

    wx_body = f"## 📋 期刊投稿追踪报告\n> {now}\n\n### ⚡ 状态变化\n{change_md}\n\n### 📄 进行中的稿件\n{body_md}{config_md}"

    _dispatch_notifications(config, title, email_body, wx_body)


def send_alert(config, errors):
    """抓取异常时发告警，避免静默失败（如登录页改版导致一直抓不到）。"""
    now = now_cn().strftime('%Y-%m-%d %H:%M（北京时间）')
    lines = '\n'.join(f'- {k}: {v}' for k, v in errors.items())
    title = f'[期刊追踪] ⚠️ 抓取异常 {now}'
    html = (f'<html><body style="font-family:Arial,sans-serif">'
            f'<h3>⚠️ 期刊监控抓取失败</h3>'
            f'<p style="color:#666">时间：{now}</p>'
            f'<p>以下期刊本次未能抓取（可能是投稿系统改版/网络问题），请关注：</p>'
            f'<pre style="background:#f6f8fa;border:1px solid #ddd;padding:10px;border-radius:6px">'
            f'{_esc(lines)}</pre></body></html>')
    md = f'## ⚠️ 期刊监控抓取失败\n> {now}\n\n本次未能抓取的期刊：\n{lines}'
    log(f'发送抓取异常告警: {list(errors.keys())}')
    _dispatch_notifications(config, title, html, md)


# ============================================================
# 主函数
# ============================================================

async def main():
    log('=' * 50)
    if not enabled_journals(CONFIG):
        log('尚未启用期刊。请在 journals_config.json 中启用条目并配置登录环境变量。')
        return
    log(f'开始检查: {", ".join(j["name"] for j in enabled_journals(CONFIG))}')

    cache = json.loads(CACHE_FILE.read_text('utf-8')) if CACHE_FILE.exists() else {}
    articles, errors = await fetch_all(CONFIG)
    apply_status_overrides(CONFIG, articles)

    if errors:
        log(f'抓取异常: {errors}')

    if not articles:
        log('未找到任何稿件')
        # 全部抓取失败时发告警，避免静默无通知
        if errors:
            send_alert(CONFIG, errors)
        return

    changes, new_cache = [], {}
    for a in articles:
        key = f'{a["journal"]}::{a["id"]}'
        old = cache.get(key)
        if isinstance(old, str):
            old = {'status': old, 'status_date': ''}

        # ScholarOne sometimes places workflow/decision text in the TITLE
        # column. Do not overwrite a previously confirmed manuscript title
        # with that UI text.
        old_title = old.get('title', '') if old else ''
        old_title_is_workflow_text = bool(re.match(
            r'^(Awaiting |Submitted Manuscripts|Approval To Transfer|Reject(?:ed)?|Accept(?:ed)?|Major Revision|Minor Revision|Revision Required)',
            old_title,
            re.IGNORECASE,
        ))
        if old and not a.get('title') and not old_title_is_workflow_text:
            a['title'] = old_title

        new_cache[key] = {
            'status': a['status'],
            'status_date': a.get('status_date', ''),
            'title': a.get('title', ''),
            'created': a.get('created', ''),
            'submitted': a.get('submitted', ''),
            'disposition': a.get('disposition', ''),
            'letter': a.get('letter', ''),
            'ae': a.get('ae', ''),
            'eic': a.get('eic', ''),
            'manual_active': a.get('manual_active'),
        }
        if old is not None:
            change = detect_status_change(a['journal'], a['id'], old, a)
            if change:
                changes.append(change)

    # A journal portal can transiently return an empty list even after a
    # successful login (ScholarOne does this while a resubmission is moving
    # between queues). Preserve that journal's previous cache entries so one
    # empty response cannot erase its tracking history.
    enabled_names = {j['name'] for j in enabled_journals(CONFIG)}
    seen_names = {a['journal'] for a in articles}
    missing_names = enabled_names - seen_names
    for key, old in cache.items():
        journal_name = key.split('::', 1)[0]
        # Also retain journals excluded from this particular run. The local
        # IJCHM checker intentionally runs with JOURNALS_ENABLED=IJCHM and
        # must not erase cache entries maintained by the cloud job.
        if journal_name not in seen_names and key not in new_cache:
            new_cache[key] = old
    if missing_names:
        log(f'未返回稿件，保留历史缓存: {", ".join(sorted(missing_names))}')

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(new_cache, ensure_ascii=False, indent=2), 'utf-8')

    # Keep a missing journal visible in the periodic report using its most
    # recently confirmed cache entry. This is especially important when a
    # cloud runner is stopped by ScholarOne's human-verification page.
    report_articles = list(articles)
    for key, saved in new_cache.items():
        journal_name, separator, manuscript_id = key.partition('::')
        if not separator or journal_name not in missing_names:
            continue
        if isinstance(saved, str):
            saved = {'status': saved}
        report_articles.append({
            'journal': journal_name,
            'id': manuscript_id,
            'title': saved.get('title', ''),
            'status': saved.get('status', '最近状态未知'),
            'status_date': saved.get('status_date', ''),
            'created': saved.get('created', ''),
            'submitted': saved.get('submitted', ''),
            'disposition': saved.get('disposition', ''),
            'ae': saved.get('ae', ''),
            'eic': saved.get('eic', ''),
            'manual_active': saved.get('manual_active'),
            'category': 'Cached fallback',
            'letter': '',
            'cached_fallback': True,
        })

    log(f'共 {len(articles)} 篇稿件，{len(changes)} 处变化')
    for a in articles:
        log(f'  [{a["journal"]}] {a["id"]:22} | {a.get("status_date",""):28} | {a["status"]}')

    notify(CONFIG, report_articles, changes)
    log('检查完成')


if __name__ == '__main__':
    asyncio.run(main())
