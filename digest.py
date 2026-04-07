"""
Gmail Daily Digest
Clasifica los correos del dia con Claude Haiku y envia el resumen por Telegram.
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BUENOS_AIRES = timezone(timedelta(hours=-3))


def get_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def fetch_emails(service):
    now = datetime.now(BUENOS_AIRES)
    since_unix = int((now - timedelta(hours=24)).timestamp())
    result = service.users().messages().list(
        userId="me",
        q=f"after:{since_unix}",
        maxResults=80,
    ).execute()
    emails = []
    for msg in result.get("messages", []):
        detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        emails.append({
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "snippet": detail.get("snippet", "")[:200],
            "unread": "UNREAD" in detail.get("labelIds", []),
        })
    return emails


AGRUPADO_KEYS = [
    "allaria", "orta", "rava bursatil", "bullmarket", "bull market",
    "balanz", "balfour", "nexo", "compounding", "zacks",
    "nytimes", "nytdirect", "linkedin", "github", "mercor", "apple", "google",
]

DESCARTABLE_KEYS = [
    "toyota", "fravega", "uber", "pedidosya", "cinemark",
    "equus", "mailing.bna", "coursera", "misionerosdigitales",
    "clubln", "wirecutter",
]

GRUPO_NOMBRES = {
    "nytimes": "NYT", "nytdirect": "NYT",
    "linkedin": "LinkedIn", "zacks": "Zacks",
    "balfour": "Balfour Capital", "compounding": "Compounding Quality",
    "github": "GitHub", "mercor": "Mercor", "apple": "Apple",
    "google": "Google", "allaria": "Allaria Research", "orta": "Allaria Research",
    "bull market": "Bull Market Brokers", "bullmarket": "Bull Market Brokers",
    "balanz": "Balanz", "nexo": "Nexo", "rava": "Rava Bursatil",
}


def pre_filter(emails):
    to_haiku, agrupado, descartable = [], {}, 0
    for e in emails:
        fl = e["from"].lower()
        if any(k in fl for k in AGRUPADO_KEYS):
            grupo = next((v for k, v in GRUPO_NOMBRES.items() if k in fl), fl.split("<")[0].strip()[:25])
            agrupado[grupo] = agrupado.get(grupo, 0) + 1
        elif any(k in fl for k in DESCARTABLE_KEYS):
            descartable += 1
        else:
            to_haiku.append(e)
    return to_haiku, agrupado, descartable


SYSTEM_PROMPT = """Clasificas correos para Mariano Poledo, economista consultor en Buenos Aires.

IMPORTANTE: correos personales dirigidos a Mariano, BID, licitaciones, siniestros, consorcios, urgentes.
INTERESANTE: transacciones financieras (Mercado Pago, bancos), noticias economicas, brokers locales.
AGRUPADO: newsletters (NYT, LinkedIn, Zacks, Balfour, Compounding, GitHub, Apple, Google, Allaria, Bull Market, Balanz, Rava).
DESCARTABLE: promociones, spam, newsletters sin relevancia profesional.

Responde UNICAMENTE con JSON valido:
{"importante": [{"from": "...", "subject": "...", "summary": "una linea"}],
"interesante": [{"from": "...", "subject": "...", "summary": "una linea"}],
"agrupado": {"nombre": N},
"descartable": N}"""


def classify_emails(emails, agrupado_pre, descartable_pre):
    if not emails:
        return {"importante": [], "interesante": [], "agrupado": agrupado_pre, "descartable": descartable_pre}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    emails_text = "\n\n".join([
        f"FROM: {e['from']}\nSUBJECT: {e['subject']}\nSNIPPET: {e['snippet']}"
        for e in emails
    ])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Clasifica estos correos:\n\n{emails_text}"}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    result = json.loads(raw.strip())

    unread_map = {e["subject"]: e["unread"] for e in emails}
    for cat in ["importante", "interesante"]:
        for item in result.get(cat, []):
            item["unread"] = unread_map.get(item["subject"], False)

    for grupo, n in agrupado_pre.items():
        result["agrupado"][grupo] = result["agrupado"].get(grupo, 0) + n
    result["descartable"] = result.get("descartable", 0) + descartable_pre

    return result


def format_message(classified, date_str):
    lines = [f"Gmail Digest - {date_str}\n"]

    lines.append("Importante")
    if classified.get("importante"):
        for item in classified["importante"]:
            sender = item["from"].split("<")[0].strip()
            bell = " (no leido)" if item.get("unread") else ""
            lines.append(f"- {sender}: {item['subject']}{bell}")
            lines.append(f"  {item['summary']}")
    else:
        lines.append("Ninguno")

    lines.append("")
    lines.append("Interesante")
    if classified.get("interesante"):
        for item in classified["interesante"]:
            sender = item["from"].split("<")[0].strip()
            bell = " (no leido)" if item.get("unread") else ""
            lines.append(f"- {sender}: {item['subject']}{bell}")
            lines.append(f"  {item['summary']}")
    else:
        lines.append("Ninguno")

    lines.append("")
    lines.append("Agrupados")
    for nombre, n in classified.get("agrupado", {}).items():
        lines.append(f"- {nombre}: {n}")

    lines.append("")
    lines.append(f"Descartable: {classified.get('descartable', 0)} correos")

    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    resp.raise_for_status()


def main():
    now = datetime.now(BUENOS_AIRES)
    date_str = now.strftime("%d/%m/%Y")
    print(f"[{date_str}] Obteniendo correos...")
    service = get_gmail_service()
    emails = fetch_emails(service)
    print(f"  -> {len(emails)} correos encontrados")

    if not emails:
        send_telegram(f"Gmail Digest - {date_str}\n\nNo hay correos nuevos.")
        return

    to_haiku, agrupado_pre, descartable_pre = pre_filter(emails)
    print(f"  -> Pre-filtro: {len(to_haiku)} a Haiku, {sum(agrupado_pre.values())} agrupados, {descartable_pre} descartables")

    classified = classify_emails(to_haiku, agrupado_pre, descartable_pre)
    message = format_message(classified, date_str)
    print("  -> Enviando por Telegram...")
    send_telegram(message)
    print("  -> Digest enviado")


if __name__ == "__main__":
    main()
