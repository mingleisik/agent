#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from seleniumbase import SB


USER_ENV_FILE = str(Path.home() / ".config" / "browser-automation-panel" / "scripts.env")
TASK_RESULT_PATH = (os.environ.get("TASK_RESULT_PATH") or "").strip()
TASK_SCREENSHOT_PATH = (os.environ.get("TASK_SCREENSHOT_PATH") or "").strip()
SCRIPT_REVISION = "2026-06-24-github-actions"

SITE_URL = "https://agentrouter.org"
LOGIN_URL = "https://agentrouter.org/login"
WALLET_URL = "https://agentrouter.org/console/topup"
LOGIN_TEXT = "使用 GitHub 继续"
WAIT_AFTER_CLICK = 90.0
READY_WAIT = 2.0
USE_UC = False
TG_CHAT_ID = ""
TG_TOKEN = ""
TG_PROXY = ""


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def load_env_file(env_file_path: str) -> bool:
    path = Path(env_file_path)
    try:
        if not path.exists():
            log(f"env file not found: {env_file_path}")
            return False
        loaded_any = False
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded_any = True
        log(f"env file loaded: {env_file_path}")
        return loaded_any
    except Exception as exc:
        log(f"env file load failed: {env_file_path}: {exc}")
        return False


def refresh_config() -> None:
    global SITE_URL, LOGIN_URL, WALLET_URL, LOGIN_TEXT, WAIT_AFTER_CLICK, READY_WAIT, USE_UC
    global TG_CHAT_ID, TG_TOKEN, TG_PROXY

    SITE_URL = (os.environ.get("AGENTROUTER_SITE_URL") or "https://agentrouter.org").strip().rstrip("/")
    LOGIN_URL = (os.environ.get("AGENTROUTER_LOGIN_URL") or f"{SITE_URL}/login").strip()
    WALLET_URL = (os.environ.get("AGENTROUTER_WALLET_URL") or f"{SITE_URL}/console/topup").strip()
    LOGIN_TEXT = (os.environ.get("AGENTROUTER_LOGIN_TEXT") or "使用 GitHub 继续").strip()
    WAIT_AFTER_CLICK = float((os.environ.get("AGENTROUTER_WAIT_AFTER_CLICK") or "90").strip() or "90")
    READY_WAIT = float((os.environ.get("AGENTROUTER_READY_WAIT") or "2").strip() or "2")
    USE_UC = (os.environ.get("AGENTROUTER_USE_UC") or "0").strip().lower() in {"1", "true", "yes", "on"}
    TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
    TG_TOKEN = (
        os.environ.get("TG_BOT_TOKEN")
        or os.environ.get("TG_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or ""
    ).strip()
    TG_PROXY = (
        os.environ.get("TG_PROXY")
        or os.environ.get("TG_PROXY_URL")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
        or ""
    ).strip()


def host_from_url(url: str) -> str:
    return urlparse(url or "").netloc.lower()


def path_from_url(url: str) -> str:
    path = urlparse(url or "").path.rstrip("/")
    return path or "/"


def is_target_host(url: str) -> bool:
    host = host_from_url(url)
    target_host = host_from_url(SITE_URL)
    return bool(host and target_host and (host == target_host or host.endswith("." + target_host)))


def current_url_safe(sb: SB) -> str:
    try:
        return sb.get_current_url() or ""
    except Exception as exc:
        return f"<unavailable: {exc}>"


def normalize_socks_proxy(proxy: str) -> str:
    value = (proxy or "").strip()
    for prefix in ("socks5h://", "socks5://", "http://", "https://"):
        if value.lower().startswith(prefix):
            return value[len(prefix):]
    return value


def send_tg_message_via_curl(text: str) -> bool:
    if not TG_PROXY:
        return False
    proxy = normalize_socks_proxy(TG_PROXY)
    if not proxy:
        return False
    cmd = [
        "curl", "-sS", "--max-time", "25",
        "--socks5-hostname", proxy,
        "-X", "POST",
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        "--data-urlencode", f"chat_id={TG_CHAT_ID}",
        "--data-urlencode", f"text={text}",
        "--data-urlencode", "disable_web_page_preview=true",
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        body = (proc.stdout or "").strip()
        if '"ok":true' in body.replace(" ", ""):
            log(f"TG text push sent via curl+socks ({proxy})")
            return True
        log(f"TG curl response not ok: {body[:220]}")
    except Exception as exc:
        log(f"TG curl push failed: {exc}")
    return False


def send_tg_message(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        log("TG not configured, skipping text push")
        return
    message = (text or "").strip()
    if not message:
        return
    if send_tg_message_via_curl(message):
        return
    try:
        payload = urllib.parse.urlencode(
            {
                "chat_id": TG_CHAT_ID,
                "text": message,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        log("TG text push sent")
    except Exception as exc:
        log(f"TG text push failed: {exc}")


def build_tg_card(ok: bool, data: dict | None = None, error: str = "") -> str:
    data = data or {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "✅ 成功" if ok else "❌ 失败"
    lines = [
        "🤖 AgentRouter 签到通知",
        "",
        f"🕒 运行时间: {now_str}",
        f"📊 结果: {status}",
        f"💰 签到前余额: {data.get('balanceBeforeText') or '未读取'}",
        f"💵 签到后余额: {data.get('balanceAfterText') or '未读取'}",
        f"📈 余额变动: {data.get('balanceDeltaText') or '未读取'}",
        f"🧪 判定依据: {data.get('reason') or ('OK' if ok else 'FAILED')}",
    ]
    if data.get("url"):
        lines.append(f"🔗 最终页面: {data['url']}")
    if error:
        lines.append(f"⚠️ 异常: {error[:240]}")
    return "\n".join(lines)


def write_result(ok: bool, error: str | None = None, data: dict | None = None, screenshot_path: str | None = None) -> None:
    if not TASK_RESULT_PATH:
        return
    payload = {
        "ok": ok,
        "screenshotPath": screenshot_path or TASK_SCREENSHOT_PATH or None,
        "data": data or {},
    }
    if error:
        payload["error"] = error
    path = Path(TASK_RESULT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_screenshot(sb: SB, path: str | None = None) -> str | None:
    shot = path or TASK_SCREENSHOT_PATH
    if not shot:
        return None
    try:
        p = Path(shot)
        p.parent.mkdir(parents=True, exist_ok=True)
        sb.save_screenshot(str(p))
        return str(p)
    except Exception as exc:
        log(f"screenshot failed: {exc}")
        return None


def normalize_sb_proxy(proxy: str) -> str:
    value = proxy.strip()
    for prefix in ("socks5h://", "socks5://", "https://", "http://"):
        if value.lower().startswith(prefix):
            return value[len(prefix):]
    return value


def build_sb_args() -> dict:
    chrome_path = (os.environ.get("BROWSER_CHROME_PATH") or "").strip()
    user_data_dir = (os.environ.get("BROWSER_USER_DATA_DIR") or "").strip()
    proxy = (os.environ.get("BROWSER_PROXY") or "").strip()
    locale = (os.environ.get("BROWSER_LOCALE") or "").strip()

    args = {"test": True, "headed": True}
    if USE_UC:
        args["uc"] = True
    if chrome_path:
        args["binary_location"] = chrome_path
    if user_data_dir:
        args["user_data_dir"] = user_data_dir
    if proxy:
        args["proxy"] = normalize_sb_proxy(proxy)

    chromium_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if proxy:
        chromium_args.append(f"--proxy-server={proxy}")
    if locale:
        args["locale_code"] = locale
        chromium_args.append(f"--lang={locale}")
    args["chromium_arg"] = ",".join(chromium_args)
    return args


def patch_json_path(obj: dict, dotted_key: str, value) -> None:
    cur = obj
    parts = dotted_key.split(".")
    for key in parts[:-1]:
        next_obj = cur.get(key)
        if not isinstance(next_obj, dict):
            next_obj = {}
            cur[key] = next_obj
        cur = next_obj
    cur[parts[-1]] = value


def patch_json_file(path: Path, updates: dict[str, object]) -> bool:
    if not path.exists():
        return False
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return False
        for key, val in updates.items():
            patch_json_path(data, key, val)
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return True
    except Exception as exc:
        log(f"patch json failed: {path}: {exc}")
        return False


def normalize_profile_crash_state(user_data_dir: str) -> None:
    if not user_data_dir:
        return
    base = Path(user_data_dir)
    files = [
        (base / "Default" / "Preferences", {"profile.exit_type": "Normal", "profile.exited_cleanly": True}),
        (base / "Local State", {"profile.exit_type": "Normal", "profile.exited_cleanly": True}),
    ]
    patched = sum(1 for file_path, updates in files if patch_json_file(file_path, updates))
    log(f"profile crash-state patched files: {patched}")


def cleanup_profile_locks(user_data_dir: str) -> None:
    if not user_data_dir:
        return
    base = Path(user_data_dir)
    lock_files = [
        base / "SingletonLock",
        base / "SingletonCookie",
        base / "SingletonSocket",
        base / "Default" / "SingletonLock",
    ]
    removed = 0
    for lock in lock_files:
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
                removed += 1
        except Exception as exc:
            log(f"lock cleanup failed: {lock}: {exc}")
    log(f"profile lock files removed: {removed}")


def dismiss_chrome_crash_prompt() -> None:
    try:
        subprocess.run(["xdotool", "key", "Escape"], check=True)
        time.sleep(0.2)
        subprocess.run(["xdotool", "key", "Escape"], check=True)
        log("crash prompt dismiss keys sent")
    except Exception as exc:
        log(f"crash prompt dismiss skipped: {exc}")


def open_url(sb: SB, url: str, label: str) -> None:
    log(f"open {label}: {url}")
    sb.open(url)
    time.sleep(READY_WAIT)
    log(f"{label} URL: {current_url_safe(sb)}")


def browser_fetch_json(sb: SB, path: str, timeout: int = 15) -> dict:
    sb.driver.set_script_timeout(timeout)
    return sb.driver.execute_async_script(
        """
        const path = arguments[0];
        const done = arguments[arguments.length - 1];
        fetch(path, {
          method: 'GET',
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { 'Accept': 'application/json' }
        }).then(async (resp) => {
          const text = await resp.text();
          let body = null;
          try { body = JSON.parse(text); } catch (_) {}
          done({ ok: true, status: resp.status, url: resp.url, body, text });
        }).catch((err) => {
          done({ ok: false, error: String(err) });
        });
        """,
        path,
    )


def is_waf_text(text: str) -> bool:
    value = str(text or "")
    return "CF_APP_WAF" in value or "为了更好的访问体验，请进行验证" in value or "AliyunCaptcha" in value


def logout_via_api(sb: SB) -> None:
    if not is_target_host(current_url_safe(sb)):
        open_url(sb, SITE_URL, "site before logout")
    result = browser_fetch_json(sb, "/api/user/logout")
    body = result.get("body") if isinstance(result, dict) else None
    log(f"logout API status={result.get('status') if isinstance(result, dict) else 'unknown'} body={body}")
    if not (isinstance(body, dict) and body.get("success")):
        raise RuntimeError(f"logout API failed: {result}")
    time.sleep(1)


def locate_github_login_control(sb: SB) -> dict:
    result = sb.driver.execute_script(
        r"""
        const loginText = arguments[0];
        const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        const hrefOf = (el) => el.href || el.getAttribute('href') || '';
        const controls = Array.from(document.querySelectorAll('button,a,[role="button"]'));
        const candidates = [];
        for (const el of controls) {
          if (!visible(el)) continue;
          if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
          const text = textOf(el);
          const href = hrefOf(el);
          const hasGithubLogo = !!el.querySelector("img[aria-label='github_logo'], svg[class*='github'], .semi-icon-github_logo");
          const exactText = text === loginText;
          const githubText = /github/i.test(text) || text.includes('GitHub');
          const githubHref = /github/i.test(href);
          if (!exactText && !githubText && !githubHref && !hasGithubLogo) continue;
          const r = el.getBoundingClientRect();
          candidates.push({ el, text, href, hasGithubLogo, exactText, githubText, githubHref, area: Math.max(1, r.width * r.height) });
        }
        candidates.sort((a, b) => {
          const score = (item) =>
            (item.exactText ? 1000 : 0) +
            (item.githubText ? 500 : 0) +
            (item.githubHref ? 300 : 0) +
            (item.hasGithubLogo ? 100 : 0);
          return score(b) - score(a) || b.area - a.area;
        });
        const pick = candidates[0];
        if (!pick) {
          return { found: false, candidates: candidates.map((item) => ({ text: item.text, href: item.href })) };
        }
        const target = pick.el;
        target.scrollIntoView({ block: 'center', inline: 'center' });
        const r = target.getBoundingClientRect();
        const borderX = Math.max(0, ((window.outerWidth || 0) - (window.innerWidth || 0)) / 2);
        const topChrome = Math.max(0, (window.outerHeight || 0) - (window.innerHeight || 0) - borderX);
        return {
          found: true,
          text: textOf(target),
          href: hrefOf(target),
          viewportX: r.left + r.width / 2,
          viewportY: r.top + r.height / 2,
          screenX: Math.round((window.screenX || 0) + borderX + r.left + r.width / 2),
          screenY: Math.round((window.screenY || 0) + topChrome + r.top + r.height / 2)
        };
        """,
        LOGIN_TEXT,
    )
    return result if isinstance(result, dict) else {"found": False, "raw": result}


def webdriver_click_github_login(sb: SB) -> None:
    element = sb.driver.execute_script(
        r"""
        const loginText = arguments[0];
        const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        const controls = Array.from(document.querySelectorAll('button,a,[role="button"]'));
        return controls.find((el) => visible(el) && (textOf(el) === loginText || /github/i.test(textOf(el)))) || null;
        """,
        LOGIN_TEXT,
    )
    if not element:
        raise RuntimeError("GitHub login control not found for WebDriver click")
    element.click()


def click_github_login(sb: SB) -> None:
    deadline = time.time() + 20
    last_result = None
    while time.time() < deadline:
        last_result = locate_github_login_control(sb)
        if last_result.get("found"):
            break
        time.sleep(0.5)
    if not (isinstance(last_result, dict) and last_result.get("found")):
        raise RuntimeError(f"GitHub login control not found: {last_result}")

    log(f"GitHub login control: text={last_result.get('text')} href={last_result.get('href')}")
    try:
        x = str(int(last_result["screenX"]))
        y = str(int(last_result["screenY"]))
        log(f"xdotool click GitHub login: x={x} y={y}")
        subprocess.run(["xdotool", "mousemove", x, y], check=True, timeout=5)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], check=True, timeout=5)
        return
    except Exception as exc:
        log(f"xdotool GitHub click failed, fallback to WebDriver click: {exc}")
    webdriver_click_github_login(sb)


def page_text_sample(sb: SB, limit: int = 5000) -> str:
    try:
        return str(
            sb.driver.execute_script(
                "return (document.body && (document.body.innerText || document.body.textContent) || '').slice(0, arguments[0]);",
                int(limit),
            )
            or ""
        )
    except Exception:
        return ""


def parse_money_text(text: str) -> float | None:
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def read_balance_from_page(sb: SB) -> dict:
    try:
        payload = sb.driver.execute_script(
            r"""
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const text = norm(document.body && (document.body.innerText || document.body.textContent || ''));
            const labelIndex = text.indexOf('当前余额');
            const sample = labelIndex >= 0 ? text.slice(labelIndex, labelIndex + 120) : text.slice(0, 500);
            const match = sample.match(/\$\s*([0-9]+(?:\.[0-9]+)?)/) || text.match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
            return { balanceText: match ? match[0] : '', balanceAmount: match ? match[1] : '', sample };
            """
        )
        if isinstance(payload, dict) and payload.get("balanceText"):
            return payload
    except Exception as exc:
        log(f"read balance from page failed: {exc}")
    return {"balanceText": "", "balanceAmount": "", "sample": ""}


def open_wallet_and_read_balance(sb: SB) -> dict:
    open_url(sb, WALLET_URL, "wallet")
    deadline = time.time() + 25
    last_url = ""
    while time.time() < deadline:
        url = current_url_safe(sb)
        if url != last_url:
            log(f"wallet URL: {url}")
            last_url = url
        if path_from_url(url) == "/login":
            return {"loggedIn": False, "balanceText": "", "balanceAmount": ""}
        balance = read_balance_from_page(sb)
        if balance.get("balanceText"):
            balance["loggedIn"] = True
            log(f"balance page read: {balance.get('balanceText')}")
            return balance
        time.sleep(1)
    return {"loggedIn": is_logged_in_by_url(sb), "balanceText": "", "balanceAmount": ""}


def is_logged_in_by_url(sb: SB) -> bool:
    url = current_url_safe(sb)
    return is_target_host(url) and path_from_url(url).startswith("/console")


def switch_to_best_target_tab(sb: SB) -> None:
    try:
        handles = list(sb.driver.window_handles)
    except Exception:
        return
    best = None
    best_score = -1
    for handle in handles:
        try:
            sb.driver.switch_to.window(handle)
            url = current_url_safe(sb)
        except Exception:
            continue
        if not is_target_host(url):
            continue
        score = 1
        path = path_from_url(url)
        if path.startswith("/console") or path.startswith("/oauth"):
            score = 10
        elif path == "/login":
            score = 5
        if score > best_score:
            best = handle
            best_score = score
    if best:
        try:
            sb.driver.switch_to.window(best)
        except Exception:
            pass


def wait_for_login_success(sb: SB) -> None:
    deadline = time.time() + WAIT_AFTER_CLICK
    last_url = ""
    while time.time() < deadline:
        switch_to_best_target_tab(sb)
        url = current_url_safe(sb)
        if url != last_url:
            log(f"waiting login URL: {url}")
            last_url = url
        if is_logged_in_by_url(sb):
            log(f"login confirmed by console URL: {url}")
            return
        if is_waf_text(page_text_sample(sb)):
            raise RuntimeError("login flow hit WAF verification page")
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for login success; current={current_url_safe(sb)}")


def compute_result(data: dict) -> bool:
    before_num = parse_money_text(data.get("balanceBeforeText") or "")
    after_num = parse_money_text(data.get("balanceAfterText") or "")
    if before_num is not None and after_num is not None:
        delta = after_num - before_num
        data["balanceDelta"] = delta
        data["balanceDeltaText"] = f"{delta:+.2f}"
        if delta > 0:
            data["reason"] = f"签到成功，余额增加 {data['balanceDeltaText']}"
        elif delta == 0:
            data["reason"] = "今日可能已签到，余额未变化"
        else:
            data["reason"] = f"登录完成，但余额减少 {data['balanceDeltaText']}"
        return True
    if data.get("balanceAfterText"):
        data["balanceDeltaText"] = "N/A"
        data["reason"] = f"登录成功，当前余额 {data.get('balanceAfterText')}"
        return True
    data["reason"] = "未读取到登录后的余额"
    return False


def main() -> None:
    user_loaded = load_env_file(USER_ENV_FILE)
    env_file_from_var = (os.environ.get("AGENTROUTER_ENV_FILE") or "").strip()
    if env_file_from_var and env_file_from_var != USER_ENV_FILE:
        load_env_file(env_file_from_var)
    elif not user_loaded:
        log("no external env file loaded; using current process env only")
    refresh_config()

    profile_dir = (os.environ.get("BROWSER_USER_DATA_DIR") or "").strip()
    data = {
        "siteUrl": SITE_URL,
        "loginUrl": LOGIN_URL,
        "loginText": LOGIN_TEXT,
        "profileDir": profile_dir,
        "scriptRevision": SCRIPT_REVISION,
    }
    screenshot_path = None

    try:
        log("AgentRouter API-first check-in task started")
        log(f"SCRIPT_REVISION: {SCRIPT_REVISION}")
        log(f"SITE_URL: {SITE_URL}")
        log(f"LOGIN_URL: {LOGIN_URL}")
        log(f"AGENTROUTER_USE_UC: {int(USE_UC)}")
        log(f"BROWSER_USER_DATA_DIR: {profile_dir}")
        log(f"BROWSER_CHROME_PATH: {(os.environ.get('BROWSER_CHROME_PATH') or '').strip()}")
        normalize_profile_crash_state(profile_dir)
        cleanup_profile_locks(profile_dir)

        with SB(**build_sb_args()) as sb:
            log("browser started")
            dismiss_chrome_crash_prompt()
            open_url(sb, SITE_URL, "site")

            before_balance = open_wallet_and_read_balance(sb)
            data["startLoggedIn"] = bool(before_balance.get("loggedIn"))
            data["balanceBeforeText"] = before_balance.get("balanceText") or ""
            data["balanceBeforeAmount"] = before_balance.get("balanceAmount") or ""
            if data["startLoggedIn"]:
                log(f"already logged in, balance before: {data['balanceBeforeText'] or 'not found'}")
                logout_via_api(sb)
            else:
                log("session appears logged out")

            open_url(sb, LOGIN_URL, "login")
            if is_logged_in_by_url(sb):
                log("login URL redirected to logged-in session; logging out once more")
                logout_via_api(sb)
                open_url(sb, LOGIN_URL, "login after forced logout")

            click_github_login(sb)
            wait_for_login_success(sb)
            after_balance = open_wallet_and_read_balance(sb)
            data["balanceAfterText"] = after_balance.get("balanceText") or ""
            data["balanceAfterAmount"] = after_balance.get("balanceAmount") or ""
            data["url"] = current_url_safe(sb)
            screenshot_path = save_screenshot(sb)

        ok = compute_result(data)
        write_result(ok, error=None if ok else data.get("reason"), data=data, screenshot_path=screenshot_path)
        send_tg_message(build_tg_card(ok, data=data, error="" if ok else data.get("reason", "")))
        if not ok:
            raise RuntimeError(data.get("reason") or "check-in failed")
        log(f"check-in completed: {data.get('reason')}")
    except Exception as exc:
        error = str(exc)
        log(f"task failed: {error}")
        write_result(False, error=error, data=data, screenshot_path=screenshot_path)
        send_tg_message(build_tg_card(False, data=data, error=error))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
