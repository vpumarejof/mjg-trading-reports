import json
import os
import urllib.request


def send_email(subject, html_body):
    api_key = os.environ["SENDGRID_API_KEY"]
    from_email = os.environ["OUTLOOK_EMAIL"]
    raw = os.environ.get("REPORT_EMAIL", from_email)
    recipients = [{"email": r.strip()} for r in raw.split(",") if r.strip()]

    payload = json.dumps({
        "personalizations": [{"to": recipients}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }).encode()

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        pass

    print(f"Email sent: {subject} -> {', '.join(r['email'] for r in recipients)}")
