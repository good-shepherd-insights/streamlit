#!/usr/bin/env python3
"""streamctl - GSI Streamlit fleet controller (core).

CLI and HTTP API both wrap these functions. Every operational value comes
from streamctl.conf (or CONF_DEFAULTS here); functions hold no inline magic
values. Integration surfaces: gsictl registry.tsv + tunnels.tsv, systemd
template unit streamlit@.service (shipped in ./systemd/).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_CONF = Path("/etc/streamctl/streamctl.conf")
APPS_FILE = "apps.tsv"
APPS_HEADER = "name\tport\tsource\tpublic_url\tunit\n"
NAME_RE = r"[a-z0-9][a-z0-9-]*"
HEALTH_PATH = "/_stcore/health"
PUBLIC_URL_PLACEHOLDER = "-"


class StreamctlError(Exception):
    """Operator-visible failure, surfaced verbatim to CLI/API callers."""


# ---------- conf ----------


def load_conf() -> dict[str, str]:
    """Conf defaults; /etc/streamctl/streamctl.conf (or $STREAMCTL_CONF) overrides line-by-line."""
    conf = {
        "STREAMCTL_ROOT": "/home/dev/streamlit-apps",
        "STREAMCTL_CONFDIR": "/etc/streamctl",
        "STREAMCTL_BIND": "0.0.0.0",
        "STREAMCTL_PORT_BASE": "8502",
        "STREAMCTL_PORT_MAX": "8599",
        "STREAMCTL_VENV_CMD": "uv venv",  # {dir} appended
        "STREAMCTL_PIP_CMD": "uv pip install --python {venv_python}",  # + -r requirements.txt appended
        "STREAMCTL_APP_FILE": "app.py",
        "STREAMCTL_HEALTH_WAIT": "60",
        "STREAMCTL_POLL_INTERVAL": "1",
        "GSICTL_REG": "/etc/gsictl/registry.tsv",
        "GSICTL_TUNNELS_REG": "/etc/gsictl/tunnels.tsv",
        "STREAMCTL_API_PORT": "8510",
        "STREAMCTL_API_TOKEN": "",  # non-empty => mutating API calls require Bearer token
        "HEALTH_OK_CODES": "200|204",
        "PROBE_TIMEOUT": "8",
        "STREAMCTL_GIT_LOCAL_SHA_CMD": "git -C {dir} rev-parse HEAD",
        "STREAMCTL_GIT_REMOTE_SHA_CMD": "git -C {dir} ls-remote origin HEAD",
        "STREAMCTL_WATCH_INTERVAL": "300",  # streamctl-watch.timer cadence
        "STREAMCTL_ROUTER_URL": "",  # empty disables routing; cf-Worker or FastAPI backend URL
        "STREAMCTL_ROUTER_SECRET": "",
        "STREAMCTL_ROUTER_INTENTS_PATH": "/v1/intents",
        "STREAMCTL_ROUTE_WAIT_SEC": "30",
        "STREAMCTL_ROUTE_POST_TIMEOUT": "20",
        "STREAMCTL_WATCH_ON_BOOT": "2min",
        "STREAMCTL_UNITDIR": "/etc/systemd/system",
        "STREAMCTL_STATE_DIR": "/var/lib/streamctl",
        "STREAMCTL_CF_CREDS_FILE": "/home/dev/.cloudflared/api-credentials.env",
        "VERIFY_BODY_BYTES": "65536",  # max body read by verify_public_content
        "STREAMCTL_AUDIT_LOG": "",  # non-empty => per-call audit lines appended (jsonl)
    }
    path = Path(os.environ.get("STREAMCTL_CONF", DEFAULT_CONF))
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            conf[key.strip()] = value.strip()
    return conf


# ---------- primitives ----------


def sh(conf: dict[str, str], cmd: str, *, timeout: int = 600) -> str:
    """Run a shell command. sudo-prefixed commands run under `sudo -n` when the
    caller is not root (systemd + daemon-reload require root on this box);
    non-sudo commands run as-is.
    """
    if (
        os.geteuid() != 0
        and cmd.startswith("systemctl ")
        and not cmd.startswith("systemctl --user")
    ):
        cmd = f"sudo -n {cmd}"
    proc = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip()
        raise StreamctlError(f"`{cmd}` failed ({proc.returncode}): {detail[:500]}")
    return proc.stdout.strip()


def http_code(conf: dict[str, str], url: str) -> str:
    """HTTP status for URL; '000' when the connection itself fails (curl exit != 0)."""
    proc = subprocess.run(
        [
            "/usr/bin/curl",
            "-sS",
            "-o",
            "/dev/null",
            "-m",
            conf["PROBE_TIMEOUT"],
            "-w",
            "%{http_code}",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "000"


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(NAME_RE, name or ""))


def ok_code(conf: dict[str, str], code: str) -> bool:
    return bool(re.fullmatch(rf"(?:{conf['HEALTH_OK_CODES']})", code))


def port_listeners(conf: dict[str, str]) -> set[int]:
    out = sh(conf, "ss -ltn")
    return {
        int(tok)
        for line in out.splitlines()[1:]
        for tok in line.split()
        if tok.isdigit()
    }


# ---------- apps registry (TSV) ----------


def apps_path(conf: dict[str, str]) -> Path:
    return Path(conf["STREAMCTL_CONFDIR"]) / APPS_FILE


def load_apps(conf: dict[str, str]) -> list[dict[str, str]]:
    path = apps_path(conf)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines()[1:]:
        if not line:
            continue
        f = (line.split("\t") + [""] * 5)[:5]
        rows.append(
            dict(zip(("name", "port", "source", "public_url", "unit"), f, strict=False))
        )
    return rows


def save_apps(conf: dict[str, str], rows: list[dict[str, str]]) -> None:
    path = apps_path(conf)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    lines = [APPS_HEADER] + [
        f"{r['name']}\t{r['port']}\t{r['source']}\t{r['public_url']}\t{r['unit']}"
        for r in rows
    ]
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def get_app(conf: dict[str, str], name: str) -> dict[str, str]:
    for row in load_apps(conf):
        if row["name"] == name:
            return row
    raise StreamctlError(f"not found: {name}")


def _next_port(conf: dict[str, str], rows: list[dict[str, str]]) -> int:
    taken = {int(r["port"]) for r in rows if r["port"].isdigit()}
    base, top = int(conf["STREAMCTL_PORT_BASE"]), int(conf["STREAMCTL_PORT_MAX"])
    import socket as _socket

    for port in range(base, top + 1):
        if port in taken:
            continue
        # registry-free is not enough: an unregistered listener (manual app,
        # leftover unit) can hold the port. Require it to actually bind.
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                continue
        return port
    raise StreamctlError(f"no free port in {base}-{top}")


def health_wait(conf: dict[str, str], port: str) -> bool:
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    wait, poll = (
        int(conf["STREAMCTL_HEALTH_WAIT"]),
        int(conf["STREAMCTL_POLL_INTERVAL"]),
    )
    for _ in range(wait):
        if ok_code(conf, http_code(conf, url)):
            return True
        time.sleep(poll)
    return False



# ---------- audit log (append-only jsonl; every API-visible action records here) ----------


def audit(conf: dict[str, str], event: dict[str, object]) -> None:
    """Append one JSON line to STREAMCTL_AUDIT_LOG (when set). Never raises:
    a logging failure must never break an in-flight operation."""
    path = conf.get("STREAMCTL_AUDIT_LOG") or ""
    if not path:
        return
    try:
        import json as _json

        from datetime import datetime, timezone

        rec = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


# ---------- public-URL wiring (DNS + tunnel ingress via the CF API) ----------


def _cf_secrets(conf: dict[str, str]) -> dict[str, str]:
    """CF API credentials from STREAMCTL_CF_CREDS_FILE (never inline literals)."""
    path = Path(conf.get("STREAMCTL_CF_CREDS_FILE") or "")
    vals = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("CLOUDFLARE_EMAIL", "CLOUDFLARE_API_KEY"):
        if not vals.get(k):
            raise StreamctlError(f"CF creds file missing {k}: {path}")
    return vals


def _cf(conf: dict[str, str], method: str, path_url: str, body: dict | None = None) -> dict:
    """One Cloudflare API call. Raises StreamctlError on failure with the API error text."""
    import json as _json
    import urllib.error
    import urllib.request

    creds = _cf_secrets(conf)
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path_url}",
        data=_json.dumps(body).encode() if body is not None else None,
        headers={
            "X-Auth-Email": creds["CLOUDFLARE_EMAIL"],
            "X-Auth-Key": creds["CLOUDFLARE_API_KEY"],
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            out = _json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise StreamctlError(f"CF API {method} {path_url} -> {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise StreamctlError(f"CF API unreachable: {e.reason}") from e
    if not out.get("success"):
        raise StreamctlError(f"CF API {method} {path_url} refused: {out.get('errors')}")
    return out


def _public_host(name: str, domain: str) -> str:
    return f"{name}.{domain}"


def _resolve_domain(conf: dict[str, str], domain: str) -> dict[str, str]:
    """Look up a caller-supplied domain via the CF API: zone readable by these
    creds, that zone's account, and a healthy non-deleted tunnel in the SAME
    account. Nothing is assumed from conf; cross-account routing cannot happen.
    """
    zones = _cf(conf, "GET", f"/zones?name={domain}").get("result") or []
    if not zones:
        raise StreamctlError(f"domain not servable (no readable CF zone): {domain}")
    zone = zones[0]
    account = zone["account"]["id"]
    tunnels = (
        _cf(conf, "GET", f"/accounts/{account}/cfd_tunnel?is_deleted=false").get("result") or []
    )
    if not tunnels:
        raise StreamctlError(f"no tunnel in account for zone {domain}")
    # Deterministic pick from live CF state only - never conf-hardcoded, never
    # blind first-healthy. Anchor 1 (required for zones that already have
    # routes): the tunnel whose ingress already serves a hostname in the
    # CALLER'S zone - cross-account CNAMEs to a tunnel outside the zone's
    # account are rejected by CF edge (530/1033), so the zone-local tunnel is
    # the only correct choice when one exists. Anchor 2 (first route into a
    # fresh zone): the tunnel serving this host's STREAMCTL_PUBLIC_URL hostname.
    zone_suffix = "." + str(domain).lower()

    def _tunnel_hosts(tunnel_id: str, acct: str) -> list[str]:
        try:
            cfg = _cf(conf, "GET", f"/accounts/{acct}/cfd_tunnel/{tunnel_id}/configurations")
            return [
                (i.get("hostname") or "").lower()
                for i in (cfg.get("result", {}).get("config", {}).get("ingress") or [])
            ]
        except StreamctlError:
            return []

    accounts = [z["account"]["id"] for z in _cf(conf, "GET", "/zones?per_page=50").get("result") or []]
    public_host = re.sub(r"^https?://", "", conf.get("STREAMCTL_PUBLIC_URL", "")).rstrip("/").lower()
    chosen = None
    chosen_account = None
    # Anchor 1: the tunnel that live DNS in the caller's zone actually points at.
    # Multiple tunnels can carry the same hostname in their ingress config; only
    # the one the zone's CNAMEs target is the one CF edge routes to.
    dns_targets: set[str] = set()
    for rec in (_cf(conf, "GET", f"/zones/{zone['id']}/dns_records?per_page=100").get("result") or []):
        content = str(rec.get("content", ""))
        if content.endswith(".cfargotunnel.com"):
            dns_targets.add(content.split(".")[0])
    if dns_targets:
        for acct in dict.fromkeys(accounts):
            for t in (
                _cf(conf, "GET", f"/accounts/{acct}/cfd_tunnel?is_deleted=false").get("result") or []
            ):
                # must be a live DNS target in this zone AND carry our own
                # control hostname (proves it's this host's tunnel, not another
                # team's tunnel that merely shares the zone)
                if (
                    t.get("status") == "healthy"
                    and t["id"] in dns_targets
                    and public_host
                    and public_host in _tunnel_hosts(t["id"], acct)
                ):
                    chosen, chosen_account = t, acct
                    break
            if chosen is not None:
                break
    # Anchor 2: the tunnel serving the API's own public hostname
    if chosen is None and public_host:
        for acct in dict.fromkeys(accounts):
            for t in (
                _cf(conf, "GET", f"/accounts/{acct}/cfd_tunnel?is_deleted=false").get("result") or []
            ):
                if t.get("status") == "healthy" and public_host in _tunnel_hosts(t["id"], acct):
                    chosen, chosen_account = t, acct
                    break
            if chosen is not None:
                break
    if chosen is None:
        raise StreamctlError(
            f"no healthy tunnel routes any {domain} hostname (or {public_host!r}); "
            "wire the first route into this zone manually once, then create will follow it"
        )
    return {
        "zone_id": zone["id"],
        "zone_name": zone["name"],
        "account_id": chosen_account,
        "tunnel_id": chosen["id"],
        "tunnel_name": chosen["name"],
    }


def publish_public_url(conf: dict[str, str], name: str, port: str, domain: str) -> dict[str, str]:
    """Make {name}.{domain} live: DNS CNAME -> tunnel + ingress route -> local port.

    The domain is caller-supplied; zone/account/tunnel are resolved from the CF
    API for that domain on every call. Returns the public URL plus what was used.
    Raises StreamctlError with the CF error text on failure.
    """
    r = _resolve_domain(conf, domain)
    zone, account, tunnel = r["zone_id"], r["account_id"], r["tunnel_id"]
    host = _public_host(name, domain)

    # 1. DNS CNAME -> this account's tunnel
    recs = _cf(conf, "GET", f"/zones/{zone}/dns_records?name={host}").get("result") or []
    if recs:
        rec = recs[0]
        if rec.get("content") != f"{tunnel}.cfargotunnel.com":
            _cf(conf, "PUT", f"/zones/{zone}/dns_records/{rec['id']}",
                {"type": "CNAME", "name": host, "content": f"{tunnel}.cfargotunnel.com",
                 "proxied": True})
    else:
        _cf(conf, "POST", f"/zones/{zone}/dns_records",
            {"type": "CNAME", "name": host, "content": f"{tunnel}.cfargotunnel.com",
             "proxied": True})

    # 2. tunnel ingress: hostname -> local port (before the 404 catchall)
    cfg = _cf(conf, "GET", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations")["result"]["config"]
    ing = cfg.get("ingress") or []
    if not any(i.get("hostname") == host for i in ing):
        idx = next((i for i, x in enumerate(ing) if not x.get("hostname")), len(ing))
        ing.insert(idx, {"service": f"http://localhost:{port}", "hostname": host})
        _cf(conf, "PUT", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations",
            {"config": cfg})
    return {"public_url": f"https://{host}", **r}


def unpublish_public_url(conf: dict[str, str], name: str, domain: str) -> None:
    """Reverse publish_public_url: DNS record + ingress route for {name}.{domain}.

    Resolves zone/account/tunnel the same way publish does, so teardown always
    hits the same objects publish created.
    """
    r = _resolve_domain(conf, domain)
    zone, account, tunnel = r["zone_id"], r["account_id"], r["tunnel_id"]
    host = _public_host(name, domain)

    for rec in (_cf(conf, "GET", f"/zones/{zone}/dns_records?name={host}").get("result") or []):
        _cf(conf, "DELETE", f"/zones/{zone}/dns_records/{rec['id']}")

    cfg = _cf(conf, "GET", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations")["result"]["config"]
    ing = [i for i in (cfg.get("ingress") or []) if i.get("hostname") != host]
    cfg["ingress"] = ing
    _cf(conf, "PUT", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations", {"config": cfg})



# ---------- operations ----------


def verify_public_content(conf: dict[str, str], url: str) -> bool:
    """The bar: real rendered content at the public URL, not just an edge 200.

    Fetches the body over the public internet and requires HTTP 200 with a
    non-trivial HTML document (a Streamlit app always serves a real <html>
    shell; an error page or an empty body is a FAIL).
    """
    import http.client
    import json as _json
    import socket
    import ssl
    import urllib.parse

    def _doh_resolve(hostname: str) -> str | None:
        """Resolve via Cloudflare DoH over a direct IP connection, bypassing any
        stale negative cache on the local LAN resolver."""
        try:
            conn = http.client.HTTPSConnection("1.1.1.1", timeout=5)
            conn.request("GET", f"/dns-query?name={hostname}&type=A",
                         headers={"accept": "application/dns-json"})
            data = _json.loads(conn.getresponse().read())
            conn.close()
            for ans in data.get("Answer") or []:
                if ans.get("type") == 1:
                    return ans["data"]
        except OSError:
            return None
        return None

    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    try:
        ip = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)[0][4][0]
    except OSError:
        ip = _doh_resolve(host)
        if not ip:
            return False

    def _fetch(resolved_ip: str) -> tuple[int, str] | None:
        """TLS with SNI=host to the resolved IP, plain HTTP/1.1 GET, one retry-free shot."""
        try:
            ctx = ssl.create_default_context()
            raw = socket.create_connection((resolved_ip, 443), timeout=int(conf["PROBE_TIMEOUT"]))
            tls = ctx.wrap_socket(raw, server_hostname=host)
            req = (
                f"GET {parts.path or '/'} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "User-Agent: streamctl-verify/1.0\r\n"
                "Connection: close\r\n\r\n"
            )
            tls.sendall(req.encode())
            chunks = []
            total = int(conf.get("VERIFY_BODY_BYTES", "65536"))
            got = 0
            while got < total:
                b = tls.recv(65536)
                if not b:
                    break
                chunks.append(b)
                got += len(b)
            tls.close()
            data = b"".join(chunks).decode("utf-8", errors="replace")
            head, _, body = data.partition("\r\n\r\n")
            status = int(head.split(" ")[1]) if " " in head else 0
            return status, body
        except (OSError, ValueError):
            return None

    result = _fetch(ip)
    if result is None:
        doh_ip = _doh_resolve(host)
        if doh_ip and doh_ip != ip:
            result = _fetch(doh_ip)
    if result is None:
        return False
    status, body = result
    return (
        status == 200
        and "<html" in body.lower()
        and len(body.strip()) > 200
        and "streamlit" in body.lower()  # the app shell, not a CF/edge error page
    )


def create(conf: dict[str, str], name: str, source: str, domain: str = "") -> dict[str, object]:
    """Create+boot one Streamlit app. source = app dir with app.py, or a git URL."""
    if not valid_name(name):
        raise StreamctlError(f"bad name (must match {NAME_RE}): {name}")
    if any(r["name"] == name for r in load_apps(conf)):
        raise StreamctlError(f"already exists: {name}")
    root = Path(conf["STREAMCTL_ROOT"])
    appdir = root / name
    if appdir.exists():
        raise StreamctlError(
            f"directory exists but not registered: {appdir} (destroy it first)"
        )
    root.mkdir(parents=True, exist_ok=True)
    audit(conf, {"action": "create", "app": name, "result": "started", "source": source})

    if re.match(r"^(https?://|git@)", source or ""):
        sh(
            conf,
            f"git clone --depth 1 {shlex.quote(source)} {shlex.quote(str(appdir))}",
        )
    elif source and Path(source).is_dir():
        shutil.copytree(source, appdir, dirs_exist_ok=False)
    else:
        raise StreamctlError(f"source not a directory or git url: {source}")

    if not (appdir / conf["STREAMCTL_APP_FILE"]).is_file():
        (appdir / conf["STREAMCTL_APP_FILE"]).write_text(_starter_app(name))

    rows = load_apps(conf)
    port = _next_port(conf, rows)
    venv = appdir / ".venv"
    sh(conf, f"{conf['STREAMCTL_VENV_CMD']} {shlex.quote(str(venv))}")
    pip = conf["STREAMCTL_PIP_CMD"].format(venv_python=str(venv / "bin" / "python"))
    req = appdir / "requirements.txt"
    if req.exists():
        sh(conf, f"{pip} {shlex.quote('-r')} {shlex.quote(str(req))}", timeout=900)
    else:
        sh(conf, f"{pip} streamlit", timeout=900)

    unit = f"streamlit@{name}.service"
    public_url = PUBLIC_URL_PLACEHOLDER
    publish_info: dict[str, str] = {}
    ports_dir = Path(conf["STREAMCTL_CONFDIR"]) / "ports"
    ports_dir.mkdir(parents=True, exist_ok=True)
    (ports_dir / name).write_text(f"STREAMCTL_PORT={port}\n")
    sh(conf, "systemctl daemon-reload")
    sh(conf, f"systemctl enable --now {unit}")
    if not health_wait(conf, str(port)):
        sh(conf, f"systemctl disable --now {unit}")
        audit(conf, {"action": "create", "app": name, "result": "failed", "error": "health-wait timeout"})
        raise StreamctlError(
            f"health check failed for {name} after {conf['STREAMCTL_HEALTH_WAIT']}s; unit disabled. "
            f"Logs: journalctl -u {unit} -n 50"
        )

    if domain:
        # make {name}.{domain} live as part of create itself (DNS + ingress),
        # then hold the response until the public URL serves real content.
        result = publish_public_url(conf, name, str(port), domain)
        public_url = result["public_url"]
        publish_info = {k: v for k, v in result.items() if k != "public_url"}
        deadline = time.time() + int(conf["STREAMCTL_HEALTH_WAIT"])
        content_ok = verify_public_content(conf, public_url)
        while not content_ok and time.time() < deadline:
            time.sleep(3)
            content_ok = verify_public_content(conf, public_url)
        if not content_ok:
            audit(conf, {
                "action": "create", "app": name, "result": "failed",
                "error": f"public URL did not serve real content: {public_url}",
            })
            try:
                unpublish_public_url(conf, name, domain)
            except StreamctlError as te:
                publish_info["teardown_error"] = str(te)
            sh(conf, f"systemctl disable --now {unit}")
            raise StreamctlError(
                f"public URL did not serve real content within "
                f"{conf['STREAMCTL_HEALTH_WAIT']}s: {public_url}; app torn down"
            )

    rows.append(
        {
            "name": name,
            "port": str(port),
            "source": source,
            "public_url": public_url,
            "unit": unit,
        }
    )
    save_apps(conf, rows)
    _gsictl_add(conf, rows[-1])
    audit(conf, {"action": "create", "app": name, "result": "ok", "port": str(port), **publish_info})
    return {
        "name": name,
        "port": port,
        "dir": str(appdir),
        "unit": unit,
        "public_url": public_url,
        **publish_info,
    }


def deploy(
    conf: dict[str, str], name: str, *, restart: bool = True
) -> dict[str, object]:
    app = get_app(conf, name)
    appdir = Path(conf["STREAMCTL_ROOT"]) / name
    if (appdir / ".git").is_dir():
        sh(conf, f"git -C {shlex.quote(str(appdir))} pull --ff-only")
        req = appdir / "requirements.txt"
        if req.exists():
            pip = conf["STREAMCTL_PIP_CMD"].format(
                venv_python=str(appdir / ".venv" / "bin" / "python")
            )
            sh(conf, f"{pip} {shlex.quote('-r')} {shlex.quote(str(req))}", timeout=900)
    if restart:
        sh(conf, f"systemctl restart {app['unit']}")
        if not health_wait(conf, app["port"]):
            raise StreamctlError(f"health check failed after restart of {name}")
    return {"name": name, "ok": True}


def status(conf: dict[str, str]) -> list[dict[str, object]]:
    rows = []
    for r in load_apps(conf):
        code = http_code(conf, f"http://127.0.0.1:{r['port']}{HEALTH_PATH}")
        rows.append(dict(r, health=code, ok=ok_code(conf, code)))
    return rows


def watch_tick(conf: dict[str, str]) -> dict[str, object]:
    """One CI/CD pass: for every git-sourced app, deploy when origin HEAD moved.

    Runs from streamctl-watch.timer (systemd), so there is no resident process
    to hold memory or expose a port. Interval lives in STREAMCTL_WATCH_INTERVAL
    (streamctl-watch.timer) - change conf, reinstall.
    """
    out: dict[str, object] = {
        "checked": 0,
        "deployed": [],
        "unchanged": [],
        "errors": [],
    }
    for row in load_apps(conf):
        appdir = Path(conf["STREAMCTL_ROOT"]) / row["name"]
        if not (appdir / ".git").is_dir():
            continue
        out["checked"] = int(out["checked"]) + 1  # type: ignore[operator]
        state = Path(conf["STREAMCTL_STATE_DIR"]) / f"head-{row['name']}"
        try:
            sh(
                conf,
                conf["STREAMCTL_GIT_LOCAL_SHA_CMD"].format(
                    dir=shlex.quote(str(appdir))
                ),
            )
            remote = sh(
                conf,
                conf["STREAMCTL_GIT_REMOTE_SHA_CMD"].format(
                    dir=shlex.quote(str(appdir))
                ),
            ).split("\t", 1)[0]
        except StreamctlError as e:
            out["errors"] = [*out["errors"], {"name": row["name"], "error": str(e)}]  # type: ignore[union-attr]
            continue
        if state.is_file() and state.read_text(encoding="utf-8").strip() == remote:
            _watch_note(out, row["name"], "unchanged")
            continue
        try:
            deploy(conf, row["name"])
        except StreamctlError as e:
            out["errors"] = [*out["errors"], {"name": row["name"], "error": str(e)}]  # type: ignore[union-attr]
            continue
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(remote + "\n")
        _watch_note(out, row["name"], "deployed")
    return out


def _watch_note(out: dict[str, object], name: str, kind: str) -> None:
    out[kind] = [*(out.get(kind) or []), name]  # type: ignore[union-attr,assignment]


def destroy(conf: dict[str, str], name: str) -> dict[str, object]:
    audit(conf, {"action": "destroy", "app": name, "result": "started"})
    app = get_app(conf, name)
    appdir = Path(conf["STREAMCTL_ROOT"]) / name
    sh(conf, f"systemctl disable --now {app['unit']}")
    _gsictl_remove(conf, app)
    shutil.rmtree(appdir, ignore_errors=True)
    ports_dir = Path(conf["STREAMCTL_CONFDIR"]) / "ports"
    (ports_dir / name).unlink(missing_ok=True)
    url = app.get("public_url") or ""
    if url not in (PUBLIC_URL_PLACEHOLDER, ""):
        # derive the caller-chosen domain from the stored public URL
        host = re.sub(r"^https?://", "", url).rstrip("/")
        domain = host.split(".", 1)[1] if "." in host else ""
        if domain:
            try:
                unpublish_public_url(conf, name, domain)
            except StreamctlError as e:
                audit(conf, {"action": "destroy", "app": name, "result": "partial", "error": str(e)})
                save_apps(conf, [r for r in load_apps(conf) if r["name"] != name])
                raise
    save_apps(conf, [r for r in load_apps(conf) if r["name"] != name])
    audit(conf, {"action": "destroy", "app": name, "result": "ok"})
    return {"name": name, "destroyed": True}


# ---------- cf-router intent client (both backends, conf-selected) ----------


def _router_url(conf: dict[str, str]) -> str:
    """Conf-selected backend base URL; raises when routing is unset."""
    router_url = (conf.get("STREAMCTL_ROUTER_URL") or "").rstrip("/")
    if not router_url:
        raise StreamctlError("routing requested but STREAMCTL_ROUTER_URL unset in conf")
    return router_url


def _router_post(conf: dict[str, str], body: dict[str, object]) -> dict[str, object]:
    """Publish one signed routing intent; URL + secret from conf only."""
    import hashlib
    import hmac
    import json as _json
    import urllib.request

    router_url = _router_url(conf)
    secret = conf.get("STREAMCTL_ROUTER_SECRET") or ""
    if not secret:
        raise StreamctlError("routing requested but STREAMCTL_ROUTER_SECRET unset in conf")
    payload = dict(body)
    issued = int(time.time() * 1000)
    payload["issued_at"] = issued
    bare = _json.dumps(payload, separators=(",", ":"))
    mac = hmac.new(secret.encode(), (bare + str(issued)).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{router_url}{conf['STREAMCTL_ROUTER_INTENTS_PATH']}",
        data=_json.dumps({**payload, "mac": mac}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=int(conf["STREAMCTL_ROUTE_POST_TIMEOUT"])) as resp:
        return _json.loads(resp.read().decode())


def _route_hostname(conf: dict[str, str], app: dict[str, str]) -> str:
    """The app's public hostname; re-derives from conf when the row predates a domain."""
    if app["public_url"] != PUBLIC_URL_PLACEHOLDER:
        return re.sub(r"^https?://", "", app["public_url"]).rstrip("/")
    return re.sub(r"^https?://", "", app["public_url"]).rstrip("/")


def publish_route(conf: dict[str, str], name: str, action: str = "create") -> dict[str, object]:
    """Sign+publish a routing intent for app NAME; returns the pending envelope."""
    app = get_app(conf, name)
    return _router_post(
        conf,
        {
            "id": f"{name}-{int(time.time())}",
            "action": action,
            "target": "home",
            "app": name,
            "hostname": _route_hostname(conf, app),
            "port": int(app["port"]),
        },
    )


def route_status(conf: dict[str, str], intent_id: str) -> dict[str, object]:
    """Poll one intent's status from the conf-selected backend."""
    import json as _json
    import urllib.request

    router_url = _router_url(conf)
    with urllib.request.urlopen(
        f"{router_url}{conf['STREAMCTL_ROUTER_INTENTS_PATH']}/{intent_id}",
        timeout=int(conf["PROBE_TIMEOUT"]),
    ) as resp:
        return _json.loads(resp.read().decode())


def await_route(conf: dict[str, str], intent_id: str) -> dict[str, object]:
    """Poll until done/failed or ROUTE_WAIT_SEC elapses; last status wins."""
    deadline = time.time() + int(conf["STREAMCTL_ROUTE_WAIT_SEC"])
    last: dict[str, object] = {}
    while time.time() < deadline:
        last = route_status(conf, intent_id)
        if last.get("status") not in (None, "pending"):
            return last
        time.sleep(int(conf["STREAMCTL_POLL_INTERVAL"]))
    return {**last, "status": last.get("status") or "pending"}


def route(conf: dict[str, str], name: str, action: str = "create") -> dict[str, object]:
    """publish + await; surfaces backend status verbatim."""
    intent = publish_route(conf, name, action)
    result = await_route(conf, str(intent["id"]))
    return {**result, "intent_id": intent["id"]}


def _starter_app(name: str) -> str:
    return (
        "import streamlit as st\n\n"
        f'st.set_page_config(page_title="{name}", layout="centered")\n'
        f'st.title("{name}")\n'
        'st.caption("Self-hosted via streamctl - replace app.py with real code.")\n'
    )


# ---------- gsictl integration (supervision handoff) ----------


def _gsictl_add(conf: dict[str, str], row: dict[str, str]) -> None:
    reg = Path(conf["GSICTL_REG"])
    if reg.is_file():
        body = "".join(
            ln
            for ln in reg.read_text(encoding="utf-8").splitlines(keepends=True)
            if ln.split("\t", 1)[0] != row["name"]
        )
        body += f"{row['name']}\t{row['unit']}\t{row['port']}\t{row['public_url']}\t{HEALTH_PATH}\n"
        tmp = reg.with_suffix(".tmp")
        tmp.write_text(body)
        tmp.replace(reg)
    if row["public_url"] != PUBLIC_URL_PLACEHOLDER:
        tun = Path(conf["GSICTL_TUNNELS_REG"])
        if tun.is_file():
            host = re.sub(r"^https?://", "", row["public_url"]).rstrip("/")
            lines = tun.read_text(encoding="utf-8").splitlines()
            known = {ln.split("\t")[0] for ln in lines[1:] if ln}
            if host not in known:
                tmp = tun.with_suffix(".tmp")
                tmp.write_text(
                    "\n".join([*lines, f"{host}\t{row['port']}\t{HEALTH_PATH}"]) + "\n"
                )
                tmp.replace(tun)


def _gsictl_remove(conf: dict[str, str], row: dict[str, str]) -> None:
    reg = Path(conf["GSICTL_REG"])
    if reg.is_file():
        tmp = reg.with_suffix(".tmp")
        tmp.write_text(
            "".join(
                ln
                for ln in reg.read_text(encoding="utf-8").splitlines(keepends=True)
                if ln.split("\t", 1)[0] != row["name"]
            )
        )
        tmp.replace(reg)
    if row["public_url"] != PUBLIC_URL_PLACEHOLDER:
        tun = Path(conf["GSICTL_TUNNELS_REG"])
        if tun.is_file():
            host = re.sub(r"^https?://", "", row["public_url"]).rstrip("/")
            tmp = tun.with_suffix(".tmp")
            tmp.write_text(
                "".join(
                    ln
                    for ln in tun.read_text(encoding="utf-8").splitlines(keepends=True)
                    if ln.split("\t", 1)[0] != host
                )
            )
            tmp.replace(tun)
