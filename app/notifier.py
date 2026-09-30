"""Send an email when a new job looks like a fit."""
import html
import re
import smtplib
import ssl
from email.message import EmailMessage

from . import config, db

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_RECIPIENTS = 10


def recipients() -> list[str]:
    """The current user's saved addresses. Only the first account (whoever set up .env)
    falls back to ALERT_EMAIL_TO; other accounts get no email until they add an address."""
    saved = db.get_setting("alert_emails") or []
    if saved or db.uid() != db.first_user_id():
        return saved
    return [a.strip() for a in config.ALERT_EMAIL_TO.split(",") if a.strip()]


def clean_recipients(emails: list[str]) -> list[str]:
    """Trim, check and de-duplicate addresses typed on the dashboard. Raises ValueError."""
    out: list[str] = []
    for e in emails:
        e = e.strip()
        if not e:
            continue
        if not EMAIL_RE.match(e) or len(e) > 254:
            raise ValueError(f"'{e}' doesn't look like an email address.")
        if e.lower() not in (o.lower() for o in out):
            out.append(e)
    if len(out) > MAX_RECIPIENTS:
        raise ValueError(f"Up to {MAX_RECIPIENTS} addresses, please.")
    return out


def _row(label: str, value: str) -> str:
    return (f'<tr><td style="padding:4px 12px 4px 0;color:#5b6678">{html.escape(label)}</td>'
            f'<td style="padding:4px 0;color:#16233a">{html.escape(value)}</td></tr>')


LEVEL_LABELS = {
    "internship": "Internship",
    "fresher": "Fresher / entry level",
    "experienced": "Experienced",
    "senior": "Senior",
}


def send_job_alert(company: dict, job: dict, result: dict) -> None:
    if not config.email_configured():
        raise RuntimeError("Email isn't set up. Add SMTP_USER and SMTP_PASSWORD to .env.")
    to = recipients()
    if not to:
        raise RuntimeError("No address to send alerts to. Add one under Email alerts on the dashboard.")

    fit = result.get("fit_score") or 0
    subject = f"New match at {company['name']}: {job['title']}"
    reasons_html = "".join(f"<li>{html.escape(r)}</li>" for r in result.get("reasons", []))
    body_html = f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:560px;color:#16233a">
      <p style="margin:0 0 4px;color:#0e7c66;font-weight:600">You look eligible for a new opening</p>
      <h2 style="margin:0 0 16px;font-size:20px">{html.escape(job['title'])}</h2>
      <table style="border-collapse:collapse;font-size:14px;margin-bottom:16px">
        {_row("Company", company["name"])}
        {_row("Location", job.get("location") or "Not stated")}
        {_row("Level", LEVEL_LABELS.get(result.get("job_level") or "", "Not sure"))}
        {_row("Fit", f"{fit:.1f} out of 4")}
        {_row("Weakest requirement", f"{result['eligible_p']:.0%} sure you meet it")
                          if result.get("eligible_p") is not None else ""}
      </table>
      <ul style="font-size:14px;padding-left:18px;margin:0 0 20px">{reasons_html}</ul>
      <a href="{html.escape(job['url'])}" style="background:#16233a;color:#fff;padding:10px 16px;
         border-radius:6px;text-decoration:none;display:inline-block">Open the job posting</a>
      <p style="font-size:13px;margin-top:20px"><a href="{config.DASHBOARD_URL}">See it on your dashboard</a></p>
    </div>"""
    body_text = (
        f"{job['title']} at {company['name']}\n"
        f"Location: {job.get('location') or 'Not stated'}\n"
        f"Level: {LEVEL_LABELS.get(result.get('job_level') or '', 'Not sure')}\nFit: {fit:.1f}/4\n\n"
        + "\n".join(f"- {r}" for r in result.get("reasons", []))
        + f"\n\nApply: {job['url']}\nDashboard: {config.DASHBOARD_URL}\n"
    )

    _send(to, subject, body_text, body_html)


def send_password_reset(to: str, username: str, link: str, minutes: int) -> None:
    subject = "Reset your TheWatcher password"
    body_text = (f"Someone asked to reset the password for the TheWatcher account '{username}'.\n\n"
                 f"Set a new password here (the link works once, for {minutes} minutes):\n{link}\n\n"
                 "If this wasn't you, ignore this email. Your password stays the same.\n")
    body_html = f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:520px;color:#16233a">
      <h2 style="margin:0 0 12px;font-size:19px">Reset your password</h2>
      <p style="font-size:14px">Someone asked to reset the password for the TheWatcher account
        <strong>{html.escape(username)}</strong>.</p>
      <a href="{html.escape(link)}" style="background:#16233a;color:#fff;padding:10px 16px;border-radius:6px;
         text-decoration:none;display:inline-block">Set a new password</a>
      <p style="font-size:13px;color:#5b6678;margin-top:18px">The link works once, for {minutes} minutes.
        If this wasn't you, ignore this email. Your password stays the same.</p>
    </div>"""
    _send([to], subject, body_text, body_html)


def _send(to: list[str], subject: str, body_text: str, body_html: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = ", ".join(to)
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=30) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)


def send_test_email() -> None:
    fake_company = {"name": "TheWatcher"}
    fake_job = {"title": "Test alert", "location": "Anywhere", "url": config.DASHBOARD_URL}
    send_job_alert(fake_company, fake_job, {"fit_score": 4, "eligible_p": 1, "job_level": "fresher",
                                            "reasons": ["Your email alerts are working."]})
