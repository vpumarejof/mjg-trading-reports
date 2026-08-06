import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_email(subject, html_body):
    from_email = os.environ["OUTLOOK_EMAIL"]
    password = os.environ["OUTLOOK_APP_PASSWORD"]
    raw = os.environ.get("REPORT_EMAIL", from_email)
    recipients = [r.strip() for r in raw.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.office365.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(from_email, password)
        server.sendmail(from_email, recipients, msg.as_string())

    print(f"Email sent: {subject} -> {', '.join(recipients)}")
