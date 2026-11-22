"""Smile One (Brazil) Mobile Legends client. Only logic, no Telegram code."""
import re
import json
import requests
from bs4 import BeautifulSoup

BASE = "https://www.smile.one"
PAGE = BASE + "/merchant/mobilelegends"

# Packages the bot may buy. Prices are read live from the page (Smile Coins).
# Limited "once per week/month" packs are deliberately left out.
PACKAGES = {
    "13": "Diamond x78 +8",
    "23": "Diamond x156 +16",
    "25": "Diamond x234 +23",
    "26": "Diamond x625 +81",
}

AJAX = {"Referer": PAGE, "Origin": BASE, "X-Requested-With": "XMLHttpRequest"}


class SmileError(Exception):
    pass


def make_session(cookies, user_agent):
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
    for c in cookies:
        d = c.get("domain") or ""
        if "smile.one" in d:
            s.cookies.set(c["name"], c["value"], domain=d, path=c.get("path", "/"))
    return s


def _visible_lines(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [re.sub(r"\s+", " ", x).strip() for x in soup.get_text("\n").splitlines()]
    return [x for x in lines if x]


def inspect(s):
    """Returns (code, balance). code: ok / expired / challenge / unknown."""
    r = s.get(BASE + "/customer/order", timeout=20)
    low = r.text.lower()
    if "just a moment" in low:
        return "challenge", None
    if "account/login" in r.url:
        return "expired", None
    lines = _visible_lines(r.text)
    for i, x in enumerate(lines):
        if x.lower().startswith("saldo") and i + 1 < len(lines):
            try:
                return "ok", float(lines[i + 1].replace(",", "."))
            except ValueError:
                break
    return "unknown", None


def get_balance(s):
    code, bal = inspect(s)
    if code != "ok":
        raise SmileError("Session problem: " + code)
    return bal


def get_page(s):
    """Fresh MLBB page -> (csrf, {package_id: price_in_smile_coins})."""
    r = s.get(PAGE, timeout=20)
    if "just a moment" in r.text.lower():
        raise SmileError("Cloudflare challenge (site rejected this session/IP)")
    if "account/login" in r.url:
        raise SmileError("Session expired")
    el = BeautifulSoup(r.text, "html.parser").find("input", {"name": "_csrf"})
    if not el:
        raise SmileError("csrf not found (session expired or page changed)")
    prices = {}
    m = re.search(r"info\s*=\s*JSON\.parse\('(.*?)'\)", r.text, re.S)
    if m:
        try:
            info = json.loads(m.group(1).encode().decode("unicode_escape"))
            for pid in PACKAGES:
                prices[pid] = float(info[pid]["smilecoin"]["total_amount"])
        except Exception:
            pass
    return el["value"], prices


def _ajax(s, path, csrf, uid, zone, pid, checkrole):
    r = s.post(BASE + path, headers=AJAX, timeout=20, data={
        "user_id": uid, "zone_id": zone, "pid": pid, "checkrole": checkrole,
        "pay_methond": "smilecoin", "channel_method": "smilecoin", "_csrf": csrf})
    try:
        return r.json()
    except Exception:
        raise SmileError("Unexpected reply from site: " + r.text[:100])


def check_role(s, csrf, uid, zone, pid):
    """Read-only. Returns the in-game name."""
    j = _ajax(s, "/merchant/mobilelegends/checkrole", csrf, uid, zone, pid, "1")
    if str(j.get("code")) != "200":
        raise SmileError("Player check failed: " + str(j.get("info") or j))
    return j.get("username")


def purchase(s, uid, zone, pid):
    """ONE purchase attempt with Smile Coins. Returns (ok, detail).
    Never retry automatically: the caller must check the order page first."""
    csrf, _ = get_page(s)
    check_role(s, csrf, uid, zone, pid)
    s.post(BASE + "/merchant/customer", data={"check": "check", "_csrf": csrf},
           headers=AJAX, timeout=20)
    q = _ajax(s, "/merchant/mobilelegends/query", csrf, uid, zone, pid, "")
    if str(q.get("code")) != "200":
        raise SmileError("Order refused: " + str(q.get("info") or q))
    pr = s.post(BASE + "/merchant/mobilelegends/pay",
                headers={"Referer": PAGE, "Origin": BASE}, timeout=40,
                allow_redirects=True, data={
        "_csrf": csrf, "user_id": uid, "zone_id": zone,
        "pay_methond": "smilecoin", "product_id": pid, "channel_method": "smilecoin",
        "flowid": q.get("flowid"), "email": "", "coupon_id": ""})
    low = pr.text.lower()
    if any(k in low for k in ("captcha", "turnstile", "geetest")):
        return False, "verification page appeared - stopped, do it manually in the browser"
    if "/message/success" in pr.url:
        return True, "payment success page"
    return False, "unknown result (url: %s)" % pr.url
