#!/usr/bin/env python3
"""Binzmühle-Watcher für GitHub Actions: ein Durchlauf pro Aufruf, Push via ntfy."""
import hashlib
import json
import os
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
URL_LIST = "https://binzmuehle.ch/vermietung/wohnungen"
URL_INFO = "https://binzmuehle.ch/vermietung"
URL_DOCS = "https://binzmuehle.ch/vermietung/dokumente-fuer-bewerbung"
CLOSED_MARKER = "Momentan kann sich niemand auf eine Wohnung bewerben"
MAX_REPEATS = 12          # so viele Meldungen, solange die Bewerbung offen ist
REPEAT_AFTER = 600        # Sekunden Pause zwischen Wiederholungen
STATE_FILE = Path(__file__).with_name("state.json")
UA = "Mozilla/5.0 (private apartment watcher)"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lines, self.links, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a" and dict(attrs).get("href"):
            self.links.append(dict(attrs)["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.lines.append(" ".join(data.split()))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(html):
    p = PageParser()
    p.feed(html)
    return p.lines, p.links


def fingerprint(lines, links):
    text = "\n".join(l for l in lines if not l.startswith("©")) + "\n" + "\n".join(sorted(set(links)))
    return hashlib.sha256(text.encode()).hexdigest()


def notify(title, message, click=URL_INFO, priority="urgent"):
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC fehlt (GitHub Secret nicht gesetzt?)")
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Click": click, "Tags": "house,rotating_light"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=20).read()
    print("Push gesendet:", title)


def main():
    if os.environ.get("TEST", "").lower() == "true":
        notify("Binzmuehle-Watcher: Test", "Push funktioniert.", priority="default")
        return

    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}
    old_state = json.dumps(state, sort_keys=True)

    # Hauptsignal: Hinweis auf der Wohnungsliste verschwunden = Bewerbung offen
    is_open = CLOSED_MARKER not in fetch(URL_LIST)
    if is_open:
        n = state.get("repeats", 0)
        if n < MAX_REPEATS and time.time() - state.get("last_alert", 0) >= REPEAT_AFTER:
            notify("Binzmuehle: BEWERBUNG OFFEN", "Der Hinweis 'niemand kann sich bewerben' ist weg. Jetzt bewerben!", URL_LIST)
            state["repeats"] = n + 1
            state["last_alert"] = int(time.time())
    else:
        state["repeats"] = 0

    # Zusatzsignal: Änderungen auf Vermietungs- und Dokumente-Seite
    for key, url in (("info", URL_INFO), ("docs", URL_DOCS)):
        try:
            lines, links = parse(fetch(url))
        except Exception as e:
            print("Warnung:", url, e)
            continue
        fp = fingerprint(lines, links)
        if state.get(key) and state[key] != fp:
            hint = next((l for l in lines if "Bewerbungszeitraum" in l), "Seite wurde geändert")
            notify("Binzmuehle: Seite geaendert", hint, url, priority="high")
        state[key] = fp

    if json.dumps(state, sort_keys=True) != old_state:
        STATE_FILE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
