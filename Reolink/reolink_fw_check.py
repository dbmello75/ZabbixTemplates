#!/usr/bin/env python3
import sys
import re
import json
import html
import urllib.request
from urllib.error import URLError, HTTPError

DEFAULT_URL = "https://support.reolink.com/c/rln8-410-rln16-410/"

def usage():
    print(json.dumps({
        "error": "usage: reolink_fw_check.py <model> <hardware_version> <current_firmware> [url]"
    }))
    sys.exit(1)

def fw_build(fw):
    m = re.search(r"_([0-9]+)$", fw or "")
    return int(m.group(1)) if m else 0

def fw_tuple(fw):
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)\.(\d+)_([0-9]+)", fw or "")
    if not m:
        return (0, 0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())

def hw_matches(page_hw, wanted_hw):
    page_hw = page_hw.strip().lower()
    wanted_hw = wanted_hw.strip().lower()

    parts = re.split(r"\s+or\s+|,|/|\|", page_hw)
    parts = [p.strip() for p in parts if p.strip()]

    return wanted_hw == page_hw or wanted_hw in parts

def clean_html(raw):
    raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    raw = html.unescape(raw)
    lines = [x.strip() for x in raw.splitlines()]
    return [x for x in lines if x]

def main():
    if len(sys.argv) < 4:
        usage()

    model = sys.argv[1].strip()
    hardware = sys.argv[2].strip()
    current_fw = sys.argv[3].strip()
    url = sys.argv[4].strip() if len(sys.argv) >= 5 else DEFAULT_URL

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 zabbix-reolink-fw-check/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="ignore")
    except (URLError, HTTPError, TimeoutError) as e:
        print(json.dumps({
            "model": model,
            "hardware": hardware,
            "current": current_fw,
            "error": str(e),
            "scrape_ok": 0
        }))
        sys.exit(0)

    lines = clean_html(raw)
    records = []

    for i, line in enumerate(lines):
        if model.lower() in line.lower() and "nvr" in line.lower():
            window = lines[i:i+10]

            hw = None
            fw = None
            updated = None

            for w in window[1:]:
                if re.match(r"^[A-Z0-9_]+(\s+or\s+[A-Z0-9_]+)*$", w, re.I):
                    hw = w
                    break

            for w in window[1:]:
                if re.match(r"^v\d+\.\d+\.\d+\.\d+_[0-9]+$", w):
                    fw = w
                    break

            for w in window[1:]:
                if "Download Firmware" in w and "Updated" in w:
                    updated = w.replace("Download Firmware", "").strip()
                    break

            if hw and fw:
                records.append({
                    "model": model,
                    "hardware": hw,
                    "firmware": fw,
                    "updated": updated
                })

    matches = [r for r in records if hw_matches(r["hardware"], hardware)]

    if not matches:
        print(json.dumps({
            "model": model,
            "hardware": hardware,
            "current": current_fw,
            "latest": None,
            "update_available": 0,
            "scrape_ok": 1,
            "match_found": 0,
            "source": url
        }))
        sys.exit(0)

    latest = sorted(matches, key=lambda r: fw_tuple(r["firmware"]))[-1]
    update_available = int(fw_tuple(latest["firmware"]) > fw_tuple(current_fw))

    print(json.dumps({
        "model": model,
        "hardware": hardware,
        "current": current_fw,
        "latest": latest["firmware"],
        "latest_hw_match": latest["hardware"],
        "latest_updated": latest["updated"],
        "update_available": update_available,
        "scrape_ok": 1,
        "match_found": 1,
        "source": url
    }))

if __name__ == "__main__":
    main()
