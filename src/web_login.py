"""The dashboard's login (#1322): the login page, the per-address count of
failed passwords (per /64 and /48 network for IPv6, see _login_counts())
and the limit on logins in flight, the password check, and the checks
every request passes first - that it names this dashboard (DNS
rebinding), that no other page frames it, and that a login comes from
the dashboard's own origin.

Split out of webserver.py, step 6 of splitting it (docs/FUTURE.md).
create_app()'s request hooks and login route are still there and call
these through webserver, which imports every name back.
"""

import ipaddress
import time

from web_settings import dashboard_is_loopback_only
import adminchat
import defaults as config
import runtime


# ==========================================================================
# Flask app - only built/used when Flask is actually installed.
# ==========================================================================

# Self-contained on purpose: the login page must render before a session
# exists, so it cannot depend on web/style.css (that request would itself be
# behind the login it is trying to render) or on any operator data.
LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>DCCore Dashboard - Login</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: #0b0f12;
          color: #e6edf0; display: flex; align-items: center; justify-content: center;
          height: 100vh; margin: 0; }}
  form {{ background: #131a1f; padding: 2rem 2.25rem; border-radius: 10px;
          min-width: 260px; box-shadow: 0 4px 24px rgba(0,0,0,0.4); }}
  h1 {{ font-size: 1.05rem; margin: 0 0 1.25rem; font-weight: 600; }}
  input {{ width: 100%; padding: 0.55rem 0.6rem; margin-bottom: 1rem; box-sizing: border-box;
           background: #0b0f12; border: 1px solid #2a343b; border-radius: 6px; color: #e6edf0; }}
  button {{ width: 100%; padding: 0.55rem; background: #2dd4c8; color: #06231f; border: 0;
            border-radius: 6px; font-weight: 600; cursor: pointer; }}
  .error {{ color: #f87171; font-size: 0.85rem; margin: -0.75rem 0 1rem; }}
</style></head>
<body>
  <form method="post" action="/login">
    <h1>DCCore Dashboard</h1>
    {error_html}
    <input type="password" name="password" placeholder="Admin password" autofocus>
    <button type="submit">Log in</button>
  </form>
</body></html>"""


# Failed-login tracking for THIS route, keyed by _login_counts(): one IPv4
# address, or one IPv6 /64 network and its /48. Deliberately separate from
# adminchat.py's own _bad_ips pool even though the policy (attempt count,
# block duration) is identical and reused from there directly. The password
# is shared with the DCC CHAT admin console on purpose - but the block budget
# is not, because if it were, an attacker guessing at this HTTP form could
# spend down the same counter and lock the real operator out of the DCC
# console too. Same policy, separate pools: one abusive address costs it
# access to this route only.
_web_bad_ips = {}


# _login_counts() key -> how many of its logins are being checked right
# now. A login claims its attempt here BEFORE the password is hashed
# (_reserve_web_login) and settles it after (_settle_web_login), so
# parallel requests from one address cannot all pass the limit before any
# of them is counted.
_web_logins_in_flight = {}


# Bound from runtime.py, not built here: every lock lives there (see
# tests/test_no_reloaded_module_owns_a_lock.py and runtime.py's comment).
_web_bad_ips_lock = runtime.web_login_lock


# How long a login waits for one of runtime.web_password_checks' slots
# before it is told the dashboard is busy. Long enough that a few people
# logging in at once all get through; short enough that a flood is answered
# rather than queued for ever.
WEB_PASSWORD_CHECK_WAIT_SECONDS = 10.0


# How many dashboard sessions are kept open at once (see create_app()'s
# live_sessions); past it the oldest is logged out.
WEB_MAX_LIVE_SESSIONS = 100


# The prefix length an IPv6 address is counted under: one /64 is one
# network, the unit an ISP hands one line and SLAAC picks addresses from.
WEB_LOGIN_IPV6_PREFIX = 64

# A second, wider count for IPv6, on top of the /64 one: failures from
# anywhere in one /48 add up as well, and WEB_LOGIN_WIDE_ATTEMPTS of them
# block the whole /48 for adminchat.BAD_IP_BLOCK_SECONDS, the same way
# MAX_PASSWORD_ATTEMPTS blocks one /64. IPv4 has no wide count. Neither
# is a setting; _login_counts() applies both.
WEB_LOGIN_IPV6_WIDE_PREFIX = 48
WEB_LOGIN_WIDE_ATTEMPTS = 30

# NAT64's well-known prefix (RFC 6052): the last 32 bits are the IPv4
# address the connection really comes from.
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def _client_address(ip):
    """`ip` as the address the login counts are taken from, or None when
    it is not an address.

    An IPv6 address that carries an IPv4 one - IPv4-mapped (::ffff:192.0.2.1,
    what a dual-stack bind reports for an IPv4 client), NAT64
    (64:ff9b::192.0.2.1), 6to4 (2002:c000:201::) or Teredo - is that IPv4
    address, so a client counts the same whichever way it arrives. Only
    text is read: ipaddress would also take an int or 16 bytes.
    """
    if not isinstance(ip, str) or not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.version == 4:
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    return address


def _network_key(address, prefix):
    """The IPv6 network of `prefix` bits `address` is in, as text. Built
    from the integer, so a zone id (fe80::1%eth0) never reaches the key."""
    return str(ipaddress.IPv6Network((int(address), prefix), strict=False))


def _login_counts(ip):
    """Every (key, limit) a dashboard login from `ip` is counted under.

    An IPv4 address (or an IPv6 address carrying one, see
    _client_address()) is one count of its own, limit MAX_PASSWORD_ATTEMPTS.
    An IPv6 address is two: its /64 ("2001:db8:1:2::/64") with the same
    limit, and its /48 ("2001:db8:1::/48") with WEB_LOGIN_WIDE_ATTEMPTS.
    One host can pick any address in its /64 for each connection (privacy
    addresses), so the address alone is not counted. Anything else that
    is not empty - a name - is one count under itself, as before; None and
    "" are no count at all, so a login without an address is not limited
    here.

    The pool functions below all take the client's address and work
    through this list, so every count is checked, claimed, settled and
    cleared together, in one step under _web_bad_ips_lock.
    """
    address = _client_address(ip)
    if address is None:
        return ((ip, adminchat.MAX_PASSWORD_ATTEMPTS),) if ip else ()
    if address.version == 4:
        return ((str(address), adminchat.MAX_PASSWORD_ATTEMPTS),)
    return ((_network_key(address, WEB_LOGIN_IPV6_PREFIX), adminchat.MAX_PASSWORD_ATTEMPTS),
            (_network_key(address, WEB_LOGIN_IPV6_WIDE_PREFIX), WEB_LOGIN_WIDE_ATTEMPTS))


def _login_key(ip):
    """The narrowest key the dashboard's login pools count `ip` under:
    an IPv4 address as it is, an IPv6 address as its /64 (zone id dropped),
    an IPv6 address carrying an IPv4 one as that IPv4 address. Anything
    that is not an address - None, "", a name - is returned as it is.

    Idempotent: a key passed back in is returned unchanged.
    """
    counts = _login_counts(ip)
    return counts[0][0] if counts else ip


def _note_bad_web_login_locked(ip, now):
    """Count one failure for `ip` under every key of _login_counts(). The
    caller holds _web_bad_ips_lock."""
    # The addresses that never reached a block are forgotten after the
    # block window (#677): the expiry below only ever saw blocked ones,
    # and on an internet-exposed bind the rest stayed for ever.
    adminchat.forget_stale_failures(_web_bad_ips, now)
    for key, limit in _login_counts(ip):
        entry = _web_bad_ips.get(key) or [0, 0.0, now]
        entry[0] += 1
        entry[2] = now
        if entry[0] >= limit:
            entry[1] = now + adminchat.BAD_IP_BLOCK_SECONDS
        _web_bad_ips[key] = entry


def _note_bad_web_login(ip):
    if not ip:
        return
    with _web_bad_ips_lock:
        _note_bad_web_login_locked(ip, time.time())


def _blocked_locked(key, now):
    """True while `key` is blocked; an expired block is forgotten, so a
    typo is not permanent. The caller holds _web_bad_ips_lock."""
    entry = _web_bad_ips.get(key)
    if not entry or not entry[1]:
        return False
    if now < entry[1]:
        return True
    del _web_bad_ips[key]
    return False


def _reserve_web_login(ip):
    """Claim one of `ip`'s login attempts before its password is checked.

    Returns "ok" (claimed - the caller MUST call _settle_web_login() once,
    whatever happens next), "blocked" (one of its counts is blocked) or
    "busy" (at one of its counts, the earlier failures plus the attempts
    still being checked already use up that count's limit).

    The check and the claim are one step under the pool's lock. They used
    to be apart: the login checked the address, hashed the password, and
    only then counted a failure, so N parallel requests from one address
    all passed the check and N guesses were verified where the limit allows
    MAX_PASSWORD_ATTEMPTS. Counting the attempts in flight closes that:
    however many arrive at once, at most the remaining allowance is hashed.

    `ip` is counted under every key of _login_counts(): an IPv6 address
    shares its allowance with the rest of its /64, and its /48's with the
    rest of the /48. A claim is taken at every key or at none.
    """
    counts = _login_counts(ip)
    with _web_bad_ips_lock:
        now = time.time()
        adminchat.forget_stale_failures(_web_bad_ips, now)
        if any(_blocked_locked(key, now) for key, _ in counts):
            return "blocked"
        for key, limit in counts:
            entry = _web_bad_ips.get(key)
            failed = entry[0] if entry else 0
            if failed + _web_logins_in_flight.get(key, 0) >= limit:
                return "busy"
        for key, _ in counts:
            _web_logins_in_flight[key] = _web_logins_in_flight.get(key, 0) + 1
        return "ok"


def _settle_web_login(ip, outcome):
    """Release the attempt _reserve_web_login() claimed for `ip`.

    `outcome` is True for the right password (the failures at every key of
    `ip` are forgotten), False for a wrong one (counted at every key, and
    blocking at its limit exactly as before) or None when the password was
    never checked (the dashboard was too busy) - released without counting.
    """
    counts = _login_counts(ip)
    if not counts:
        return
    with _web_bad_ips_lock:
        for key, _ in counts:
            left = _web_logins_in_flight.get(key, 0) - 1
            if left > 0:
                _web_logins_in_flight[key] = left
            else:
                _web_logins_in_flight.pop(key, None)
            if outcome is True:
                _web_bad_ips.pop(key, None)
        if outcome is False:
            _note_bad_web_login_locked(ip, time.time())


def _check_web_password(stored, supplied, wait=None):
    """adminchat.verify_password(), but never more than
    runtime.WEB_PASSWORD_CHECKS_AT_ONCE at a time across the dashboard.

    None when no slot came free within `wait` seconds: the password was
    not checked, and the caller says the dashboard is busy instead of
    counting anything against the address.
    """
    wait = WEB_PASSWORD_CHECK_WAIT_SECONDS if wait is None else wait
    if not runtime.web_password_checks.acquire(timeout=wait):
        return None
    try:
        return bool(adminchat.verify_password(stored, supplied))
    finally:
        runtime.web_password_checks.release()


def _is_bad_web_ip(ip):
    counts = _login_counts(ip)
    if not counts:
        return False
    with _web_bad_ips_lock:
        now = time.time()
        # Every key is looked at, so each expired block is forgotten.
        return any([_blocked_locked(key, now) for key, _ in counts])


def _clear_bad_web_ip(ip):
    with _web_bad_ips_lock:
        for key, _ in _login_counts(ip):
            _web_bad_ips.pop(key, None)


def _host_name(host_header):
    """The name half of a Host header: lower-cased, without the port, the
    IPv6 brackets or a trailing dot."""
    text = (host_header or "").strip().lower()
    if text.startswith("["):
        return text[1:].split("]", 1)[0]
    if text.count(":") == 1:
        text = text.rsplit(":", 1)[0]
    return text.rstrip(".")


def _configured_dashboard_names():
    """The host names the operator has said the dashboard answers to:
    WEBUI_ALLOWED_HOSTS, plus WEBUI_HOST itself when that is a name."""
    raw = getattr(config, "WEBUI_ALLOWED_HOSTS", []) or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    names = {_host_name(str(item)) for item in raw}
    names.add(_host_name(str(getattr(config, "WEBUI_HOST", "127.0.0.1") or "")))
    names.discard("")
    return names


# This machine's own names, looked up once: socket.getfqdn() can ask DNS.
_machine_name_cache = []


def _machine_names():
    """This computer's own host names - what somebody on the LAN types to
    reach a dashboard bound to every interface. Its short name, its full
    name and the short name's mDNS (.local) spelling."""
    if not _machine_name_cache:
        import socket
        names = set()
        for lookup in (socket.gethostname, socket.getfqdn):
            try:
                name = _host_name(lookup())
            except (OSError, UnicodeError):
                continue
            if name:
                names.add(name)
                names.add(name.split(".", 1)[0])
                names.add(name.split(".", 1)[0] + ".local")
        _machine_name_cache.append(frozenset(names))
    return _machine_name_cache[0]


def _dashboard_host_ok(host_header, bound=None):
    """Is `host_header` a name this dashboard answers to?

    DNS REBINDING. A page on evil.example can point its own name at
    127.0.0.1 (or at the machine's LAN address) after it has loaded. The
    browser then sends that page's requests to the dashboard as SAME-ORIGIN
    - the Origin check (_login_origin_ok) compares Origin with Host, and
    both say evil.example - so the page could spend the operator's own
    login attempts and lock them out, and with the password it would reach
    every route. What the browser cannot change is the Host header: it
    always carries the name the page was loaded from. So the dashboard
    answers only to names that cannot be an attacker's:

      * any IP address - a page loaded from an IP literal is the dashboard
        (or whatever else answers on that address) and no DNS is involved;
      * localhost;
      * WEBUI_ALLOWED_HOSTS and WEBUI_HOST itself when it is a name - the
        names the operator configured (a reverse proxy, a LAN DNS name);
      * this machine's own host names, when the dashboard is bound to
        something other than loopback (WEBUI_HOST = 0.0.0.0) - so a phone on
        the LAN that opens http://<computer name>:8420/ still works.

    WEBUI_ALLOWED_HOSTS = * turns the check off. The port is not compared:
    a router's port forward or a proxy may change it, and it is not what a
    rebinding page controls.
    """
    import ipaddress

    name = _host_name(host_header)
    if not name:
        return False
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    configured = _configured_dashboard_names()
    if "*" in configured or name in configured:
        return True
    if not dashboard_is_loopback_only(bound):
        return name in _machine_names()
    return False


def _refuse_framing(response):
    """No other page may show the dashboard inside a frame (clickjacking).

    A page on another port of the same host is the same SITE to a browser,
    so the SameSite=Lax session cookie goes with a framed dashboard, and a
    click lured onto a framed button arrives from the dashboard's own page
    - past the Origin check. X-Frame-Options for older browsers, CSP
    frame-ancestors for current ones; the dashboard never frames itself.
    """
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    return response


def _password_stamp(secret_key, stored_hash):
    """What a dashboard session records about the password it was opened
    with: an HMAC of ADMIN_PASSWORD_HASH under the app's own secret. A
    session whose stamp no longer matches the current hash is refused, so
    changing the password ends every session opened with the old one. The
    hash itself never goes into the cookie, which the browser can read."""
    import hashlib
    import hmac
    key = secret_key if isinstance(secret_key, bytes) else str(secret_key).encode("utf-8")
    message = ("dccore-dashboard-session:" + str(stored_hash or "")).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def _admin_password_hash():
    """ADMIN_PASSWORD_HASH as the operator configured it, read under
    runtime.config_reload_lock. A rehash's reload of defaults puts it back to
    "" for about a millisecond, until settings.conf is applied again (see that
    lock's comment). A request read in that window took its own session for
    one opened with a changed password and logged the operator out - most
    often the Settings page's own re-fetch right after a save - and a correct
    password checked there was refused and counted as a failure (#1433)."""
    with runtime.config_reload_lock:
        return getattr(config, "ADMIN_PASSWORD_HASH", "")


def _origin_netloc(url):
    """host[:port] of an Origin, Referer or Host value, lower-cased, without
    a default port - a browser writes neither Origin nor Host with :80 or
    :443, but a proxy or a hand-typed URL may, and the two must still agree."""
    from urllib.parse import urlsplit
    netloc = urlsplit((url or "").strip()).netloc.lower()
    for default in (":80", ":443"):
        if netloc.endswith(default):
            netloc = netloc[:-len(default)]
    return netloc


def _login_origin_ok(origin, referer, host):
    """False when a login POST was sent by a page on another site.

    The failed-attempt pool above is keyed on the address, and on the default
    loopback install the operator's browser and any hostile page open in it
    both arrive as 127.0.0.1: MAX_PASSWORD_ATTEMPTS cross-site POSTs with a
    wrong password would block the operator's own login for
    BAD_IP_BLOCK_SECONDS, repeatable for ever (#609). Every browser puts the
    sending page's origin in Origin on a cross-site POST (a sandboxed frame
    sends the literal "null"), an older one only in Referer; whichever is
    present is compared with the Host header the request itself carries, the
    way _setup_host_ok() guards /setup. A request with neither header (curl,
    the test client) is not a browser forwarding another site's form and
    passes: this is a guard against the lockout, not a second password."""
    source = origin or referer
    if not source:
        return True
    sender = _origin_netloc(source)
    return bool(sender) and sender == _origin_netloc("//" + (host or "").strip())
