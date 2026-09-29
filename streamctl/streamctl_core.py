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
        "STREAMCTL_DOMAIN": "",  # base domain; empty disables public-URL bookkeeping
        "GSICTL_REG": "/etc/gsictl/registry.tsv",
        "GSICTL_TUNNELS_REG": "/etc/gsictl/tunnels.tsv",
        "STREAMCTL_API_PORT": "8510",
        "STREAMCTL_API_TOKEN": "",  # non-empty => mutating API calls require Bearer token
        "HEALTH_OK_CODES": "200|204",
        "PROBE_TIMEOUT": "8",
    }
    path = Path(os.environ.get("STREAMCTL_CONF", DEFAULT_CONF))
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            conf[key.strip()] = value.strip()
    return conf


# ---------- primitives ----------

def sh(conf: dict, cmd: str, *, timeout: int = 600) -> str:
    """Run a shell command. sudo-prefixed commands run under `sudo -n` when the
    caller is not root (systemd + daemon-reload require root on this box);
    non-sudo commands run as-is."""
    if os.geteuid() != 0 and cmd.startswith("systemctl ") and not cmd.startswith("systemctl --user"):
        cmd = f"sudo -n {cmd}"
    proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip()
        raise StreamctlError(f"`{cmd}` failed ({proc.returncode}): {detail[:500]}")
    return proc.stdout.strip()


def http_code(conf: dict, url: str) -> str:
    """HTTP status for URL; '000' when the connection itself fails (curl exit != 0)."""
    proc = subprocess.run(
        ["curl", "-sS", "-o", "/dev/null", "-m", conf["PROBE_TIMEOUT"], "-w", "%{http_code}", url],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() or "000"


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(NAME_RE, name or ""))


def ok_code(conf: dict, code: str) -> bool:
    return bool(re.fullmatch(rf"(?:{conf['HEALTH_OK_CODES']})", code))


def port_listeners(conf: dict) -> set[int]:
    out = sh(conf, "ss -ltn")
    return {int(tok) for line in out.splitlines()[1:] for tok in line.split() if tok.isdigit()}


# ---------- apps registry (TSV) ----------

def apps_path(conf: dict) -> Path:
    return Path(conf["STREAMCTL_CONFDIR"]) / APPS_FILE


def load_apps(conf: dict) -> list[dict]:
    path = apps_path(conf)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines()[1:]:
        if not line:
            continue
        f = (line.split("\t") + [""] * 5)[:5]
        rows.append(dict(zip(("name", "port", "source", "public_url", "unit"), f)))
    return rows


def save_apps(conf: dict, rows: list[dict]) -> None:
    path = apps_path(conf)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    lines = [APPS_HEADER] + [f"{r['name']}\t{r['port']}\t{r['source']}\t{r['public_url']}\t{r['unit']}" for r in rows]
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def get_app(conf: dict, name: str) -> dict:
    for row in load_apps(conf):
        if row["name"] == name:
            return row
    raise StreamctlError(f"not found: {name}")


def _next_port(conf: dict, rows: list[dict]) -> int:
    taken = {int(r["port"]) for r in rows if r["port"].isdigit()}
    base, top = int(conf["STREAMCTL_PORT_BASE"]), int(conf["STREAMCTL_PORT_MAX"])
    for port in range(base, top + 1):
        if port not in taken:
            return port
    raise StreamctlError(f"no free port in {base}-{top}")


def _public_url(conf: dict, name: str) -> str:
    domain = conf.get("STREAMCTL_DOMAIN", "")
    return f"https://{name}.{domain}" if domain else PUBLIC_URL_PLACEHOLDER


def health_wait(conf: dict, port: str) -> bool:
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    wait, poll = int(conf["STREAMCTL_HEALTH_WAIT"]), int(conf["STREAMCTL_POLL_INTERVAL"])
    for _ in range(wait):
        if ok_code(conf, http_code(conf, url)):
            return True
        time.sleep(poll)
    return False


# ---------- operations ----------

def create(conf: dict, name: str, source: str) -> dict:
    """Create+boot one Streamlit app. source = app dir with app.py, or a git URL."""
    if not valid_name(name):
        raise StreamctlError(f"bad name (must match {NAME_RE}): {name}")
    if any(r["name"] == name for r in load_apps(conf)):
        raise StreamctlError(f"already exists: {name}")
    root = Path(conf["STREAMCTL_ROOT"])
    appdir = root / name
    if appdir.exists():
        raise StreamctlError(f"directory exists but not registered: {appdir} (destroy it first)")
    root.mkdir(parents=True, exist_ok=True)

    if re.match(r"^(https?://|git@)", source or ""):
        sh(conf, f"git clone --depth 1 {shlex.quote(source)} {shlex.quote(str(appdir))}")
    elif Path(source).is_dir():
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
    public_url = _public_url(conf, name)
    ports_dir = Path(conf["STREAMCTL_CONFDIR"]) / "ports"
    ports_dir.mkdir(parents=True, exist_ok=True)
    (ports_dir / name).write_text(f"STREAMCTL_PORT={port}\n")
    sh(conf, "systemctl daemon-reload")
    sh(conf, f"systemctl enable --now {unit}")
    if not health_wait(conf, str(port)):
        sh(conf, f"systemctl disable --now {unit}")
        raise StreamctlError(
            f"health check failed for {name} after {conf['STREAMCTL_HEALTH_WAIT']}s; unit disabled. "
            f"Logs: journalctl -u {unit} -n 50")

    rows.append({"name": name, "port": str(port), "source": source,
                 "public_url": public_url, "unit": unit})
    save_apps(conf, rows)
    _gsictl_add(conf, rows[-1])
    return {"name": name, "port": port, "dir": str(appdir), "unit": unit, "public_url": public_url}


def deploy(conf: dict, name: str, *, restart: bool = True) -> dict:
    app = get_app(conf, name)
    appdir = Path(conf["STREAMCTL_ROOT"]) / name
    if (appdir / ".git").is_dir():
        sh(conf, f"git -C {shlex.quote(str(appdir))} pull --ff-only")
        req = appdir / "requirements.txt"
        if req.exists():
            pip = conf["STREAMCTL_PIP_CMD"].format(venv_python=str(appdir / ".venv" / "bin" / "python"))
            sh(conf, f"{pip} {shlex.quote('-r')} {shlex.quote(str(req))}", timeout=900)
    if restart:
        sh(conf, f"systemctl restart {app['unit']}")
        if not health_wait(conf, app["port"]):
            raise StreamctlError(f"health check failed after restart of {name}")
    return {"name": name, "ok": True}


def status(conf: dict) -> list[dict]:
    rows = []
    for r in load_apps(conf):
        code = http_code(conf, f"http://127.0.0.1:{r['port']}{HEALTH_PATH}")
        rows.append(dict(r, health=code, ok=ok_code(conf, code)))
    return rows


def destroy(conf: dict, name: str) -> dict:
    app = get_app(conf, name)
    appdir = Path(conf["STREAMCTL_ROOT"]) / name
    sh(conf, f"systemctl disable --now {app['unit']}")
    _gsictl_remove(conf, app)
    shutil.rmtree(appdir, ignore_errors=True)
    ports_dir = Path(conf["STREAMCTL_CONFDIR"]) / "ports"
    (ports_dir / name).unlink(missing_ok=True)
    save_apps(conf, [r for r in load_apps(conf) if r["name"] != name])
    return {"name": name, "destroyed": True}


def _starter_app(name: str) -> str:
    return (
        "import streamlit as st\n\n"
        f"st.set_page_config(page_title=\\\"{name}\\\", layout=\\\"centered\\\")\n"
        f"st.title(\\\"{name}\\\")\n"
        "st.caption(\\\"Self-hosted via streamctl - replace app.py with real code.\\\")\n"
    )


# ---------- gsictl integration (supervision handoff) ----------

def _gsictl_add(conf: dict, row: dict) -> None:
    reg = Path(conf["GSICTL_REG"])
    if reg.is_file():
        body = "".join(ln for ln in reg.read_text().splitlines(keepends=True)
                       if ln.split("\t", 1)[0] != row["name"])
        body += f"{row['name']}\t{row['unit']}\t{row['port']}\t{row['public_url']}\t{HEALTH_PATH}\n"
        tmp = reg.with_suffix(".tmp")
        tmp.write_text(body)
        tmp.replace(reg)
    if row["public_url"] != PUBLIC_URL_PLACEHOLDER:
        tun = Path(conf["GSICTL_TUNNELS_REG"])
        if tun.is_file():
            host = re.sub(r"^https?://", "", row["public_url"]).rstrip("/")
            lines = tun.read_text().splitlines()
            known = {ln.split("\t")[0] for ln in lines[1:] if ln}
            if host not in known:
                tmp = tun.with_suffix(".tmp")
                tmp.write_text("\n".join(lines + [f"{host}\t{row['port']}\t{HEALTH_PATH}"]) + "\n")
                tmp.replace(tun)


def _gsictl_remove(conf: dict, row: dict) -> None:
    reg = Path(conf["GSICTL_REG"])
    if reg.is_file():
        tmp = reg.with_suffix(".tmp")
        tmp.write_text("".join(ln for ln in reg.read_text().splitlines(keepends=True)
                               if ln.split("\t", 1)[0] != row["name"]))
        tmp.replace(reg)
    if row["public_url"] != PUBLIC_URL_PLACEHOLDER:
        tun = Path(conf["GSICTL_TUNNELS_REG"])
        if tun.is_file():
            host = re.sub(r"^https?://", "", row["public_url"]).rstrip("/")
            tmp = tun.with_suffix(".tmp")
            tmp.write_text("".join(ln for ln in tun.read_text().splitlines(keepends=True)
                                   if ln.split("\t", 1)[0] != host))
            tmp.replace(tun)