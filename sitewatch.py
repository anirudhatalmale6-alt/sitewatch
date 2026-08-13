#!/usr/bin/env python3
"""
sitewatch - simple uptime + keyword monitor.

Checks each site for HTTP status and for a keyword that should be present in
the page body. Only notifies on a CHANGE of state, so a site that is down for
a week generates one email, not two thousand.

Config lives next to this script:
  sites.conf   one site per line:  url | keyword | optional-expected-status
  config.conf  KEY=value settings

Run from cron:
  */5 * * * * /opt/sitewatch/sitewatch.py >/dev/null 2>&1
"""

import html
import json
import os
import re
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
SITES_FILE = os.path.join(HERE, "sites.conf")
CONFIG_FILE = os.path.join(HERE, "config.conf")
STATE_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "sitewatch.log")

DEFAULTS = {
    "EMAIL_TO": "",
    "EMAIL_FROM": "sitewatch@localhost",
    "WEBHOOK_URL": "",
    "TIMEOUT": "20",
    "RETRIES": "2",
    "RETRY_WAIT": "10",
    "USER_AGENT": "sitewatch/1.0 (uptime monitor)",
    "LOG_KEEP_LINES": "5000",
    "THREADS": "12",
}


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def trim_log(keep):
    """Stop the log growing forever."""
    try:
        with open(LOG_FILE) as fh:
            lines = fh.readlines()
        if len(lines) > keep:
            with open(LOG_FILE, "w") as fh:
                fh.writelines(lines[-keep:])
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        log("no config.conf found, using defaults")
    return cfg


def load_sites():
    sites = []
    try:
        with open(SITES_FILE) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or raw.startswith("#"):
                    continue
                parts = [p.strip() for p in raw.split("|")]
                url = parts[0]
                keyword = parts[1] if len(parts) > 1 and parts[1] else None
                expect = int(parts[2]) if len(parts) > 2 and parts[2] else 200
                # 4th column "offsite-ok" allows a deliberate redirect to a
                # different domain (one brand pointing at another).
                flags = parts[3].lower() if len(parts) > 3 else ""
                sites.append({
                    "url": url,
                    "keyword": keyword,
                    "expect": expect,
                    "offsite_ok": "offsite-ok" in flags,
                })
    except FileNotFoundError:
        log("ERROR: %s not found" % SITES_FILE)
    return sites


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def registrable(host):
    """crude 'same site' comparison - last two labels, or three for co.uk style."""
    parts = (host or "").lower().rstrip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def resolves(host):
    """
    Resolve as an ABSOLUTE name (trailing dot).

    Without the trailing dot the resolver appends whatever is in the 'search'
    line of /etc/resolv.conf. On a Plesk box that is <ip>.plesk.page, which is
    a wildcard pointing at the box itself - so a domain whose DNS has lapsed
    resolves to this server and answers 200. That would report a dead site as
    healthy, which is the exact failure this tool exists to catch.
    """
    try:
        socket.getaddrinfo(host + ".", None)
        return True
    except socket.gaierror:
        return False
    except Exception:
        return True  # don't let an odd resolver error create a false outage


def check_once(site, cfg):
    """Return (ok, detail, elapsed_ms)."""
    host = urllib.parse.urlsplit(site["url"]).hostname
    if host and not resolves(host):
        return False, "DNS: %s does not resolve" % host, 0

    req = urllib.request.Request(
        site["url"], headers={"User-Agent": cfg["USER_AGENT"]}
    )
    # Deliberately lenient TLS: an expired cert should be reported as a warning
    # by whoever owns the cert, not turn into a false "site is down".
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    started = time.time()
    try:
        with urllib.request.urlopen(
            req, timeout=int(cfg["TIMEOUT"]), context=ctx
        ) as resp:
            body = resp.read(400000).decode("utf-8", "replace")
            code = resp.getcode()
            final_host = urllib.parse.urlsplit(resp.geturl()).hostname
            elapsed = int((time.time() - started) * 1000)
            # bare -> www is fine; landing on a different site is not
            # (parked domain, hijacked DNS, expired registration)
            if (host and final_host and not site.get("offsite_ok")
                    and registrable(final_host) != registrable(host)):
                return (
                    False,
                    "redirected off-site to %s" % final_host,
                    elapsed,
                )
    except urllib.error.HTTPError as exc:
        elapsed = int((time.time() - started) * 1000)
        return False, "HTTP %s" % exc.code, elapsed
    except Exception as exc:  # socket errors, DNS, timeouts, TLS
        elapsed = int((time.time() - started) * 1000)
        return False, "%s" % (str(exc)[:120] or exc.__class__.__name__), elapsed

    if code != site["expect"]:
        return False, "HTTP %s (wanted %s)" % (code, site["expect"]), elapsed

    if site["keyword"]:
        # strip tags so a keyword split by markup still matches, and decode
        # entities so a keyword containing & or ' matches "&amp;" / "&#039;"
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
        text = re.sub(r"\s+", " ", text)
        if site["keyword"].lower() not in text.lower():
            return False, 'keyword "%s" missing' % site["keyword"], elapsed

    return True, "HTTP %s" % code, elapsed


def check(site, cfg):
    """Check with retries, so one dropped packet is not an outage."""
    attempts = int(cfg["RETRIES"]) + 1
    last = ("", 0)
    for i in range(attempts):
        ok, detail, ms = check_once(site, cfg)
        if ok:
            return True, detail, ms
        last = (detail, ms)
        if i < attempts - 1:
            time.sleep(int(cfg["RETRY_WAIT"]))
    return False, last[0], last[1]


def send_email(cfg, subject, body):
    if not cfg["EMAIL_TO"]:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["EMAIL_FROM"]
    msg["To"] = cfg["EMAIL_TO"]
    msg.set_content(body)

    # Preferred: authenticated relay on 587.
    # Most hosts (IONOS included) block outbound port 25 on new servers, so a
    # local MTA just queues the mail forever and reports success. Relaying
    # through a real mailbox also gets us SPF/DKIM, so alerts don't go to spam.
    if cfg.get("SMTP_HOST"):
        try:
            port = int(cfg.get("SMTP_PORT") or 587)
            if port == 465:
                srv = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, timeout=30)
            else:
                srv = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=30)
                srv.starttls(context=ssl.create_default_context())
            if cfg.get("SMTP_USER"):
                srv.login(cfg["SMTP_USER"], cfg.get("SMTP_PASS", ""))
            srv.send_message(msg)
            srv.quit()
            log("  notified by email -> %s (relay)" % cfg["EMAIL_TO"])
            return
        except Exception as exc:
            log("  SMTP RELAY FAILED (%s)" % str(exc)[:100])

    # Fallback: local MTA. Only works if outbound 25 is actually open.
    try:
        subprocess.run(
            ["/usr/sbin/sendmail", "-t", "-oi"],
            input=msg.as_bytes(),
            check=True,
            timeout=30,
        )
        log("  handed to local mail server -> %s "
            "(NOTE: only arrives if outbound port 25 is open)" % cfg["EMAIL_TO"])
    except Exception as exc:
        log("  EMAIL FAILED (%s)" % str(exc)[:80])


def send_webhook(cfg, text):
    url = cfg["WEBHOOK_URL"]
    if not url:
        return
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log("  notified by webhook")
    except Exception as exc:
        log("  WEBHOOK FAILED (%s)" % str(exc)[:80])


def notify(cfg, going_down, url, detail, since=None):
    if going_down:
        subject = "DOWN: %s" % url
        body = "%s looks down.\n\nReason: %s\nChecked: %s\n" % (
            url,
            detail,
            time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
    else:
        mins = int((time.time() - since) / 60) if since else 0
        subject = "RECOVERED: %s" % url
        body = "%s is back up after about %s minutes.\n\nNow: %s\nChecked: %s\n" % (
            url,
            mins,
            detail,
            time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
    send_email(cfg, subject, body)
    send_webhook(cfg, subject + " - " + detail)


def main():
    cfg = load_config()
    sites = load_sites()
    if not sites:
        log("nothing to check")
        return 1

    state = load_state()
    changed = False

    # Check concurrently - a hundred sites one at a time would not finish
    # inside a 5 minute cron slot, especially once retries kick in.
    workers = max(1, min(int(cfg["THREADS"]), len(sites)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: check(s, cfg), sites))

    for site, (ok, detail, ms) in zip(sites, results):
        url = site["url"]
        prev = state.get(url, {})
        was_ok = prev.get("ok", True)

        log("%-7s %-45s %s (%sms)" % ("OK" if ok else "DOWN", url, detail, ms))

        if ok != was_ok:
            changed = True
            notify(cfg, going_down=not ok, url=url, detail=detail,
                   since=prev.get("since"))
            state[url] = {"ok": ok, "since": time.time(), "detail": detail}
        else:
            state[url] = {
                "ok": ok,
                "since": prev.get("since", time.time()),
                "detail": detail,
            }

    save_state(state)
    trim_log(int(cfg["LOG_KEEP_LINES"]))
    if not changed:
        log("no state changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
