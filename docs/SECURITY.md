# Security Policy

## Supported versions

Only the latest release on `main` is supported. There is no back-porting
of fixes to older versions — upgrade to get one.

## Reporting a vulnerability

Do **not** open a public GitHub issue for a security problem — DCCore
listens on IRC and, optionally, a web dashboard, both of which take
untrusted network input, so a public issue is a public exploit
announcement before a fix ships.

Instead, use GitHub's private vulnerability reporting for this repository:
the **Security** tab → **Report a vulnerability**. It opens a private
thread with the maintainers — nothing you write there is public until
everyone involved agrees it should be.

Include what you'd include in a bug report: what you did, what happened,
what you expected, and the version/commit you tested against. A proof of
concept is welcome and speeds things up.

## What to expect

This is a small, mostly one-person project — not a funded security team.
Reports are taken seriously and a fix is the priority once something is
confirmed, but there is no guaranteed response time. You'll get a reply
acknowledging the report, and credit in the changelog when the fix ships,
unless you'd rather stay anonymous.

## Scope

In scope: DCCore itself — `irc.py`, `dcc.py`, `dcc_fetch.py`, the web
dashboard, and anything else in this repository.

Out of scope: the IRC network or servers DCCore connects to, and
vulnerabilities that require an operator to have already misconfigured
the daemon against the documented setup in
[INSTALL.md](INSTALL.md) (for example, exposing the web dashboard —
which has no TLS — directly to the open internet).
