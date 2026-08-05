import os
import re
import json
import smtplib
import shutil
import tempfile
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import requests
import feedparser
from bs4 import BeautifulSoup

# Переменные окружения из GitHub Secrets
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

FEED_URL = "https://www.hronikatm.com/feed/"
SENT_LOG_FILE = "sent_articles.json"
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 МБ в байтах
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


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


def clean_html_content(soup_content):
    # 1. Удаляем скрипты, стили, фреймы и лишние виджеты
    for tag in soup_content.find_all(["script", "style", "iframe", "ins", "form"]):
        tag.decompose()

    # 2. Удаляем блоки типа "Последние события", "Читайте также", похожие статьи
    # Ищем по популярным классам/ID на WordPress и заголовкам блоков
    selectors_to_remove = [
        ".recent-posts", ".related-posts", ".popular-posts", 
        ".yarpp-related", ".widget", ".post-publisher", ".entry-meta",
        "#recent-posts", "#related-posts"
    ]
    for selector in selectors_to_remove:
        for element in soup_content.select(selector):
            element.decompose()

    # Поиск и удаление блоков по тексту (например, "Последние события", "Читайте также")
    for header in soup_content.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div"]):
        header_text = header.get_text().strip().lower()
        if any(phrase in header_text for phrase in ["последние события", "читайте также", "похожие новости", "рекомендуем"]):
            # Удаляем сам заголовок и следующий за ним список/блок, если есть
            next_sibling = header.find_next_sibling()
            if next_sibling and next_sibling.name in ["ul", "ol", "div"]:
                next_sibling.decompose()
            header.decompose()

    # Примечание: теги  с текстом (ссылки вшитые в слова) не трогаем — они остаются.

    return str(soup_content)


def download_attachments(soup_content, temp_dir):
    downloaded_files = []
    
    # Находим все ссылки на файлы документов/архивов
    for a in soup_content.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(pdf|doc|docx|zip|rar|7z|xlsx|xls)$", href, re.IGNORECASE):
            try:
                filename = os.path.basename(urllib.parse.urlparse(href).path)
                if not filename:
                    filename = "attachment_file"
                
                local_path = os.path.join(temp_dir, filename)

                print(f"Скачиваем вложение: {href}")
                res = requests.get(href, headers=HEADERS, stream=True, timeout=30)
                if res.status_code == 200:
                    file_size = 0
                    with open(local_path, "wb") as f:
                        for chunk in res.iter_content(chunk_size=8192):
                            file_size += len(chunk)
                            f.write(chunk)
                    
                    # Проверка размера скачанного файла
                    if file_size <= MAX_ATTACHMENT_SIZE and file_size > 0:
                        downloaded_files.append(local_path)
                        print(f"Вложение сохранено локально ({file_size} байт): {filename}")
                    else:
                        print(f"Пропущено вложение крупнее 25МБ ({file_size} байт): {filename}")
                        if os.path.exists(local_path):
                            os.remove(local_path)
            except Exception as e:
                print(f"Ошибка при скачивании вложения {href}: {e}")

    return downloaded_files


def get_article_data(url, temp_dir):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Основной контейнер статьи
    content_div = soup.find("div", class_="entry-content") or soup.find("article")
    if not content_div:
        return None, []

    # Скачиваем вложения себе во временную папку
    local_attachments = download_attachments(content_div, temp_dir)

    # Очищаем HTML от лишних блоков (но сохраняем контекстные ссылки в словах)
    cleaned_html = clean_html_content(content_div)

    return cleaned_html, local_attachments


def send_email(html_body, local_attachments, article_url, article_title):
    msg = MIMEMultipart("mixed")
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    # Строгая тема письма без дополнительных текстов
    msg["Subject"] = "ХТ"

    full_html = f"""
    
      
        
        
      
      
        {article_title}
        
        {html_body}
        
          Оригинал статьи: {article_url}
        
      
    
    """

    msg.attach(MIMEText(full_html, "html", "utf-8"))

    # Прикрепляем скачанные локальные файлы
    for filepath in local_attachments:
        try:
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                msg.attach(part)
        except Exception as e:
            print(f"Ошибка при упаковке файла {filepath} в письмо: {e}")

    # Отправка по SMTP
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)


def main():
    if not all([GMAIL_USER, GMAIL_PASS, RECIPIENT_EMAIL]):
        raise ValueError("Проверьте секреты: GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL")

    sent_urls = load_sent_urls()
    feed = feedparser.parse(FEED_URL)
    new_sent_count = 0

    # Временная директория для загрузки вложений в рамках работы скрипта
    with tempfile.TemporaryDirectory() as temp_dir:
        for entry in reversed(feed.entries):
            url = entry.link
            title = entry.title

            # Проверка урла на паттерны сайта
            if not re.search(r"hronikatm\.com/\d{4}/\d{2}/", url):
                continue

            if url in sent_urls:
                continue

            print(f"\n[+] Найдена новая статья: {title} ({url})")
            
            try:
                html_body, downloaded_attachments = get_article_data(url, temp_dir)

                if html_body:
                    send_email(html_body, downloaded_attachments, url, title)
                    sent_urls.add(url)
                    new_sent_count += 1
                    print(f"[✓] Успешно отправлено письмо с темой 'ХТ': {title}")
            except Exception as e:
                print(f"[!] Ошибка при обработке {url}: {e}")

    save_sent_urls(sent_urls)
    print(f"\nГотово. Отправлено новых публикаций: {new_sent_count}")


if __name__ == "__main__":
    main()
