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
            "unread": "UNREAD" in detail.get("labelIds", []),
        })

    return emails


# ─── Pre-filtro por remitente ─────────────────────────────────────────────────

# Remitentes que van directo a agrupado sin pasar por Haiku
AGRUPADO_KEYWORDS = [
    "allaria", "orta", "rava bursatil", "bull market", "balanz",
    "balfour", "nexo", "compounding", "zacks", "nytimes", "nytdirect",
    "linkedin", "github", "mercor", "apple", "google",
]

# Remitentes que van directo a descartable sin pasar por Haiku
DESCARTABLE_KEYWORDS = [
    "toyota", "fravega", "uber", "pedidosya", "cinemark", "lanacion",
    "equus", "mailing.bna", "coursera", "misionerosdigitales",
    "clubln", "wirecutter", "promociones@", "newsletter@", "noreply@",
]

def pre_filter(emails):
    """Clasifica en Python los correos obvios, devuelve solo los ambiguos a Haiku."""
    to_haiku = []
    agrupado = {}
    descartable = 0

    for email in emails:
        from_lower = email["from"].lower()
        subject_lower = email["subject"].lower()

        matched_agrupado = any(k in from_lower for k in AGRUPADO_KEYWORDS)
        matched_descartable = any(k in from_lower for k in DESCARTABLE_KEYWORDS)

        if matched_agrupado:
            # determinar nombre del grupo
            if "nytimes" in from_lower or "nytdirect" in from_lower:
                grupo = "NYT"
            elif "linkedin" in from_lower:
                grupo = "LinkedIn"
            elif "zacks" in from_lower:
                grupo = "Zacks"
            elif "balfour" in from_lower:
                grupo = "Balfour Capital"
            elif "compounding" in from_lower:
                grupo = "Compounding Quality"
            elif "github" in from_lower:
                grupo = "GitHub"
            elif "mercor" in from_lower:
                grupo = "Mercor"
            elif "apple" in from_lower:
                grupo = "Apple"
            elif "google" in from_lower:
                grupo = "Google"
            elif "allaria" in from_lower or "orta" in from_lower:
                grupo = "Allaria Research"
            elif "bull market" in from_lower:
                grupo = "Bull Market Brokers"
            elif "balanz" in from_lower:
                grupo = "Balanz"
            elif "nexo" in from_lower:
                grupo = "Nexo"
            elif "rava" in from_lower:
                grupo = "Rava Bursátil"
            else:
                grupo = email["from"].split("<")[0].strip()[:30]
            agrupado[grupo] = agrupado.get(grupo, 0) + 1
        elif matched_descartable:
            descartable += 1
        else:
            to_haiku.append(email)

    return to_haiku, agrupado, descartable


# ─── Claude Haiku ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Sos un asistente que clasifica correos para Mariano Poledo,
economista consultor independiente en Buenos Aires (CABA).

Tu tarea es clasificar cada correo en una de estas cuatro categorías:

🔴 IMPORTANTE: correos personales o de trabajo con remitentes reales dirigidos a Mariano directamente,
correos del BID o relacionados a licitaciones/oportunidades profesionales, eventos o convocatorias
relevantes para un economista consultor. También siniestros, consorcios, o asuntos urgentes personales.

🟡 INTERESANTE: información útil pero no urgente. Confirmaciones de transacciones financieras
(Mercado Pago, Balanz, bancos, transferencias). Noticias económicas relevantes,
novedades de mercado de La Nación, operatoria de mercados de brokers locales.

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


def classify_emails(emails, agrupado_prefiltro, descartable_prefiltro):
    """Llama a Claude Haiku solo con los correos ambiguos."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    emails_text = "\n\n".join([
        f"FROM: {e['from']}\nSUBJECT: {e['subject']}\nSNIPPET: {e['snippet']}"
        for e in emails
    ])

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Clasificá estos correos:\n\n{emails_text}"}
        ],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    classified = json.loads(raw.strip())

    # cruzar el campo unread desde los emails originales
    unread_map = {e["subject"]: e["unread"] for e in emails}
    for categoria in ["importante", "interesante"]:
        for item in classified.get(categoria, []):
            item["unread"] = unread_map.get(item["subject"], False)

    # mergear agrupado y descartable del pre-filtro
    for grupo, count in agrupado_prefiltro.items():
        classified["agrupado"][grupo] = classified["agrupado"].get(grupo, 0) + count
    classified["descartable"] = classified.get("descartable", 0) + descartable_prefiltro

    return classified


# ─── Formateo del mensaje ──────────────────────────────────────────────────────

def format_message(classified, date_str):
    lines = [f"📬 *Gmail Digest — {date_str}*\n"]

    # 🔴 Importante
    lines.append("🔴 *Para prestar atención*")
    if classified.get("importante"):
        for item in classified["importante"]:
            sender = item["from"].split("<")[0].strip()
            unread_mark = " 🔔" if item.get("unread") else ""
            lines.append(f"• *{sender}* — {item['subject']}{unread_mark}")
            lines.append(f"  _{item['summary']}_")
    else:
        lines.append("_Ninguno_")

    lines.append("")

    # 🟡 Interesante
    lines.append("🟡 *Potencialmente interesante*")
    if classified.get("interesante"):
        for item in classified["interesante"]:
            sender = item["from"].split("<")[0].strip()
            unread_mark = " 🔔" if item.get("unread") else ""
            lines.append(f"• *{sender}* — {item['subject']}{unread_mark}")
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
    to_haiku, agrupado_pre, descartable_pre = pre_filter(emails)
    print(f"  → Pre-filtro: {len(to_haiku)} correos ambiguos a Haiku, {sum(agrupado_pre.values())} agrupados, {descartable_pre} descartables")
    classified = classify_emails(to_haiku, agrupado_pre, descartable_pre)

    message = format_message(classified, date_str)
    print("  → Enviando por Telegram...")
    send_telegram(message)
    print("  ✅ Digest enviado")


if __name__ == "__main__":
    main()
