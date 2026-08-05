import os
import re
import json
import smtplib
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import requests
import feedparser
from bs4 import BeautifulSoup

# Настройки из environment variables (GitHub Secrets)
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")  # Пароль приложения Gmail
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

FEED_URL = "https://www.hronikatm.com/feed/"
SENT_LOG_FILE = "sent_articles.json"
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 МБ в байтах


def load_sent_urls():
    if os.path.exists(SENT_LOG_FILE):
        try:
            with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_sent_urls(sent_urls):
    with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_urls), f, ensure_ascii=False, indent=2)


def get_article_content(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Основной контейнер статьи на WordPress/hronikatm
    content_div = soup.find("div", class_="entry-content") or soup.find("article")
    if not content_div:
        return None, []

    # Удаляем ненужные блоки (похожие статьи, реклама, скрипты)
    for tag in content_div.find_all(["script", "style", "iframe"]):
        tag.decompose()

    # Поиск потенциальных файлов-вложений (PDF, документы, архивы)
    attachments = []
    for a in content_div.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(pdf|doc|docx|zip|rar|7z|xlsx)$", href, re.IGNORECASE):
            try:
                head = requests.head(href, headers=headers, timeout=5, allow_redirects=True)
                size = int(head.headers.get("Content-Length", 0))
                if 0 < size <= MAX_ATTACHMENT_SIZE:
                    attachments.append((href, size))
                elif size > MAX_ATTACHMENT_SIZE:
                    print(f"Пропущено вложение крупнее 25МБ: {href} ({size} байт)")
            except Exception as e:
                print(f"Ошибка проверки размера файла {href}: {e}")

    return str(content_div), attachments


def send_email(subject, html_body, attachments, article_url):
    msg = MIMEMultipart("related")
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    # Строго соблюдаем тему ХТ
    msg["Subject"] = f"ХТ: {subject}"

    # Оформляем тело с сохранением верстки статьи
    full_html = f"""
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }}
          img {{ max-width: 100%; height: auto; display: block; margin: 15px 0; }}
          a {{ color: #0066cc; }}
          .footer {{ margin-top: 30px; padding-top: 10px; border-top: 1px solid #ccc; font-size: 0.8em; color: #666; }}
        </style>
      </head>
      <body>
        <h2><a href="{article_url}">{subject}</a></h2>
        <hr>
        {html_body}
        <div class="footer">
          <p>Источник: <a href="{article_url}">{article_url}</a></p>
        </div>
      </body>
    </html>
    """

    msg.attach(MIMEText(full_html, "html", "utf-8"))

    # Скачивание и прикрепление валидных вложений
    for file_url, _ in attachments:
        try:
            filename = os.path.basename(urllib.parse.urlparse(file_url).path)
            file_resp = requests.get(file_url, timeout=30)
            if file_resp.status_code == 200:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(file_resp.content)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                msg.attach(part)
        except Exception as e:
            print(f"Не удалось прикрепить файл {file_url}: {e}")

    # Отправка через Gmail SMTP
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)


def main():
    if not all([GMAIL_USER, GMAIL_PASS, RECIPIENT_EMAIL]):
        raise ValueError("Не заданы секреты GMAIL_USER, GMAIL_APP_PASSWORD или RECIPIENT_EMAIL")

    sent_urls = load_sent_urls()
    feed = feedparser.parse(FEED_URL)

    new_sent_count = 0

    for entry in reversed(feed.entries):  # Обрабатываем от старых к новым
        url = entry.link
        title = entry.title

        # Фильтр по паттерну года/месяца
        if "/2026/" not in url and not re.search(r"hronikatm\.com/\d{4}/\d{2}/", url):
            continue

        if url in sent_urls:
            continue

        print(f"Обработка статьи: {title} ({url})")
        html_body, attachments = get_article_content(url)

        if html_body:
            send_email(title, html_body, attachments, url)
            sent_urls.add(url)
            new_sent_count += 1
            print(f"Успешно отправлено: {title}")

    save_sent_urls(sent_urls)
    print(f"Завершено. Отправлено новых статей: {new_sent_count}")


if __name__ == "__main__":
    main()
