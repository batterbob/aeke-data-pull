#!/usr/bin/env python3
"""On-demand AEKE token-refresh window.

The AEKE token lasts ~30 days and its login can't be intercepted, so it must be
re-captured periodically. This script makes that nearly hands-off:

  1. brings mitmproxy up (reusing the persistent CA volume, so the phone's
     already-trusted cert keeps working),
  2. Telegrams the operator the exact steps (turn the phone Wi-Fi proxy on,
     log out/in of the app),
  3. watches the proxy for a token newer than the one in .env and writes it,
  4. tears the proxy down — immediately on capture, or after a time cap.

The proxy exists ONLY during this window, then is removed. Started by the daily
poller near expiry, or manually via deploy/refresh-now.sh.

Config comes from ../.env. Stdlib only; needs `docker` on PATH.
"""
import base64
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = os.path.join(ROOT, ".env")


def load_env():
    env = {}
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def jwt_payload(tok):
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def tg(env, text):
    bt = env.get("ALERT_TELEGRAM_BOT_TOKEN")
    cid = env.get("ALERT_TELEGRAM_CHAT_ID")
    if not (bt and cid):
        return
    data = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{bt}/sendMessage", data=data, timeout=15)
    except Exception:
        pass


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


class Mitmweb:
    """Minimal authenticated client for the mitmweb backend on this host."""

    def __init__(self, env):
        self.base = "http://127.0.0.1:%s" % env.get("MITM_WEB_PORT", "8082")
        self.pw = env.get("MITM_WEB_PASSWORD", "aeke")
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        # mitmweb serves its login page with HTTP 403 — read the body anyway
        # (cookies are still captured) to get the XSRF field, then POST the
        # password to obtain the auth cookie.
        page = self._read("/").decode("utf-8", "replace")
        m = re.search(r'name="_xsrf" value="([^"]+)"', page)
        if m:
            self._read("/", urllib.parse.urlencode({"token": self.pw, "_xsrf": m.group(1)}).encode())

    def _read(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            return self.op.open(req, timeout=15).read()
        except urllib.error.HTTPError as e:
            return e.read()

    def flows(self):
        try:
            return json.loads(self._read("/flows"))
        except Exception:
            return []

    def clear(self):
        xsrf = next((c.value for c in self.cj if c.name == "_mitmproxy_xsrf"), "")
        self._read("/clear", data=b"", headers={"X-XSRFToken": xsrf})


def newer_token(flows, baseline_ts):
    """Return the newest AEKE bearer token whose issue-time beats baseline."""
    best, best_ts = None, baseline_ts
    for f in flows:
        headers = {k.lower(): v for k, v in f["request"]["headers"]}
        a = headers.get("authorization", "")
        if "aeke" in f["request"]["host"] and a.count(".") == 2 and len(a) > 80:
            ts = jwt_payload(a).get("TIMESTAMP", 0)
            if ts > best_ts:
                best, best_ts = a, ts
    return best


def write_token(tok):
    s = open(ENV).read()
    s = re.sub(r"(?m)^AEKE_TOKEN=.*$", "AEKE_TOKEN=" + tok, s)
    open(ENV, "w").write(s)


def proxy_up(env):
    name = env.get("MITM_CONTAINER", "aeke-mitm-refresh")
    docker("rm", "-f", name)
    docker(
        "run", "-d", "--name", name, "--restart", "no",
        "-p", "%s:8080" % env.get("MITM_PROXY_PORT", "8081"),
        "-p", "%s:8081" % env.get("MITM_WEB_PORT", "8082"),
        "-v", "%s:/home/mitmproxy/.mitmproxy" % env.get("MITM_VOLUME", "/mnt/tank/apps/mitm"),
        "mitmproxy/mitmproxy", "mitmweb", "--web-host", "0.0.0.0",
        "--set", "web_password=%s" % env.get("MITM_WEB_PASSWORD", "aeke"),
    )
    time.sleep(6)


def proxy_down(env):
    try:
        Mitmweb(env).clear()  # purge captured traffic (privacy) before removing
    except Exception:
        pass
    docker("rm", "-f", env.get("MITM_CONTAINER", "aeke-mitm-refresh"))


def main():
    env = load_env()
    baseline = jwt_payload(env.get("AEKE_TOKEN", "")).get("TIMESTAMP", 0)
    window_min = float(env.get("REFRESH_WINDOW_MINUTES", "360"))
    poll_secs = float(env.get("REFRESH_POLL_SECS", "10"))
    host = env.get("MITM_LAN_HOST", "192.168.1.5")
    port = env.get("MITM_PROXY_PORT", "8081")

    proxy_up(env)
    tg(env, (
        "🔑 AEKE token refresh — expires soon. Takes ~1 minute:\n\n"
        f"1. iPhone → Settings → Wi-Fi → ⓘ → Configure Proxy → Manual → "
        f"Server {host}, Port {port} → Save. (Likely already saved — just switch it On.)\n"
        "2. Open the AEKE app, log out, then log back in.\n\n"
        "I'll grab the new token automatically and confirm here. "
        f"Window open for {int(window_min)} min."
    ))

    deadline = time.time() + window_min * 60
    got = None
    while time.time() < deadline:
        try:
            got = newer_token(Mitmweb(env).flows(), baseline)
            if got:
                break
        except Exception:
            pass
        time.sleep(poll_secs)

    if got:
        write_token(got)
        exp = jwt_payload(got).get("exp", 0) / 1000
        tg(env, (
            "✅ AEKE token refreshed — expires "
            + time.strftime("%Y-%m-%d", time.localtime(exp))
            + ". Turn the Wi-Fi proxy back Off (Settings → Wi-Fi → ⓘ → Configure Proxy → Off). "
            "Set for another ~30 days."
        ))
        proxy_down(env)
        return 0

    tg(env, ("⏳ AEKE refresh window closed — no new token seen. I'll reopen it on the next "
             "daily run. Make sure the Wi-Fi proxy is On, and log out/in of the app."))
    proxy_down(env)
    return 1


if __name__ == "__main__":
    sys.exit(main())
