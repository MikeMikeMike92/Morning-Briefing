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

import os, sys, json, html, time, datetime, urllib.request
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
- „take" ist eine einzige pointierte Meinungszeile in der Haus-Stimme (Fakt und Meinung trennen).
- Wähle hart aus: nur die wirklich wichtigen Themen. Filtern ist die Aufgabe.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, ohne Markdown, in genau dieser Struktur:
{
 "lage": ["4 ultrakurze Bullet-Zeilen zur Gesamtlage"],
 "aufmacher": {"kicker":"", "headline":"", "body":"", "take":"", "quelle":"", "url":""},
 "maerkte": [{"headline":"", "body":"", "quelle":"", "url":""}],   // 2-4 Stück, Bericht
 "welt":    [{"headline":"", "body":"", "take":"", "quelle":"", "url":""}],  // 3-5 Stück
 "schwarm": {"body":"", "take":"", "quelle":"", "url":""},          // Reddit/Social-Hype, optional
 "sport":   {"headline":"", "body":"", "quelle":"", "url":""}       // optional, ein Schlusspunkt
}
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
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 3000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        raise RuntimeError("Keine JSON-Antwort von Claude erhalten:\n" + text[:500])
    return json.loads(text[a:b + 1])


# ------------------------------------------------------------- E-MAIL-RENDER
def esc(s):
    return html.escape(str(s or ""))

def src_link(item):
    if item.get("url"):
        return (f' <a href="{esc(item["url"])}" style="color:#9a968c;text-decoration:none;'
                f'border-bottom:1px solid #ddd">{esc(item.get("quelle","Quelle"))} ↗</a>')
    return f' <span style="color:#9a968c">{esc(item.get("quelle",""))}</span>'

def take_box(text):
    if not text:
        return ""
    return (f'<div style="border-left:3px solid #ff4326;background:#fdeee9;padding:10px 14px;'
            f'margin:12px 0 0;border-radius:0 4px 4px 0">'
            f'<div style="font:700 10px Arial,sans-serif;letter-spacing:.16em;color:#ff4326;'
            f'margin-bottom:4px">DER TAKE</div>'
            f'<div style="font:italic 16px Georgia,serif;color:#1a1a1a;line-height:1.4">{esc(text)}</div></div>')

def kicker(label, rep=""):
    extra = (f'<span style="color:#9a968c;font-weight:600;letter-spacing:.1em"> — {esc(rep)}</span>'
             if rep else "")
    return (f'<div style="font:700 12px Arial,sans-serif;letter-spacing:.16em;color:#ff4326;'
            f'text-transform:uppercase;margin:30px 0 12px;border-bottom:1px solid #e7e3d8;'
            f'padding-bottom:8px">{esc(label)}{extra}</div>')

def item_block(it, with_take=False):
    h = (f'<div style="font:700 18px Arial,sans-serif;color:#111;text-transform:uppercase;'
         f'margin:0 0 6px">{esc(it.get("headline",""))}</div>')
    p = (f'<div style="font:16px/1.5 Georgia,serif;color:#2a2a2a">{esc(it.get("body",""))}'
         f'{src_link(it)}</div>')
    mt = ""
    if with_take and it.get("take"):
        mt = (f'<div style="font:italic 15px Georgia,serif;color:#777;margin-top:6px">'
              f'{esc(it["take"])}</div>')
    return f'<div style="padding:14px 0;border-top:1px solid #eee">{h}{p}{mt}</div>'

def render_email(d):
    today = datetime.datetime.now(TIMEZONE).strftime("%A · %d. %B %Y")

    lage = "".join(
        f'<li style="margin:0 0 8px;padding-left:16px;position:relative;font:16px/1.4 Georgia,serif">'
        f'<span style="position:absolute;left:0;top:8px;width:6px;height:6px;background:#ff4326;'
        f'display:inline-block"></span>{esc(x)}</li>'
        for x in d.get("lage", [])
    )

    auf = d.get("aufmacher", {}) or {}
    aufmacher = (
        kicker("Aufmacher", auf.get("kicker", "")) +
        f'<div style="font:700 30px/1.05 Arial,sans-serif;color:#111;text-transform:uppercase;'
        f'margin:0 0 12px">{esc(auf.get("headline",""))}</div>'
        f'<div style="font:18px/1.55 Georgia,serif;color:#222">{esc(auf.get("body",""))}{src_link(auf)}</div>'
        + take_box(auf.get("take", ""))
    ) if auf else ""

    maerkte = (kicker("Märkte & Geld", "Bericht, kein Rat") +
               "".join(item_block(it) for it in d.get("maerkte", []))) if d.get("maerkte") else ""
    welt = (kicker("Welt") +
            "".join(item_block(it, with_take=True) for it in d.get("welt", []))) if d.get("welt") else ""

    schwarm = ""
    sw = d.get("schwarm") or {}
    if sw.get("body"):
        schwarm = (kicker("Der Schwarm · Reddit") +
                   f'<div style="background:#fafafa;border:1px solid #eee;border-radius:6px;padding:18px">'
                   f'<div style="font:16px/1.55 Georgia,serif;color:#222">{esc(sw.get("body",""))}{src_link(sw)}</div>'
                   + take_box(sw.get("take", "")) + "</div>")

    sport = ""
    sp = d.get("sport") or {}
    if sp.get("headline"):
        sport = kicker("Schlusspunkt") + item_block(sp)

    return f"""\
<!DOCTYPE html><html><body style="margin:0;background:#f4f1ea">
<div style="max-width:600px;margin:0 auto;background:#fffdf8;padding:28px 26px 36px;
            font-family:Georgia,serif;color:#1a1a1a">
  <div style="font:700 11px Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;
              color:#9a968c;margin-bottom:14px">Morgenbriefing · Ausgabe</div>
  <div style="font:700 48px Arial,sans-serif;letter-spacing:-.5px;text-transform:uppercase;color:#111">
     TACHELES<span style="color:#ff4326">.</span></div>
  <div style="border-top:3px solid #111;margin-top:10px;padding-top:8px;
              font:600 12px Arial,sans-serif;letter-spacing:.1em;text-transform:uppercase;color:#9a968c">
     {esc(today)}</div>
  <div style="font:italic 19px Georgia,serif;color:#1a1a1a;margin:18px 0 0">
     Die Welt vor dem ersten Espresso — kuratiert, eingeordnet, ohne Beruhigungspille.</div>

  <div style="border:1px solid #e7e3d8;border-radius:4px;padding:16px 18px;margin-top:22px">
     <div style="font:700 11px Arial,sans-serif;letter-spacing:.16em;text-transform:uppercase;
                 color:#9a968c;margin-bottom:10px">Die Lage in 30 Sekunden</div>
     <ul style="list-style:none;margin:0;padding:0">{lage}</ul>
  </div>

  {aufmacher}{maerkte}{welt}{schwarm}{sport}

  <div style="margin-top:34px;border-top:1px solid #e7e3d8;padding-top:16px;
              font:12px/1.6 Arial,sans-serif;color:#9a968c">
     <b style="color:#444">TACHELES.</b> ist ein automatisch erzeugtes Briefing. Die „Takes" sind die
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
