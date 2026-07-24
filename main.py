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
SCRIPT_REVISION = "2026-07-24-github-2fa-tg-fix"

SITE_URL = "https://agentrouter.org"
LOGIN_URL = "https://agentrouter.org/login"
WALLET_URL = "https://agentrouter.org/console/topup"
LOGIN_TEXT = "使用 GitHub 继续"
WAIT_AFTER_CLICK = 180.0
READY_WAIT = 2.0
USE_UC = False
TG_CHAT_ID = ""
TG_TOKEN = ""
TG_PROXY = ""
GH_USERNAME = ""
GH_PASSWORD = ""
# 同一轮登录内避免 2FA 被 wait 循环反复触发（多次要码/重复填码）
_GH_2FA_HANDLED = False


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
    global TG_CHAT_ID, TG_TOKEN, TG_PROXY, GH_USERNAME, GH_PASSWORD

    SITE_URL = (os.environ.get("AGENTROUTER_SITE_URL") or "https://agentrouter.org").strip().rstrip("/")
    LOGIN_URL = (os.environ.get("AGENTROUTER_LOGIN_URL") or f"{SITE_URL}/login").strip()
    WALLET_URL = (os.environ.get("AGENTROUTER_WALLET_URL") or f"{SITE_URL}/console/topup").strip()
    LOGIN_TEXT = (os.environ.get("AGENTROUTER_LOGIN_TEXT") or "使用 GitHub 继续").strip()
    WAIT_AFTER_CLICK = float((os.environ.get("AGENTROUTER_WAIT_AFTER_CLICK") or "180").strip() or "180")
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
    GH_USERNAME = (os.environ.get("GH_USERNAME") or "").strip()
    GH_PASSWORD = (os.environ.get("GH_PASSWORD") or "").strip()


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


def _tg_proxy_curl_args() -> list[str]:
    """Build curl proxy args for Telegram API calls."""
    if not TG_PROXY:
        return []
    proxy = normalize_socks_proxy(TG_PROXY)
    if not proxy:
        return []
    # socks5:// / socks5h:// / bare host:port → socks5-hostname
    if TG_PROXY.lower().startswith("socks") or "://" not in TG_PROXY:
        return ["--socks5-hostname", proxy]
    return ["-x", proxy]


def send_tg_message_via_curl(text: str) -> bool:
    if not TG_PROXY:
        return False
    proxy_args = _tg_proxy_curl_args()
    if not proxy_args:
        return False
    cmd = [
        "curl", "-sS", "--max-time", "25",
        *proxy_args,
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
            log(f"TG text push sent via curl+proxy ({proxy_args[-1]})")
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
        log("TG text push sent via urllib")
    except Exception as exc:
        log(f"TG text push failed: {exc}")


def _tg_api_get(method: str, params: dict | None = None, timeout: int = 20) -> dict:
    """Call Telegram Bot API GET with curl(+proxy) first, urllib fallback."""
    params = params or {}
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    if qs:
        url = f"{url}?{qs}"

    # 1) curl (+ optional proxy) — preferred when TG_PROXY is set
    cmd = ["curl", "-sS", "--max-time", str(max(5, timeout))]
    cmd += _tg_proxy_curl_args()
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            data = json.loads(proc.stdout)
            if isinstance(data, dict):
                if data.get("ok") is False:
                    log(f"TG {method} curl ok=false: {str(data)[:220]}")
                return data
        err = (proc.stderr or proc.stdout or "").strip()
        if err:
            log(f"TG {method} curl failed rc={proc.returncode}: {err[:220]}")
    except Exception as exc:
        log(f"TG {method} curl exception: {exc}")

    # 2) urllib direct — same path that already works for sendMessage
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                if data.get("ok") is False:
                    log(f"TG {method} urllib ok=false: {str(data)[:220]}")
                return data
    except Exception as exc:
        log(f"TG {method} urllib failed: {exc}")
    return {}


def ensure_tg_getupdates_ready() -> bool:
    """
    Make getUpdates usable:
    - deleteWebhook so polling is not blocked by a leftover webhook
    - smoke-test getUpdates once and log the real error
    """
    if not TG_TOKEN:
        return False
    wh = _tg_api_get("deleteWebhook", {"drop_pending_updates": "false"}, timeout=15)
    if wh.get("ok"):
        log("TG deleteWebhook ok (getUpdates polling unlocked)")
    elif wh:
        log(f"TG deleteWebhook response: {str(wh)[:220]}")

    probe = _tg_api_get("getUpdates", {"timeout": 0, "limit": 1}, timeout=15)
    if probe.get("ok"):
        log("TG getUpdates probe ok")
        return True
    if probe:
        desc = ""
        if isinstance(probe.get("description"), str):
            desc = probe["description"]
        log(f"TG getUpdates probe failed: {desc or str(probe)[:220]}")
        if "webhook" in (desc or "").lower():
            log("HINT: bot 仍被 webhook 占用，请确认没有其他服务占用该 bot")
        if "conflict" in (desc or "").lower():
            log("HINT: 有其他 getUpdates 轮询在抢同一个 bot（本地脚本/另一个 Actions/面板）")
    else:
        log("TG getUpdates probe empty — 网络/代理可能不通")
    return False


def get_tg_updates(offset=None, long_poll: int = 5) -> dict:
    params = {"timeout": int(long_poll)}
    if offset is not None:
        params["offset"] = int(offset)
    # long poll 需要比 timeout 更长的 HTTP 超时
    return _tg_api_get("getUpdates", params, timeout=max(15, int(long_poll) + 10))


def _extract_code_from_update(upd: dict, chat_id: str) -> str:
    """Extract a 6-digit code from message / edited_message / channel_post."""
    for key in ("message", "edited_message", "channel_post"):
        msg = upd.get(key) or {}
        if not isinstance(msg, dict):
            continue
        chat = msg.get("chat") or {}
        if str(chat.get("id", "")) != str(chat_id):
            continue
        text = (msg.get("text") or msg.get("caption") or "").strip()
        # 优先整段就是 6 位；否则抓独立的 6 位数字
        m = re.fullmatch(r"\s*(\d{6})\s*", text)
        if m:
            return m.group(1)
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if m:
            return m.group(1)
    return ""


def tg_wait_code(timeout: int = 120) -> str:
    if not TG_TOKEN or not TG_CHAT_ID:
        log("TG not configured for 2FA code retrieval")
        return ""

    ensure_tg_getupdates_ready()
    log(f"等待 TG 两步验证码（{timeout}s），chat_id={TG_CHAT_ID}，请直接回复 6 位数字...")

    offset = None
    # 先消费积压，避免把旧消息当新码；同时定位最新 offset
    data = get_tg_updates(offset=None, long_poll=0)
    if data.get("ok") and data.get("result"):
        offset = data["result"][-1]["update_id"] + 1
        log(f"TG backlog drained, next offset={offset}, skipped={len(data['result'])}")
    elif not data.get("ok"):
        log(f"TG initial getUpdates not ok: {str(data)[:220]}")

    deadline = time.time() + timeout
    last_err_log = 0.0
    polls = 0
    while time.time() < deadline:
        polls += 1
        data = get_tg_updates(offset=offset, long_poll=5)
        if data.get("ok") and data.get("result"):
            for upd in data["result"]:
                offset = upd["update_id"] + 1
                code = _extract_code_from_update(upd, TG_CHAT_ID)
                if code:
                    log(f"TG 收到验证码（来自 update_id={upd.get('update_id')}）")
                    return code
                # 同 chat 但不是验证码：打日志方便排查“回了但没识别”
                for key in ("message", "edited_message"):
                    msg = upd.get(key) or {}
                    if str((msg.get("chat") or {}).get("id", "")) == str(TG_CHAT_ID):
                        preview = (msg.get("text") or "")[:80]
                        log(f"TG 收到消息但不是 6 位码: {preview!r}")
        elif data and not data.get("ok"):
            now = time.time()
            if now - last_err_log > 15:
                log(f"TG getUpdates error: {str(data)[:220]}")
                last_err_log = now
                if "conflict" in str(data).lower():
                    # 冲突时稍等，避免和另一个轮询死磕
                    time.sleep(3)
        elif not data:
            now = time.time()
            if now - last_err_log > 15:
                log("TG getUpdates empty response（网络？代理？）")
                last_err_log = now
        # long poll 已阻塞 ~5s，短睡即可
        time.sleep(0.5)

    log(f"TG 验证码等待超时（polls={polls}, offset={offset}）")
    return ""


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


def patch_json_file(path: Path, updates: dict) -> bool:
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
        return {
          found: true,
          text: textOf(target),
          href: hrefOf(target)
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


def _left_login_page(sb: SB, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "github.com" in current_url_safe(sb):
            return True
        try:
            for h in sb.driver.window_handles:
                sb.driver.switch_to.window(h)
                if "github.com" in current_url_safe(sb):
                    return True
        except Exception:
            pass
        if is_target_host(current_url_safe(sb)) and path_from_url(current_url_safe(sb)) != "/login":
            return True
        time.sleep(0.4)
    return False


def click_github_login(sb: SB) -> None:
    deadline = time.time() + 20
    last = None
    while time.time() < deadline:
        last = locate_github_login_control(sb)
        if last.get("found"):
            break
        time.sleep(0.5)
    if not (isinstance(last, dict) and last.get("found")):
        raise RuntimeError(f"GitHub login control not found: {last}")
    log(f"GitHub login control: text={last.get('text')} href={last.get('href')}")

    # 优先 WebDriver 原生点击（Xvfb 下最稳，不吃屏幕坐标）
    try:
        webdriver_click_github_login(sb)
        if _left_login_page(sb):
            log("login click via WebDriver ok")
            return
        log("WebDriver click didn't navigate, trying JS click")
    except Exception as exc:
        log(f"WebDriver click failed: {exc}")

    # 兜底：JS click
    try:
        sb.driver.execute_script(
            r"""
            const t = arguments[0];
            const vis = (el)=>!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
            const txt = (el)=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();
            const el = Array.from(document.querySelectorAll('button,a,[role="button"]'))
              .find(e=>vis(e)&&(txt(e)===t||/github/i.test(txt(e))));
            if (el) el.click();
            """,
            LOGIN_TEXT,
        )
        if _left_login_page(sb):
            log("login click via JS ok")
            return
    except Exception as exc:
        log(f"JS click failed: {exc}")

    raise RuntimeError(f"clicked GitHub login but stayed on /login; current={current_url_safe(sb)}")


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


def find_github_tab(sb: SB) -> str | None:
    try:
        handles = list(sb.driver.window_handles)
    except Exception:
        return None
    for h in handles:
        try:
            sb.driver.switch_to.window(h)
            if "github.com" in current_url_safe(sb):
                return h
        except Exception:
            continue
    return None


def _js_click_by_text(sb: SB, keywords: list[str]) -> str | None:
    """Click first visible link/button whose text/href matches any keyword. Returns matched text."""
    result = sb.driver.execute_script(
        r"""
        const keywords = (arguments[0] || []).map((k) => String(k).toLowerCase());
        const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        const hrefOf = (el) => el.href || el.getAttribute('href') || '';
        const nodes = Array.from(document.querySelectorAll('a,button,[role="button"],summary,input[type="submit"]'));
        for (const el of nodes) {
          if (!visible(el)) continue;
          if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
          const text = textOf(el);
          const href = hrefOf(el);
          const blob = (text + ' ' + href).toLowerCase();
          if (!keywords.some((k) => k && blob.includes(k))) continue;
          el.scrollIntoView({ block: 'center', inline: 'center' });
          el.click();
          return text || href || 'clicked';
        }
        return null;
        """,
        keywords,
    )
    return result if isinstance(result, str) and result else None


def switch_github_2fa_to_totp(sb: SB) -> bool:
    """
    GitHub 默认可能停在 webauthn / passkey / mobile。
    尽量切到 authentication app（TOTP 6 位码）页面。
    """
    url = current_url_safe(sb)
    log(f"2FA page before switch: {url}")

    # 已有 OTP 输入框就不用切
    for sel in (
        'input[name="app_otp"]',
        'input#app_totp',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
    ):
        try:
            if sb.is_element_visible(sel):
                log(f"2FA TOTP input already visible: {sel}")
                return True
        except Exception:
            pass

    # 常见入口：更多选项 / 使用验证器 App / 恢复码以外的 TOTP
    click_rounds = [
        ["two-factor/app", "authentication app", "authenticator app", "use an authenticator",
         "use authenticator", "验证器", "身份验证器", "身份验证应用程序", "使用身份验证应用程序"],
        ["two-factor/app", "app", "totp"],
        ["more options", "more 2fa options", "two-factor authentication options",
         "其他验证方式", "更多选项", "其他方式"],
        ["two-factor/app", "authentication app", "authenticator app", "use an authenticator",
         "验证器", "身份验证器", "身份验证应用程序"],
    ]
    for keywords in click_rounds:
        matched = None
        try:
            matched = _js_click_by_text(sb, keywords)
        except Exception as exc:
            log(f"2FA switch click failed: {exc}")
        if matched:
            log(f"2FA switch clicked: {matched!r}")
            time.sleep(2)
        url = current_url_safe(sb)
        # 直接跳 app 路径也试一次
        if "two-factor" in url and "two-factor/app" not in url and "webauthn" in url:
            try:
                app_url = url.replace("/two-factor/webauthn", "/two-factor/app")
                if app_url != url:
                    log(f"navigate 2FA app url: {app_url}")
                    sb.open(app_url)
                    time.sleep(2)
                    url = current_url_safe(sb)
            except Exception as exc:
                log(f"2FA app url navigate failed: {exc}")
        for sel in (
            'input[name="app_otp"]',
            'input#app_totp',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
        ):
            try:
                if sb.is_element_visible(sel):
                    log(f"2FA TOTP input visible after switch: {sel}, url={url}")
                    return True
            except Exception:
                pass

    # 最后兜底：硬跳 /sessions/two-factor/app
    if "two-factor" in current_url_safe(sb):
        try:
            log("2FA hard-open /sessions/two-factor/app")
            sb.open("https://github.com/sessions/two-factor/app")
            time.sleep(2)
        except Exception as exc:
            log(f"2FA hard-open failed: {exc}")
        for sel in (
            'input[name="app_otp"]',
            'input#app_totp',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
        ):
            try:
                if sb.is_element_visible(sel):
                    log(f"2FA TOTP input visible after hard-open: {sel}")
                    return True
            except Exception:
                pass

    log(f"2FA TOTP input not found; current={current_url_safe(sb)}")
    return False


def fill_github_totp_code(sb: SB, code: str) -> bool:
    code = (code or "").strip()
    if not code:
        return False
    selectors = [
        'input[name="app_otp"]',
        'input#app_totp',
        'input[autocomplete="one-time-code"]',
        'input#otp',
        'input[inputmode="numeric"]',
        'input[name="otp"]',
    ]
    filled = False
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                log(f"fill TOTP into {sel}")
                try:
                    sb.driver.execute_script(
                        "arguments[0].value=''; arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",
                        sb.driver.find_element("css selector", sel),
                    )
                except Exception:
                    pass
                sb.type(sel, code)
                filled = True
                break
        except Exception as exc:
            log(f"fill TOTP selector {sel} failed: {exc}")
    if not filled:
        # JS 兜底：找可见数字输入框
        try:
            ok = sb.driver.execute_script(
                r"""
                const code = arguments[0];
                const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
                const el = inputs.find((i) => {
                  const t = (i.type || '').toLowerCase();
                  const mode = (i.inputMode || i.getAttribute('inputmode') || '').toLowerCase();
                  const name = (i.name || i.id || i.autocomplete || '').toLowerCase();
                  return t === 'tel' || t === 'text' || t === 'number' || mode === 'numeric'
                    || name.includes('otp') || name.includes('totp') || name.includes('one-time');
                });
                if (!el) return false;
                el.focus();
                el.value = code;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
                """,
                code,
            )
            filled = bool(ok)
            if filled:
                log("fill TOTP via JS fallback ok")
        except Exception as exc:
            log(f"fill TOTP JS fallback failed: {exc}")
    if not filled:
        return False

    time.sleep(0.8)
    # 提交
    try:
        if sb.is_element_visible('button:contains("Verify")'):
            sb.click('button:contains("Verify")')
        elif sb.is_element_visible('button[type="submit"]'):
            sb.click('button[type="submit"]')
        elif sb.is_element_visible('input[type="submit"]'):
            sb.click('input[type="submit"]')
        else:
            sb.driver.execute_script(
                "const el=document.activeElement; if(el && el.form) el.form.submit();"
            )
        log("TOTP submit clicked")
    except Exception as exc:
        log(f"TOTP submit failed: {exc}")
    return True


def handle_github_login(sb: SB) -> None:
    global _GH_2FA_HANDLED
    url = current_url_safe(sb)
    if "github.com" not in url:
        return

    # 1. 账号密码
    try:
        login_visible = sb.is_element_visible('input[name="login"]')
    except Exception:
        login_visible = False
    if login_visible:
        if not GH_USERNAME or not GH_PASSWORD:
            log("WARNING: 缺少 GH_USERNAME / GH_PASSWORD，无法自动登录 GitHub")
            return
        log("填入 GitHub 账号密码...")
        sb.type('input[name="login"]', GH_USERNAME)
        time.sleep(0.5)
        sb.type('input[name="password"]', GH_PASSWORD)
        time.sleep(0.5)
        sb.click('input[type="submit"], button[type="submit"]')
        time.sleep(3)

    # 2. 设备验证
    url = current_url_safe(sb)
    if "verified-device" in url or "device-verification" in url:
        log("需要设备验证，等待批准（60s）...")
        send_tg_message("⚠️ GitHub 需要设备验证，请去邮箱/App 点击批准！等待 60 秒...")
        for i in range(60):
            time.sleep(1)
            u = current_url_safe(sb)
            if "verified-device" not in u and "device-verification" not in u:
                log("设备验证通过")
                send_tg_message("✅ 设备验证通过")
                break
            if i and i % 10 == 0:
                try:
                    sb.refresh_page()
                except Exception:
                    pass

    # 3. 两步验证（含 webauthn → TOTP 切换）
    url = current_url_safe(sb)
    if "two-factor" in url:
        if _GH_2FA_HANDLED:
            log("2FA already handled in this run, skip re-entry")
            return

        # GitHub Mobile 批准流
        if "two-factor/mobile" in url:
            send_tg_message("⚠️ GitHub Mobile 两步验证：请打开手机 App 批准本次登录，等待中...")
            for _ in range(45):
                time.sleep(2)
                if "two-factor" not in current_url_safe(sb):
                    _GH_2FA_HANDLED = True
                    return
            # mobile 等不到就尝试切到 TOTP
            log("GitHub Mobile 未及时批准，尝试切换到验证器 App 码")

        # webauthn / passkey / 其他 → 强制切 TOTP
        has_totp = switch_github_2fa_to_totp(sb)
        url = current_url_safe(sb)
        if "two-factor/mobile" in url and not has_totp:
            # 仍在 mobile 且没有输入框：继续等一会
            for _ in range(20):
                time.sleep(2)
                if "two-factor" not in current_url_safe(sb):
                    _GH_2FA_HANDLED = True
                    return
            log("仍停在 mobile 2FA，且无 TOTP 输入框")
            return

        if not has_totp:
            # 再尝试一次硬切
            has_totp = switch_github_2fa_to_totp(sb)
        if not has_totp:
            send_tg_message(
                f"⚠️ GitHub 停在 2FA 页面且未找到验证码输入框（可能是 passkey/webauthn）。"
                f"用户 {GH_USERNAME} url={current_url_safe(sb)}"
            )
            log(f"无法进入 TOTP 输入页: {current_url_safe(sb)}")
            # 标记已处理，避免外层循环反复轰炸 TG
            _GH_2FA_HANDLED = True
            return

        send_tg_message(
            f"🔐 需要 GitHub 两步验证码，用户 {GH_USERNAME} 正在登录，请直接回复 6 位数字："
        )
        code = tg_wait_code(timeout=120)
        if code:
            log("收到验证码，正在填入...")
            if fill_github_totp_code(sb, code):
                for _ in range(20):
                    time.sleep(1)
                    if "two-factor" not in current_url_safe(sb):
                        log("已离开 2FA 页面")
                        break
            else:
                log("验证码填入失败：页面上没有可用 OTP 输入框")
        else:
            log("未收到 TG 验证码")
        _GH_2FA_HANDLED = True
        return

    # 4. OAuth 授权页
    url = current_url_safe(sb)
    if "login/oauth/authorize" in url:
        log("OAuth 授权页，点击 Authorize...")
        for sel in ['button[name="authorize"]', 'button:contains("Authorize")']:
            try:
                if sb.is_element_visible(sel):
                    sb.click(sel)
                    time.sleep(3)
                    break
            except Exception:
                pass
        # 再试 JS 文本匹配
        try:
            matched = _js_click_by_text(sb, ["authorize", "授权"])
            if matched:
                log(f"OAuth authorize clicked via text: {matched!r}")
                time.sleep(2)
        except Exception:
            pass


def wait_for_login_success(sb: SB) -> None:
    global _GH_2FA_HANDLED
    _GH_2FA_HANDLED = False
    deadline = time.time() + WAIT_AFTER_CLICK
    last_url = ""
    while time.time() < deadline:
        # 发现 github 标签：切过去自动处理登录/2FA/授权
        gh = find_github_tab(sb)
        if gh:
            sb.driver.switch_to.window(gh)
            url = current_url_safe(sb)
            if url != last_url:
                log(f"github 流程: {url}")
                last_url = url
            try:
                handle_github_login(sb)
            except Exception as exc:
                log(f"handle_github_login 异常: {exc}")
            # 若仍停在 github 且已处理过 2FA，给一点时间跳转，不要空转耗尽
            time.sleep(1)
            continue

        # 没有 github 标签，回到目标站点看是否已进 console
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
        log("AgentRouter check-in task started")
        log(f"SCRIPT_REVISION: {SCRIPT_REVISION}")
        log(f"SITE_URL: {SITE_URL}")
        log(f"LOGIN_URL: {LOGIN_URL}")
        log(f"AGENTROUTER_USE_UC: {int(USE_UC)}")
        log(f"BROWSER_USER_DATA_DIR: {profile_dir}")
        log(f"BROWSER_CHROME_PATH: {(os.environ.get('BROWSER_CHROME_PATH') or '').strip()}")
        log(f"GH_USERNAME set: {bool(GH_USERNAME)} / GH_PASSWORD set: {bool(GH_PASSWORD)}")
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
