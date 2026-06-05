import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_email(subject, html_body):
    smtp_user = os.environ["OUTLOOK_EMAIL"]
    smtp_password = os.environ["OUTLOOK_PASSWORD"]
    raw = os.environ.get("REPORT_EMAIL", smtp_user)
    recipients = [r.strip() for r in raw.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.office365.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, recipients, msg.as_string())

    print(f"Email sent: {subject} -> {', '.join(recipients)}")
