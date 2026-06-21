#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TACHELES. — tägliche Briefing-Pipeline
--------------------------------------
Ablauf:  RSS-Feeds holen  ->  Claude kuratiert & schreibt (Haus-Stimme)
         ->  E-Mail rendern  ->  via Resend verschicken.

Läuft als GitHub-Action jeden Morgen. Braucht 4 Secrets (siehe README):
  ANTHROPIC_API_KEY, RESEND_API_KEY, FROM_EMAIL, TO_EMAIL
"""

import os, sys, json, re, html, time, datetime, urllib.request
from zoneinfo import ZoneInfo
import feedparser
import requests

# ------------------------------------------------------------------ KONFIG
# Deine Nachrichtenquellen. Einfach Zeilen ändern/ergänzen — Reihenfolge egal.
FEEDS = [
    ("Tagesschau",  "https://www.tagesschau.de/index~rss2.xml"),
    ("WELT",        "https://www.welt.de/feeds/topnews.rss"),
    ("Guardian",    "https://www.theguardian.com/world/rss"),
    ("CNBC Markets","https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("The Verge",   "https://www.theverge.com/rss/index.xml"),
    # Reddit kann aus CI heraus zicken; läuft mit eigenem User-Agent meist trotzdem:
    ("r/wallstreetbets", "https://www.reddit.com/r/wallstreetbets/hot/.rss"),
]

MODEL              = "claude-sonnet-4-6"   # günstiger Tausch: "claude-haiku-4-5-20251001"
HOURS_BACK         = 30                     # nur Meldungen der letzten ~30 h
MAX_ITEMS_PER_FEED = 8
MAX_CANDIDATES     = 45                     # so viel Rohmaterial geht an Claude
TIMEZONE           = ZoneInfo("Europe/Berlin")
SEND_HOUR          = 6                      # Versandstunde (lokal); DST wird automatisch beachtet
USER_AGENT         = "TachelesBriefing/1.0 (persönliches Projekt)"

# ------------------------------------------------------------- HAUS-STIMME
SYSTEM_PROMPT = """\
Du bist die Chefredaktion von „TACHELES.", einem deutschsprachigen Morgenbriefing.
Haltung: liberal-konservativ, marktfreundlich, pointiert, wach, ohne Beruhigungspille —
skeptisch gegenüber Staatseingriffen und Phrasen, freundlich zu Disziplin und Eigenverantwortung.
Schreibe knapp, treffsicher, mit Biss, aber niemals hetzerisch und ohne Gruppen herabzuwürdigen.

HARTE REGELN:
- Nutze AUSSCHLIESSLICH die unten gelieferten Meldungen als Faktenbasis. Erfinde nichts dazu.
  Wenn etwas unklar ist, lass es weg. Lieber wenige starke Themen als viele schwache.
- Fasse jede Meldung in EIGENEN Worten zusammen (kein Abschreiben), je 1–3 Sätze.
- Der Märkte-Teil BERICHTET nur (was Notenbanken, Analysten, der Schwarm sagen) — er gibt
  KEINE Kauf-/Verkaufsempfehlung. Keine Anlageberatung, nirgends.
- "take" ist eine einzige pointierte Meinungszeile in der Haus-Stimme (Fakt und Meinung trennen).
- Wähle hart aus: nur die wirklich wichtigen Themen. Filtern ist die Aufgabe.

AUSGABEFORMAT — sehr wichtig:
- Antworte mit GÜLTIGEM JSON: kein Text davor oder danach, KEINE Kommentare, KEINE //-Zeilen,
  KEIN ```-Codeblock.
- Für Anführungszeichen INNERHALB der Texte nutze immer typografische „ und " —
  niemals gerade Zoll-Zeichen, sonst zerbricht das JSON.

Struktur (genau diese Schlüssel):
{
 "lage": ["", "", "", ""],
 "aufmacher": {"kicker":"", "headline":"", "body":"", "take":"", "quelle":"", "url":""},
 "maerkte": [{"headline":"", "body":"", "quelle":"", "url":""}],
 "welt": [{"headline":"", "body":"", "take":"", "quelle":"", "url":""}],
 "schwarm": {"body":"", "take":"", "quelle":"", "url":""},
 "sport": {"headline":"", "body":"", "quelle":"", "url":""}
}
Umfang — fülle das Briefing SUBSTANZIELL und nutze das vorhandene Material aus:
"lage" genau 4 Zeilen; "aufmacher" mit 2–4 Sätzen im body; "maerkte" 3–4 Einträge;
"welt" 4–6 Einträge; "schwarm" füllen, sobald Social-/Reddit-Material vorhanden ist;
"sport" als Schlusspunkt, wenn es etwas hergibt. Jeder body 1–3 Sätze, nie nur ein Halbsatz.
"""

# --------------------------------------------------------------- FUNKTIONEN
def fetch_items():
    """Holt aktuelle Einträge aus allen Feeds und gibt eine flache Liste zurück."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=HOURS_BACK)
    items = []
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            count = 0
            for e in feed.entries:
                if count >= MAX_ITEMS_PER_FEED:
                    break
                # Datum prüfen, falls vorhanden
                when = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                if when:
                    dt = datetime.datetime(*when[:6], tzinfo=datetime.timezone.utc)
                    if dt < cutoff:
                        continue
                title = getattr(e, "title", "").strip()
                summary = getattr(e, "summary", "") or ""
                # HTML aus der Zusammenfassung grob entfernen und kürzen
                summary = " ".join(summary.replace("<", " <").split())
                summary = summary.replace("&nbsp;", " ")
                if len(summary) > 300:
                    summary = summary[:300] + "…"
                link = getattr(e, "link", "")
                if title:
                    items.append({"source": source, "title": title,
                                  "summary": summary, "url": link})
                    count += 1
        except Exception as ex:
            print(f"[warn] Feed '{source}' übersprungen: {ex}", file=sys.stderr)
    return items[:MAX_CANDIDATES]


def candidates_text(items):
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['source']}] {it['title']}\n   {it['summary']}\n   URL: {it['url']}")
    return "\n".join(lines)


def call_claude(items):
    """Schickt das Rohmaterial an Claude und bekommt das kuratierte Briefing als JSON zurück."""
    today = datetime.datetime.now(TIMEZONE).strftime("%A, %d. %B %Y")
    user = (f"Heutiges Datum: {today}.\n\n"
            f"Hier sind die aktuellen Meldungen als Rohmaterial. Kuratiere daraus das "
            f"TACHELES.-Briefing nach deinen Regeln:\n\n{candidates_text(items)}")
    tool = {
        "name": "briefing",
        "description": "Gibt das fertig kuratierte Morgenbriefing strukturiert zurück.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lage": {"type": "array", "items": {"type": "string"}},
                "aufmacher": {"type": "object", "properties": {
                    "kicker": {"type": "string"}, "headline": {"type": "string"},
                    "body": {"type": "string"}, "take": {"type": "string"},
                    "quelle": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["headline", "body", "take"]},
                "maerkte": {"type": "array", "items": {"type": "object", "properties": {
                    "headline": {"type": "string"}, "body": {"type": "string"},
                    "quelle": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["headline", "body"]}},
                "welt": {"type": "array", "items": {"type": "object", "properties": {
                    "headline": {"type": "string"}, "body": {"type": "string"},
                    "take": {"type": "string"}, "quelle": {"type": "string"},
                    "url": {"type": "string"}}, "required": ["headline", "body"]}},
                "schwarm": {"type": "object", "properties": {
                    "body": {"type": "string"}, "take": {"type": "string"},
                    "quelle": {"type": "string"}, "url": {"type": "string"}}},
                "sport": {"type": "object", "properties": {
                    "headline": {"type": "string"}, "body": {"type": "string"},
                    "quelle": {"type": "string"}, "url": {"type": "string"}}},
            },
            "required": ["lage", "aufmacher", "maerkte", "welt"],
        },
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 5000,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": "briefing"},
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "briefing":
            return block["input"]
    raise RuntimeError("Keine strukturierte Antwort erhalten:\n" + json.dumps(data)[:800])


# ------------------------------------------------------------- E-MAIL-RENDER
def as_dict(x):
    return x if isinstance(x, dict) else {}

def as_list(x):
    if isinstance(x, list):
        return x
    return [] if x in (None, "") else [x]

def as_str(x):
    return "" if (x is None or isinstance(x, (dict, list))) else str(x)

def _item(x):
    """Jeder Eintrag wird zu einem dict (ein blanker String wird zum Fließtext)."""
    return x if isinstance(x, dict) else {"body": as_str(x)}

def esc(s):
    return html.escape(as_str(s))

def src_link(item):
    item = as_dict(item)
    url, quelle = as_str(item.get("url")), as_str(item.get("quelle"))
    if url:
        return (f' <a href="{esc(url)}" style="color:#b9b4a8;text-decoration:none;'
                f'border-bottom:1px solid #3a3a42">{esc(quelle or "Quelle")} ↗</a>')
    if quelle:
        return f' <span style="color:#948f85">{esc(quelle)}</span>'
    return ""

def take_box(text):
    text = as_str(text)
    if not text:
        return ""
    return (f'<div style="border-left:3px solid #ff4326;background:#241310;padding:12px 16px;'
            f'margin:14px 0 0">'
            f'<div style="font:700 10px Arial,sans-serif;letter-spacing:.18em;color:#ff6b54;'
            f'margin-bottom:5px">DER TAKE</div>'
            f'<div style="font:italic 16px Georgia,serif;color:#f0ece2;line-height:1.45">{esc(text)}</div></div>')

def kicker(label, rep=""):
    extra = (f'<span style="color:#948f85;font-weight:600;letter-spacing:.1em"> — {esc(rep)}</span>'
             if rep else "")
    return (f'<div style="font:700 12px Arial,sans-serif;letter-spacing:.18em;color:#ff4326;'
            f'text-transform:uppercase;margin:34px 0 14px;border-bottom:1px solid #2c2c33;'
            f'padding-bottom:8px">{esc(label)}{extra}</div>')

def item_block(it, with_take=False):
    it = _item(it)
    head, body, take = as_str(it.get("headline")), as_str(it.get("body")), as_str(it.get("take"))
    h = (f'<div style="font:700 19px Arial,sans-serif;color:#f3efe6;text-transform:uppercase;'
         f'margin:0 0 6px">{esc(head)}</div>') if head else ""
    p = (f'<div style="font:16px/1.55 Georgia,serif;color:#cdc7bb">{esc(body)}'
         f'{src_link(it)}</div>')
    mt = ""
    if with_take and take:
        mt = (f'<div style="font:italic 15px Georgia,serif;color:#948f85;margin-top:6px">'
              f'{esc(take)}</div>')
    return f'<div style="padding:15px 0;border-top:1px solid #23232a">{h}{p}{mt}</div>'

def render_email(d):
    d = as_dict(d)
    today = datetime.datetime.now(TIMEZONE).strftime("%A · %d. %B %Y")

    lage = "".join(
        f'<li style="margin:0 0 9px;padding-left:18px;position:relative;font:16px/1.45 Georgia,serif;color:#e7e2d7">'
        f'<span style="position:absolute;left:0;top:8px;width:6px;height:6px;background:#ff4326;'
        f'display:inline-block"></span>{esc(x)}</li>'
        for x in as_list(d.get("lage")) if as_str(x)
    )

    auf = _item(d.get("aufmacher"))
    head, body = as_str(auf.get("headline")), as_str(auf.get("body"))
    aufmacher = ((
        kicker("Aufmacher", as_str(auf.get("kicker"))) +
        (f'<div style="font:800 30px/1.05 Arial,Helvetica,sans-serif;color:#f6f2e9;text-transform:uppercase;'
         f'margin:0 0 12px">{esc(head)}</div>' if head else "") +
        f'<div style="font:18px/1.55 Georgia,serif;color:#d8d2c6">{esc(body)}{src_link(auf)}</div>'
        + take_box(auf.get("take"))
    ) if (head or body) else "")

    maerkte_items = [_item(x) for x in as_list(d.get("maerkte"))]
    maerkte = (kicker("Märkte & Geld", "Bericht, kein Rat") +
               "".join(item_block(it) for it in maerkte_items)) if maerkte_items else ""

    welt_items = [_item(x) for x in as_list(d.get("welt"))]
    welt = (kicker("Welt") +
            "".join(item_block(it, with_take=True) for it in welt_items)) if welt_items else ""

    schwarm = ""
    sw = _item(d.get("schwarm"))
    if as_str(sw.get("body")):
        schwarm = (kicker("Der Schwarm · Reddit") +
                   f'<div style="background:#16161a;border:1px solid #2c2c33;padding:18px">'
                   f'<div style="font:16px/1.55 Georgia,serif;color:#d8d2c6">{esc(as_str(sw.get("body")))}{src_link(sw)}</div>'
                   + take_box(sw.get("take")) + "</div>")

    sport = ""
    sp = _item(d.get("sport"))
    if as_str(sp.get("headline")) or as_str(sp.get("body")):
        sport = kicker("Schlusspunkt") + item_block(sp)

    return f"""\
<!DOCTYPE html><html><body style="margin:0;background:#0c0c0e">
<div style="max-width:600px;margin:0 auto;background:#0c0c0e;padding:28px 24px 40px;
            font-family:Georgia,serif;color:#f3efe6">
  <div style="font:700 11px Arial,sans-serif;letter-spacing:.16em;text-transform:uppercase;
              color:#948f85;margin-bottom:14px">Morgenbriefing · Ausgabe</div>
  <div style="font:900 50px Arial,Helvetica,sans-serif;letter-spacing:-1px;text-transform:uppercase;
              color:#f6f2e9;line-height:.9">TACHELES<span style="color:#ff4326">.</span></div>
  <div style="border-top:3px solid #f6f2e9;margin-top:12px;padding-top:9px;
              font:700 12px Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#948f85">
     {esc(today)}</div>
  <div style="font:italic 19px Georgia,serif;color:#e7e2d7;margin:18px 0 0">
     Die Welt vor dem ersten Espresso — kuratiert, eingeordnet, ohne Beruhigungspille.</div>

  <div style="border:1px solid #2c2c33;padding:16px 18px;margin-top:24px">
     <div style="font:700 11px Arial,sans-serif;letter-spacing:.18em;text-transform:uppercase;
                 color:#948f85;margin-bottom:11px">Die Lage in 30 Sekunden</div>
     <ul style="list-style:none;margin:0;padding:0">{lage}</ul>
  </div>

  {aufmacher}{maerkte}{welt}{schwarm}{sport}

  <div style="margin-top:38px;border-top:1px solid #2c2c33;padding-top:16px;
              font:12px/1.6 Arial,sans-serif;color:#948f85">
     <b style="color:#cdc7bb">TACHELES.</b> ist ein automatisch erzeugtes Briefing. Die „Takes" sind die
     redaktionelle Stimme des Produkts — keine Anlage- oder Wahlempfehlung. Der Märkte-Teil berichtet,
     er rät nicht zum Kauf oder Verkauf. Fakten in eigenen Worten zusammengefasst, Quellen verlinkt.
  </div>
</div></body></html>"""


def send_email(html_body):
    today = datetime.datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                 "Content-Type": "application/json"},
        json={
            "from": os.environ["FROM_EMAIL"],
            "to": [os.environ["TO_EMAIL"]],
            "subject": f"TACHELES. · Dein Morgenbriefing · {today}",
            "html": html_body,
        },
        timeout=60,
    )
    if r.status_code >= 400:
        print(f"[fehler] Resend antwortete {r.status_code}: {r.text[:600]}", file=sys.stderr)
    r.raise_for_status()
    print("[ok] E-Mail verschickt:", r.json().get("id", ""))


def main():
    tz = TIMEZONE
    forced = (os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
              or os.getenv("FORCE_SEND") == "1")

    # 1) Inhalt zuerst vorbereiten (dauert nur Sekunden) ...
    items = fetch_items()
    print(f"[info] {len(items)} Meldungen gesammelt.")
    if not items:
        print("[abbruch] Keine Meldungen gefunden — kein Versand.")
        return
    briefing = call_claude(items)
    email_html = render_email(briefing)

    # 2) ... dann punktgenau bis 06:00 Berliner Zeit warten und erst dann senden.
    #    Beim manuellen Test (Run workflow) wird sofort gesendet.
    if not forced:
        target = datetime.datetime.now(tz).replace(
            hour=SEND_HOUR, minute=0, second=0, microsecond=0)
        wait = (target - datetime.datetime.now(tz)).total_seconds()
        if wait > 0:
            print(f"[warten] {int(wait)} s bis {SEND_HOUR:02d}:00 Berliner Zeit …")
            time.sleep(wait)
        else:
            print(f"[hinweis] Bereits {int(-wait)} s nach {SEND_HOUR}:00 — sende sofort.")

    send_email(email_html)


if __name__ == "__main__":
    main()
