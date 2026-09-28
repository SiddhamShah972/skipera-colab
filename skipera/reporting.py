import os
import smtplib
from email.message import EmailMessage


def send_daily_report(events: list[dict]) -> None:
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    recipient = os.getenv("REPORT_EMAIL_TO", "siddhamshah972@gmail.com")

    if not smtp_username or not smtp_password:
        return

    videos = [event for event in events if event["kind"] == "video"]
    quizzes = [event for event in events if event["kind"] == "quiz"]
    other_items = [event for event in events if event["kind"] == "other"]

    lines = [
        "Skipera daily report",
        "",
        f"Videos watched: {len(videos)}",
        f"Quizzes solved: {len(quizzes)}",
        f"Other completed items: {len(other_items)}",
        "",
        "Completed details:",
    ]

    if not events:
        lines.append("No items were completed in this run.")
    else:
        for event in events:
            lines.append(
                f"- {event['kind'].title()}: course={event['course']}; "
                f"module={event['module']}; item={event['name']} ({event['item_type']})"
            )

    message = EmailMessage()
    message["Subject"] = "Skipera daily report"
    message["From"] = smtp_username
    message["To"] = recipient
    message.set_content("\n".join(lines))

    with smtplib.SMTP_SSL(
        os.getenv("SMTP_HOST", "smtp.gmail.com"),
        int(os.getenv("SMTP_PORT", "465")),
        timeout=30,
    ) as smtp:
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)
