# sitewatch

Small uptime + keyword monitor. No dependencies beyond Python 3 stdlib.

Checks each site for HTTP status **and** for a keyword that should appear in the
page. Only emails on a *change* of state, so a site down for a week sends one
alert, not two thousand.

## Why the keyword matters

A plain status check calls a site healthy if it returns 200. That misses the
failures that actually matter: a blank page, a "Error establishing a database
connection", a defaced page, or a WordPress white screen. All of those return
200. The keyword is a word you know appears on a working page - if it vanishes,
something is wrong even though the server is answering.

## Install

    mkdir -p /opt/sitewatch
    cp sitewatch.py sites.conf config.conf /opt/sitewatch/
    chmod 750 /opt/sitewatch/sitewatch.py

Cron, every 5 minutes:

    # /etc/cron.d/sitewatch
    MAILTO=""
    */5 * * * * root /opt/sitewatch/sitewatch.py >/dev/null 2>&1

## sites.conf

    url | keyword | expected status (default 200)

    https://example.com/        | Welcome
    https://shop.example.com/   | Add to basket | 200
    https://old.example.com/    |               | 301

Leave the keyword blank to check status only. Edit any time, no restart needed.

## config.conf

Notifications go by email, webhook, or both.

**Read this before relying on email.** Most hosts block outbound port 25 on new
servers. If they do, a local mail server accepts the message, queues it, and
never delivers it - and nothing looks broken. Either set `SMTP_HOST` to an
authenticated relay on port 587 (also gets you SPF/DKIM so alerts don't land in
spam), or use a webhook, which goes over 443 and always works.

## Two things it deliberately gets right

**DNS is resolved as an absolute name.** Without a trailing dot the resolver
appends the `search` domain from `/etc/resolv.conf`. On a Plesk box that is a
wildcard pointing at the box itself, so a domain whose registration has lapsed
resolves to the monitoring server and answers 200 - reporting a dead site as
healthy. That is the exact failure this tool exists to catch, so it is checked
explicitly.

**Off-site redirects are treated as failures.** Bare-to-www is fine. Landing on
a different registrable domain means a parked, expired or hijacked domain, and
is reported as down.

## Files

    sitewatch.py    the script
    sites.conf      what to check
    config.conf     where alerts go
    state.json      written automatically - current up/down state
    sitewatch.log   written automatically, trimmed to 5000 lines
