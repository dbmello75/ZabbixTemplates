#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reolink_fw_check.py

Zabbix External Script for Reolink firmware check.

Modo único:
    reolink_fw_check.py "<ZABBIX_HOSTNAME>"

O script:
  1. Lê /etc/zabbix/reolink_fw_check.conf
  2. Consulta a API do Zabbix usando Authorization: Bearer TOKEN
  3. Busca o Inventory do host
  4. Usa inventory.model, inventory.hardware e inventory.software
  5. Faz scraping da página de suporte/download da Reolink
  6. Retorna JSON para itens dependentes no Zabbix

Arquivo de configuração:
    /etc/zabbix/reolink_fw_check.conf

Conteúdo mínimo:
    ZABBIX_URL=https://zbx.exemplo.com/zabbix/api_jsonrpc.php
    ZABBIX_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

Opcional:
    REOLINK_DEFAULT_URL=https://support.reolink.com/c/rln8-410-rln16-410/
"""

import json
import re
import sys
import html
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

CONFIG_FILE = "/etc/zabbix/reolink_fw_check.conf"
DEFAULT_REOLINK_URL = "https://support.reolink.com/c/rln8-410-rln16-410/"


def out(payload, exit_code=0):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.exit(exit_code)


def base_result(host=None):
    return {
        "host": host,
        "resolved_host": None,
        "hostid": None,
        "model": None,
        "hardware": None,
        "current": None,
        "latest": None,
        "latest_hw_match": None,
        "latest_updated": None,
        "update_available": 0,
        "scrape_ok": 0,
        "match_found": 0,
        "inventory_ok": 0,
        "zabbix_api_ok": 0,
        "source": None,
    }


def load_config(path=CONFIG_FILE):
    cfg = {}
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"config file not found: {path}")

    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")

    if not cfg.get("ZABBIX_URL"):
        raise RuntimeError("missing ZABBIX_URL in config")
    if not cfg.get("ZABBIX_TOKEN"):
        raise RuntimeError("missing ZABBIX_TOKEN in config")

    cfg.setdefault("REOLINK_DEFAULT_URL", DEFAULT_REOLINK_URL)
    return cfg


def zabbix_api_call(url, token, method, params):
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }

    headers = {
        "Content-Type": "application/json-rpc",
        "Authorization": f"Bearer {token}",
    }

    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {err_body[:500]}") from e
    except URLError as e:
        raise RuntimeError(f"URL error: {e}") from e

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from Zabbix API: {body[:500]}") from e

    if "error" in data:
        raise RuntimeError(f"zabbix api error: {data['error']}")

    return data.get("result")


def normalize(value):
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


def get_host_inventory(cfg, host_name):
    params = {
        "output": ["hostid", "host", "name", "status"],
        "filter": {
            "host": [host_name]
        },
        "selectInventory": ["model", "hardware", "software", "serialno_a"],
        "limit": 1,
    }

    result = zabbix_api_call(
        cfg["ZABBIX_URL"],
        cfg["ZABBIX_TOKEN"],
        "host.get",
        params,
    )

    if not result:
        params = {
            "output": ["hostid", "host", "name", "status"],
            "search": {
                "name": host_name
            },
            "selectInventory": ["model", "hardware", "software", "serialno_a"],
            "limit": 1,
        }
        result = zabbix_api_call(
            cfg["ZABBIX_URL"],
            cfg["ZABBIX_TOKEN"],
            "host.get",
            params,
        )

    if not result:
        raise RuntimeError(f"host not found or token has no permission: {host_name}")

    host = result[0]
    inv = host.get("inventory") or {}

    return {
        "hostid": host.get("hostid"),
        "host": host.get("host"),
        "name": host.get("name"),
        "model": normalize(inv.get("model")),
        "hardware": normalize(inv.get("hardware")),
        "software": normalize(inv.get("software")),
        "serialno_a": normalize(inv.get("serialno_a")),
    }


def firmware_tuple(fw):
    if not fw:
        return (0, 0, 0, 0, 0)
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)\.(\d+)_([0-9]+)", fw)
    if not m:
        return (0, 0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())


def hw_matches(page_hw, wanted_hw):
    if not page_hw or not wanted_hw:
        return False

    p = page_hw.strip().lower()
    w = wanted_hw.strip().lower()

    if p == w:
        return True

    parts = re.split(r"\s+or\s+|,|/|\||;", p)
    parts = [x.strip() for x in parts if x.strip()]
    return w in parts


def clean_html(raw):
    raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    raw = html.unescape(raw)
    lines = [x.strip() for x in raw.splitlines()]
    return [x for x in lines if x]


def build_candidate_urls(model, default_url):
    urls = []

    if default_url:
        urls.append(default_url)

    urls.append("https://support.reolink.com/c/firmware/")
    urls.append("https://reolink.com/us/download-center/")

    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def fetch_url(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 zabbix-reolink-fw-check/2.0"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def extract_date(text):
    if not text:
        return None

    patterns = [
        r"(Jan\.?|Feb\.?|Mar\.?|Apr\.?|May|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Oct\.?|Nov\.?|Dec\.?)\s+\d{1,2},\s+\d{4}",
        r"\d{4}-\d{2}-\d{2}",
        r"\d{1,2}/\d{1,2}/\d{4}",
    ]

    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(0)
    return None


def find_latest_firmware(model, hardware, urls):
    records = []
    last_error = None

    for url in urls:
        try:
            raw = fetch_url(url)
        except Exception as e:
            last_error = str(e)
            continue

        lines = clean_html(raw)

        for i, line in enumerate(lines):
            if model.lower() not in line.lower():
                continue

            window = lines[i:i + 25]
            joined = " | ".join(window)

            fws = re.findall(r"v\d+\.\d+\.\d+\.\d+_[0-9]+", joined)
            if not fws:
                continue

            possible_hw = []
            for w in window:
                if re.fullmatch(r"[A-Z0-9_]+(?:\s+or\s+[A-Z0-9_]+)*(?:\s*,\s*[A-Z0-9_]+)*", w, flags=re.I):
                    if not w.lower().startswith("v") and len(w) >= 4:
                        possible_hw.append(w)

            if hardware.lower() in joined.lower():
                possible_hw.append(hardware)

            for fw in fws:
                if not possible_hw:
                    records.append({
                        "firmware": fw,
                        "hardware": None,
                        "updated": extract_date(joined),
                        "source": url,
                    })
                else:
                    for hw in possible_hw:
                        records.append({
                            "firmware": fw,
                            "hardware": hw,
                            "updated": extract_date(joined),
                            "source": url,
                        })

        for i, line in enumerate(lines):
            if hardware.lower() not in line.lower():
                continue

            window = lines[max(0, i - 10):i + 20]
            joined = " | ".join(window)
            fws = re.findall(r"v\d+\.\d+\.\d+\.\d+_[0-9]+", joined)

            for fw in fws:
                records.append({
                    "firmware": fw,
                    "hardware": hardware,
                    "updated": extract_date(joined),
                    "source": url,
                })

        matches = [
            r for r in records
            if r.get("firmware") and (
                hw_matches(r.get("hardware"), hardware) or
                (r.get("hardware") is None and hardware.lower() in raw.lower())
            )
        ]

        if matches:
            return sorted(matches, key=lambda r: firmware_tuple(r["firmware"]))[-1]

    if last_error:
        raise RuntimeError(f"scrape failed: {last_error}")

    return None


def main():
    if len(sys.argv) != 2:
        out({
            "error": 'usage: reolink_fw_check.py "<ZABBIX_HOSTNAME>"',
            "scrape_ok": 0,
            "match_found": 0,
            "inventory_ok": 0,
            "zabbix_api_ok": 0,
            "update_available": 0,
        })

    requested_host = sys.argv[1].strip()
    result = base_result(requested_host)

    try:
        cfg = load_config()
    except Exception as e:
        result["error"] = str(e)
        out(result)

    try:
        inv = get_host_inventory(cfg, requested_host)
        result["zabbix_api_ok"] = 1
        result["hostid"] = inv["hostid"]
        result["resolved_host"] = inv["host"]
        result["model"] = inv["model"]
        result["hardware"] = inv["hardware"]
        result["current"] = inv["software"]
    except Exception as e:
        result["error"] = str(e)
        out(result)

    if result["model"] and result["hardware"] and result["current"]:
        result["inventory_ok"] = 1
    else:
        missing = []
        if not result["model"]:
            missing.append("inventory.model")
        if not result["hardware"]:
            missing.append("inventory.hardware")
        if not result["current"]:
            missing.append("inventory.software")
        result["error"] = "missing inventory fields: " + ", ".join(missing)
        out(result)

    urls = build_candidate_urls(result["model"], cfg.get("REOLINK_DEFAULT_URL"))
    result["source"] = urls[0] if urls else None

    try:
        latest = find_latest_firmware(result["model"], result["hardware"], urls)
        result["scrape_ok"] = 1
    except Exception as e:
        result["error"] = str(e)
        out(result)

    if not latest:
        result["error"] = "model/hardware firmware not found online"
        out(result)

    result["latest"] = latest.get("firmware")
    result["latest_hw_match"] = latest.get("hardware")
    result["latest_updated"] = latest.get("updated")
    result["source"] = latest.get("source")
    result["match_found"] = 1
    result["update_available"] = int(
        firmware_tuple(result["latest"]) > firmware_tuple(result["current"])
    )

    out(result)


if __name__ == "__main__":
    main()
