#!/usr/bin/env python3
"""Binzmühle-Watcher für GitHub Actions: ein Durchlauf pro Aufruf, Push via ntfy.

Durchsucht die ganze Website binzmuehle.ch (alle verlinkten Unterseiten) und meldet
JEDE Änderung an Text oder Links. Dazu: Alarm "Bewerbung offen", tägliches
Lebenszeichen und Fehlermeldung, wenn die Seite nicht lesbar ist.
"""
import json
import os
import sys
import time
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
import urllib.request

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

HOST = "binzmuehle.ch"
URL_HOME = "https://binzmuehle.ch/"
URL_LIST = "https://binzmuehle.ch/vermietung/wohnungen"
URL_INFO = "https://binzmuehle.ch/vermietung"
# Einzelne Seiten ausserhalb der Domain, die ebenfalls beobachtet werden (ohne Unterseiten)
EXTRA_URLS = ["https://www.bg-gisa.ch/projekt-binzmuehle"]   # "News" im Menü

CLOSED_MARKER = "Momentan kann sich niemand auf eine Wohnung bewerben"
MAX_PAGES = 40            # Obergrenze an Seiten pro Durchlauf
MAX_NOTIFY = 5            # höchstens so viele Einzelmeldungen pro Durchlauf
MAX_REPEATS = 12          # Wiederholungen der Meldung "Bewerbung offen"
REPEAT_AFTER = 600        # Sekunden zwischen den Wiederholungen
FAIL_ALERT_AFTER = 3      # Fehlermeldung nach so vielen fehlgeschlagenen Prüfungen in Folge
HEARTBEAT_HOUR = 8        # Tagesmeldung frühestens ab dieser Uhrzeit (Schweizer Zeit)
MAX_MSG = 1500            # maximale Länge einer Änderungsmeldung
PAUSE = 0.3               # Sekunden Pause zwischen Seitenabrufen
STATE_FILE = Path(__file__).with_name("state.json")
UA = "Mozilla/5.0 (private apartment watcher)"
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".docx", ".xlsx", ".mp4", ".ico", ".css", ".js")
# Alte State-Schlüssel früherer Versionen -> URL (für die Übernahme)
OLD_KEYS = {"home": URL_HOME, "list": URL_LIST, "info": URL_INFO,
            "docs": "https://binzmuehle.ch/vermietung/dokumente-fuer-bewerbung"}


class PageParser(HTMLParser):
    """Sammelt sichtbaren Text (Tabellenzeilen als eine Zeile) und Links."""

    def __init__(self):
        super().__init__()
        self.lines, self.links = [], []
        self._skip = 0
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a" and dict(attrs).get("href"):
            self.links.append(dict(attrs)["href"])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            row = " | ".join(c for c in self._row if c)
            if row:
                self.lines.append(row)
            self._row = None

    def handle_data(self, data):
        if self._skip or not data.strip():
            return
        text = " ".join(data.split())
        if self._cell is not None:
            self._cell.append(text)
        else:
            self.lines.append(text)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(html):
    p = PageParser()
    p.feed(html)
    lines = [l for l in p.lines if not l.startswith("©")]
    snap = lines + ["LINK " + l for l in sorted(set(p.links))]
    return snap, p.links


def normalize(href, base):
    """Interne Seite (ohne Query, ohne Dateien) als einheitliche URL, sonst None."""
    u, _ = urldefrag(urljoin(base, href))
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        return None
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != HOST or p.query:
        return None
    path = p.path or "/"
    if path.startswith("/files/") or path.lower().endswith(SKIP_EXT):
        return None
    if path != "/":
        path = path.rstrip("/")
    return f"https://{HOST}{path}"


def crawl(known, prefetched):
    """Liest die Startseite, alle intern verlinkten Seiten und bereits bekannte Seiten."""
    pages, queue, seen = {}, deque([URL_HOME]), {URL_HOME}
    for u in sorted(known):
        if u not in seen and u not in EXTRA_URLS:
            seen.add(u)
            queue.append(u)
    while queue and len(pages) < MAX_PAGES:
        url = queue.popleft()
        try:
            html = prefetched.get(url)
            if html is None:
                time.sleep(PAUSE)
                html = fetch(url)
            snap, links = parse(html)
        except Exception as e:
            print("Warnung:", url, e)
            continue
        pages[url] = snap
        for href in links:
            n = normalize(href, url)
            if n and n not in seen:
                seen.add(n)
                queue.append(n)
    for url in EXTRA_URLS:
        try:
            time.sleep(PAUSE)
            pages[url] = parse(fetch(url))[0]
        except Exception as e:
            print("Warnung:", url, e)
    return pages


def describe_change(old, new):
    added = [x for x in new if x not in old]
    removed = [x for x in old if x not in new]
    parts = []
    if added:
        parts.append("NEU/GEAENDERT:\n" + "\n".join("+ " + x for x in added))
    if removed:
        parts.append("ENTFERNT/ALT:\n" + "\n".join("- " + x for x in removed))
    msg = "\n\n".join(parts) or "Reihenfolge oder Inhalt wurde geaendert."
    return msg if len(msg) <= MAX_MSG else msg[:MAX_MSG] + "\n[...gekuerzt]"


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


def zurich_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Zurich"))
    except Exception:
        return datetime.now()


def home_hint(snap):
    """Zeile(n) der Startseite mit dem Bewerbungszeitraum, für die Tagesmeldung."""
    for i, line in enumerate(snap or []):
        if line.startswith("Die Bewerbung"):
            return " ".join(snap[i:i + 4])
    return "Startseite: Bewerbungszeitraum nicht gefunden"


def main():
    if os.environ.get("TEST", "").lower() == "true":
        notify("Binzmuehle-Watcher: Test", "Push funktioniert.", priority="default")
        return

    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}
    # Übernahme aus früheren Versionen (Schlüssel -> URL)
    pages_state = state.setdefault("pages", {})
    for key, url in OLD_KEYS.items():
        if isinstance(state.get(key), list):
            pages_state.setdefault(url, state.pop(key))
    old_state = json.dumps(state, sort_keys=True)

    def save():
        if json.dumps(state, sort_keys=True) != old_state:
            STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))

    # Wohnungsliste laden. Schlägt das mehrfach in Folge fehl, Fehlermeldung senden.
    try:
        list_html = fetch(URL_LIST)
    except Exception as e:
        state["fails"] = state.get("fails", 0) + 1
        print("Fehler beim Laden:", e)
        if state["fails"] == FAIL_ALERT_AFTER:
            notify("Binzmuehle-Watcher: Seite nicht lesbar",
                   f"{FAIL_ALERT_AFTER} Pruefungen in Folge fehlgeschlagen. Bitte selbst nachschauen.",
                   URL_INFO, priority="high")
        save()
        return
    if state.get("fails", 0) >= FAIL_ALERT_AFTER:
        notify("Binzmuehle-Watcher: wieder erreichbar", "Die Seite ist wieder lesbar.", URL_INFO, priority="default")
    state["fails"] = 0

    # Hauptsignal: Hinweis auf der Wohnungsliste verschwunden = Bewerbung offen
    is_open = CLOSED_MARKER not in list_html
    if is_open:
        n = state.get("repeats", 0)
        if n < MAX_REPEATS and time.time() - state.get("last_alert", 0) >= REPEAT_AFTER:
            notify("Binzmuehle: BEWERBUNG OFFEN", "Der Hinweis 'niemand kann sich bewerben' ist weg. Jetzt bewerben!", URL_LIST)
            state["repeats"] = n + 1
            state["last_alert"] = int(time.time())
    else:
        state["repeats"] = 0

    # Ganze Website durchsuchen und jede Änderung melden
    pages = crawl(pages_state.keys(), {URL_LIST: list_html})
    changes = []
    baselined = state.get("baselined", False)
    for url, snap in pages.items():
        old = pages_state.get(url)
        if baselined:
            if old is None:
                changes.append((url, "NEUE SEITE gefunden:\n" + "\n".join(snap[:12])[:MAX_MSG]))
            elif old != snap:
                changes.append((url, describe_change(old, snap)))
        pages_state[url] = snap
    state["baselined"] = True   # der allererste Durchlauf speichert nur den Ausgangszustand

    for url, msg in changes[:MAX_NOTIFY]:
        path = urlparse(url).path or "/"
        notify("Binzmuehle: Seite geaendert", f"{url}\n\n{msg}" if path == "/" else f"{path}\n\n{msg}", url, priority="high")
    if len(changes) > MAX_NOTIFY:
        rest = "\n".join(u for u, _ in changes[MAX_NOTIFY:])
        notify("Binzmuehle: weitere Aenderungen", rest, URL_HOME, priority="high")

    # Tägliches Lebenszeichen (einmal pro Tag, ab 8 Uhr Schweizer Zeit)
    now = zurich_now()
    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR and state.get("last_heartbeat") != today:
        status = "Bewerbung OFFEN" if is_open else "Bewerbung noch geschlossen"
        notify("Binzmuehle-Watcher laeuft",
               f"{status}.\n{home_hint(pages_state.get(URL_HOME))}\nSeiten ueberwacht: {len(pages)}",
               URL_HOME, priority="low")
        state["last_heartbeat"] = today

    save()


if __name__ == "__main__":
    main()
