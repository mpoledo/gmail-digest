"""
Gmail Daily Digest
Clasifica los correos del día con Claude Haiku y envía el resumen por Telegram.
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic

# ─── Configuración ────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BUENOS_AIRES = timezone(timedelta(hours=-3))

# ─── Gmail ────────────────────────────────────────────────────────────────────

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


def fetch_today_emails(service):
    """Obtiene los correos de las últimas 24 horas usando solo headers y snippets."""
    now = datetime.now(BUENOS_AIRES)
    since = now - timedelta(hours=24)
    since_unix = int(since.timestamp())

    result = service.users().messages().list(
        userId="me",
        q=f"after:{since_unix}",
        maxResults=80,
    ).execute()

    messages = result.get("messages", [])
    emails = []

    for msg in messages:
        detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        emails.append({
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "snippet": detail.get("snippet", "")[:200],
        })

    return emails


# ─── Claude Haiku ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Sos un asistente que clasifica correos para Mariano Poledo,
economista consultor independiente en Buenos Aires (CABA).

Tu tarea es clasificar cada correo en una de estas cuatro categorías:

🔴 IMPORTANTE: correos personales o de trabajo con remitentes reales dirigidos a Mariano directamente, 
correos del BID o relacionados a licitaciones/oportunidades profesionales, eventos o convocatorias relevantes para
un economista consultor. También siniestros, consorcios, o asuntos urgentes personales.

🟡 INTERESANTE: informacion util pero no urgente. Confirmaciones de transacciones
financieras (Mercado Pago, Balanz, bancos, transferencias). Noticias economicas,
novedades de mercado de La Nacion, operatoria de brokers locales.

📦 AGRUPADO: newsletters conocidos que se muestran solo como grupo sin detalle:
Martín Orta / Allaria Research, Google alertas de seguridad, Rava Bursátil, Bull Market Brokers,
Balanz Daily, Balfour Capital, Nexo, Compounding Quality, Zacks, NYT (The Morning / The World /
breaking news / Wirecutter), LinkedIn alertas de empleo, GitHub, Mercor Trust & Safety, Apple.

🗑️ DESCARTABLE: promociones comerciales (Toyota, Fravega, Uber, tiendas, PedidosYa, Cinemark,
Club La Nación, Equus, Banco Nación promos), newsletters sin relevancia profesional
(Coursera, GrabFi, EAFP, Teatro, Misioneros, etc.), spam.

Respondé ÚNICAMENTE con un JSON válido con esta estructura exacta:
{
  "importante": [{"from": "...", "subject": "...", "summary": "una línea de resumen"}],
  "interesante": [{"from": "...", "subject": "...", "summary": "una línea de resumen"}],
  "agrupado": {"nombre_grupo": N},
  "descartable": N
}

Para "agrupado", agrupa por fuente y cuenta cuántos hay de cada una.
Para "descartable", solo el número total.
No incluyas explicaciones fuera del JSON."""


def classify_emails(emails):
    """Llama a Claude Haiku con los headers+snippets para clasificar."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    emails_text = "\n\n".join([
        f"FROM: {e['from']}\nSUBJECT: {e['subject']}\nSNIPPET: {e['snippet']}"
        for e in emails
    ])

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Clasificá estos correos:\n\n{emails_text}"}
        ],
    )

    raw = message.content[0].text.strip()
    # limpiar posibles backticks si el modelo los agrega
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ─── Formateo del mensaje ──────────────────────────────────────────────────────

def format_message(classified, date_str):
    lines = [f"📬 *Gmail Digest — {date_str}*\n"]

    # 🔴 Importante
    lines.append("🔴 *Para prestar atención*")
    if classified.get("importante"):
        for item in classified["importante"]:
            sender = item["from"].split("<")[0].strip()
            lines.append(f"• *{sender}* — {item['subject']}")
            lines.append(f"  _{item['summary']}_")
    else:
        lines.append("_Ninguno_")

    lines.append("")

    # 🟡 Interesante
    lines.append("🟡 *Potencialmente interesante*")
    if classified.get("interesante"):
        for item in classified["interesante"]:
            sender = item["from"].split("<")[0].strip()
            lines.append(f"• *{sender}* — {item['subject']}")
            lines.append(f"  _{item['summary']}_")
    else:
        lines.append("_Ninguno_")

    lines.append("")

    # 📦 Agrupados
    lines.append("📦 *Agrupados*")
    agrupado = classified.get("agrupado", {})
    if agrupado:
        for nombre, cantidad in agrupado.items():
            lines.append(f"• {nombre}: {cantidad}")
    else:
        lines.append("_Ninguno_")

    lines.append("")

    # 🗑️ Descartable
    n = classified.get("descartable", 0)
    lines.append(f"🗑️ *Descartable:* {n} correos ignorados")

    return "\n".join(lines)


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(BUENOS_AIRES)
    date_str = now.strftime("%d/%m/%Y")

    print(f"[{date_str}] Obteniendo correos...")
    service = get_gmail_service()
    emails = fetch_today_emails(service)
    print(f"  → {len(emails)} correos encontrados")

    if not emails:
        send_telegram(f"📬 *Gmail Digest — {date_str}*\n\n_No hay correos nuevos hoy._")
        return

    print("  → Clasificando con Claude Haiku...")
    classified = classify_emails(emails)

    message = format_message(classified, date_str)
    print("  → Enviando por Telegram...")
    send_telegram(message)
    print("  ✅ Digest enviado")


if __name__ == "__main__":
    main()
