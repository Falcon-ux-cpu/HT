import os
import re
import json
import smtplib
import tempfile
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
import requests
import feedparser
from bs4 import BeautifulSoup
from PIL import Image

# Переменные окружения из GitHub Secrets
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

FEED_URL = "https://www.hronikatm.com/feed/"
SENT_LOG_FILE = "sent_articles.json"
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 МБ в байтах
MAX_IMAGE_DIMENSION = 1200  # Максимальная ширина/высота для фото в px

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


def optimize_image(input_path, output_path):
    """Сжимает и оптимизирует изображение с помощью Pillow."""
    try:
        with Image.open(input_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            width, height = img.size
            if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
            
            img.save(output_path, "JPEG", optimize=True, quality=80)
            return True
    except Exception as e:
        print(f"Ошибка оптимизации картинки {input_path}: {e}")
        return False


def process_single_image(img_tag, idx, temp_dir):
    """Скачивает и оптимизирует одну картинку, возвращает tuple (cid, path) или None."""
    src = img_tag.get("src") or img_tag.get("data-src")
    if not src:
        return None

    if not src.startswith("http"):
        src = urllib.parse.urljoin("https://www.hronikatm.com", src)

    try:
        raw_path = os.path.join(temp_dir, f"raw_img_{idx}")
        opt_path = os.path.join(temp_dir, f"opt_img_{idx}.jpg")

        res = requests.get(src, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            with open(raw_path, "wb") as f:
                f.write(res.content)

            if optimize_image(raw_path, opt_path):
                cid = f"image_{idx}"
                img_tag["src"] = f"cid:{cid}"
                for attr in ["srcset", "sizes", "style"]:
                    if attr in img_tag.attrs:
                        del img_tag[attr]

                if os.path.exists(raw_path):
                    os.remove(raw_path)
                return cid, opt_path

            if os.path.exists(raw_path):
                os.remove(raw_path)
    except Exception as e:
        print(f"[!] Ошибка обработки картинки {src}: {e}")

    return None


def download_file_attachments(soup_content, temp_dir):
    """Скачивает вложения (PDF, ZIP, DOCX и т.д.) из тела статьи."""
    downloaded_files = []
    
    for a in soup_content.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(pdf|doc|docx|zip|rar|7z|xlsx|xls)$", href, re.IGNORECASE):
            try:
                filename = os.path.basename(urllib.parse.urlparse(href).path) or "attachment"
                local_path = os.path.join(temp_dir, filename)

                res = requests.get(href, headers=HEADERS, stream=True, timeout=30)
                if res.status_code == 200:
                    file_size = 0
                    with open(local_path, "wb") as f:
                        for chunk in res.iter_content(chunk_size=8192):
                            file_size += len(chunk)
                            f.write(chunk)
                    
                    if 0 < file_size <= MAX_ATTACHMENT_SIZE:
                        downloaded_files.append(local_path)
                        print(f"[✓] Вложение сохранено локально: {filename}")
                    else:
                        if os.path.exists(local_path):
                            os.remove(local_path)
            except Exception as e:
                print(f"[!] Ошибка скачивания файла {href}: {e}")

    return downloaded_files


def format_pub_date_gmt5(soup, fallback_date):
    """Строго ищет <time class="entry-date updated td-module-date">, берет datetime и переводит в GMT+5."""
    time_tag = soup.find("time", class_=lambda c: c and "entry-date" in c)
    
    if time_tag and time_tag.get("datetime"):
        dt_str = time_tag["datetime"]
        try:
            dt = datetime.fromisoformat(dt_str)
            target_tz = timezone(timedelta(hours=5))
            dt_gmt5 = dt.astimezone(target_tz)
            return dt_gmt5.strftime("%d.%m.%Y, %H:%M (GMT+5)")
        except Exception as e:
            print(f"[!] Не удалось распарсить ISO дату {dt_str}: {e}")

    if time_tag:
        text_val = time_tag.get_text().strip()
        if text_val:
            return f"{text_val} (GMT+5)"

    return f"{fallback_date} (GMT+5)" if fallback_date else ""


def parse_article_exact_fields(soup, temp_dir):
    """Точный извлекатель по указанной структуре элемента."""
    inline_images = []
    image_idx = 0

    # 1. Заголовок из <h1 class="tdb-title-text">
    h1_tag = soup.find("h1", class_="tdb-title-text") or soup.find("h1")
    title = h1_tag.get_text().strip() if h1_tag else ""

    # 2. Главное фото из <img class="entry-thumb td-modal-image">
    main_img_html = ""
    featured_img = soup.find("img", class_="entry-thumb td-modal-image")
    if featured_img:
        res = process_single_image(featured_img, image_idx, temp_dir)
        if res:
            cid, path = res
            inline_images.append((cid, path))
            main_img_html = f'<div style="margin: 15px 0;"><img src="cid:{cid}" style="max-width: 100%; height: auto; display: block; margin: 0 auto;"></div>'
            image_idx += 1

    # 3. Контент статьи: ищем родительский контейнер параграфов wp-block-paragraph
    content_div = None
    paragraphs = soup.find_all("p", class_="wp-block-paragraph")
    
    if paragraphs:
        content_div = paragraphs[0].find_parent("div", class_=lambda c: c and "tdb-block-inner" in c)
        if not content_div:
            content_div = paragraphs[0].parent

    if not content_div:
        content_div = soup.find("div", class_="entry-content") or soup.find("article")
    
    if not content_div:
        return title, main_img_html, "", [], []

    content_copy = BeautifulSoup(str(content_div), "html.parser")

    for tag in content_copy.find_all(["script", "style", "iframe", "ins", "form", "button"]):
        tag.decompose()

    file_attachments = download_file_attachments(content_copy, temp_dir)

    for img in content_copy.find_all("img"):
        res = process_single_image(img, image_idx, temp_dir)
        if res:
            cid, path = res
            inline_images.append((cid, path))
            image_idx += 1

    article_body_html = str(content_copy)

    return title, main_img_html, article_body_html, inline_images, file_attachments


def get_article_data(entry, temp_dir):
    url = entry.link
    fallback_title = entry.title
    fallback_date = entry.get("published", "") or entry.get("updated", "")

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Точный поиск даты публикации из структуры
    pub_date_str = format_pub_date_gmt5(soup, fallback_date)

    # Точный парсинг статьи
    title, main_img_html, body_html, inline_images, file_attachments = parse_article_exact_fields(soup, temp_dir)

    final_title = title if title else fallback_title

    return final_title, pub_date_str, main_img_html, body_html, inline_images, file_attachments


def send_email(title, pub_date_str, main_img_html, body_html, inline_images, file_attachments):
    msg_root = MIMEMultipart("related")
    msg_root["From"] = GMAIL_USER
    msg_root["To"] = RECIPIENT_EMAIL
    msg_root["Subject"] = "ХТ"

    msg_alternative = MIMEMultipart("alternative")
    msg_root.attach(msg_alternative)

    # Верстка: Заголовок -> Дата (GMT+5) -> Титульное фото -> Текст статьи
    full_html = f"""
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #111; max-width: 800px; margin: 0 auto; padding: 15px; }}
          h1 {{ font-size: 22px; font-weight: bold; margin-bottom: 5px; color: #000; }}
          .date {{ font-size: 13px; color: #666; margin-bottom: 15px; }}
          img {{ max-width: 100%; height: auto; }}
          a {{ color: #0056b3; text-decoration: underline; }}
        </style>
      </head>
      <body>
        <h1>{title}</h1>
        {f'<div class="date">{pub_date_str}</div>' if pub_date_str else ''}
        {main_img_html}
        <hr style="border: 0; border-top: 1px solid #ccc; margin: 20px 0;">
        {body_html}
      </body>
    </html>
    """

    msg_alternative.attach(MIMEText(full_html, "html", "utf-8"))

    for cid, img_path in inline_images:
        try:
            with open(img_path, "rb") as f:
                img_part = MIMEImage(f.read(), _subtype="jpeg")
                img_part.add_header("Content-ID", f"<{cid}>")
                img_part.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
                msg_root.attach(img_part)
        except Exception as e:
            print(f"[!] Ошибка прикрепления CID-картинки {cid}: {e}")

    for filepath in file_attachments:
        try:
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=filename)
                msg_root.attach(part)
        except Exception as e:
            print(f"[!] Ошибка упаковки вложения {filepath}: {e}")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg_root)


def main():
    if not all([GMAIL_USER, GMAIL_PASS, RECIPIENT_EMAIL]):
        raise ValueError("Проверьте секреты: GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL")

    sent_urls = load_sent_urls()
    feed = feedparser.parse(FEED_URL)
    new_sent_count = 0

    with tempfile.TemporaryDirectory() as temp_dir:
        for entry in reversed(feed.entries):
            url = entry.link

            if not re.search(r"hronikatm\.com/\d{4}/\d{2}/", url):
                continue

            if url in sent_urls:
                continue

            print(f"\n[+] Обработка статьи: {entry.title} ({url})")

            try:
                title, pub_date, main_img, body, inline_images, attachments = get_article_data(entry, temp_dir)

                if body or main_img:
                    send_email(title, pub_date, main_img, body, inline_images, attachments)
                    sent_urls.add(url)
                    new_sent_count += 1
                    print(f"[✓] Успешно отправлено письмо с темой 'ХТ': {title}")
            except Exception as e:
                print(f"[!] Ошибка при обработке статьи {url}: {e}")

    save_sent_urls(sent_urls)
    print(f"\nЗавершено. Отправлено новых статей: {new_sent_count}")


if __name__ == "__main__":
    main()
