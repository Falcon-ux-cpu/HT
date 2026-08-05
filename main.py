import os
import re
import json
import smtplib
import tempfile
import urllib.parse
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
            # Конвертируем RGBA/P в RGB если нужно
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            # Изменяем размер, если картинка больше MAX_IMAGE_DIMENSION
            width, height = img.size
            if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
            
            # Сохраняем с понижением качества
            img.save(output_path, "JPEG", optimize=True, quality=80)
            return True
    except Exception as e:
        print(f"Ошибка оптимизации картинки {input_path}: {e}")
        return False


def process_and_download_images(soup_content, temp_dir):
    """
    Скачивает все картинки статьи на диск GitHub Runner'а,
    оптимизирует их и подменяет src на cid:image_X
    """
    inline_images = []  # Список кортежей: (cid, filepath)
    
    for idx, img in enumerate(soup_content.find_all("img")):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        
        # Разрешаем относительные ссылки
        if not src.startswith("http"):
            src = urllib.parse.urljoin("https://www.hronikatm.com", src)

        try:
            # Скачиваем фото во временный файл
            raw_path = os.path.join(temp_dir, f"raw_img_{idx}")
            opt_path = os.path.join(temp_dir, f"opt_img_{idx}.jpg")

            res = requests.get(src, headers=HEADERS, timeout=15)
            if res.status_code == 200:
                with open(raw_path, "wb") as f:
                    f.write(res.content)

                # Оптимизируем
                if optimize_image(raw_path, opt_path):
                    cid = f"image_{idx}"
                    # Подменяем атрибут src на CID
                    img["src"] = f"cid:{cid}"
                    # Удаляем лишние атрибуты srcset и sizes, чтобы письмо не путало источника
                    if "srcset" in img.attrs:
                        del img["srcset"]
                    if "sizes" in img.attrs:
                        del img["sizes"]

                    inline_images.append((cid, opt_path))
                    print(f"[✓] Картинка {src} успешно скачана, оптимизирована и привязана как {cid}")
                
                if os.path.exists(raw_path):
                    os.remove(raw_path)
        except Exception as e:
            print(f"[!] Ошибка скачивания фото {src}: {e}")

    return inline_images


def download_file_attachments(soup_content, temp_dir):
    """Скачивает не-карточные вложения (PDF, ZIP, DOCX и т.д.) на runner."""
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
                        print(f"[✓] Вложение сохранено локально: {filename} ({file_size} байт)")
                    else:
                        print(f"[!] Вложение {filename} пропущено (размер > 25MB: {file_size} байт)")
                        if os.path.exists(local_path):
                            os.remove(local_path)
            except Exception as e:
                print(f"[!] Ошибка скачивания файла {href}: {e}")

    return downloaded_files


def clean_article_body(soup_content):
    """Оставляет строго текст статьи и картинки, вычищая весь остальной UI."""
    # 1. Удаляем интерактивные и вспомогательные теги
    for tag in soup_content.find_all(["script", "style", "iframe", "ins", "form", "button"]):
        tag.decompose()

    # 2. Удаляем блоки похожих новостей, метаданных, соцсетей, тегов и виджетов
    unwanted_selectors = [
        ".recent-posts", ".related-posts", ".popular-posts", ".yarpp-related",
        ".widget", ".post-publisher", ".entry-meta", ".post-meta", ".share-buttons",
        ".tags-links", ".cat-links", ".comments-area", "#comments", "#respond",
        ".nav-links", ".post-navigation"
    ]
    for selector in unwanted_selectors:
        for element in soup_content.select(selector):
            element.decompose()

    # 3. Удаляем текстовые блоки вида "Последние события", "Читайте также" и т.п.
    for header in soup_content.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div"]):
        header_text = header.get_text().strip().lower()
        if any(phrase in header_text for phrase in ["последние события", "читайте также", "похожие новости", "рекомендуем"]):
            next_sibling = header.find_next_sibling()
            if next_sibling and next_sibling.name in ["ul", "ol", "div"]:
                next_sibling.decompose()
            header.decompose()

    return str(soup_content)


def get_article_data(entry, temp_dir):
    url = entry.link
    title = entry.title
    pub_date = entry.get("published", "") or entry.get("updated", "")

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Основной контейнер статьи
    content_div = soup.find("div", class_="entry-content") or soup.find("article")
    if not content_div:
        return None, [], [], title, pub_date

    # 1. Скачиваем обычные файлы-вложения (PDF, ZIP и др.)
    file_attachments = download_file_attachments(content_div, temp_dir)

    # 2. Скачиваем все фото на Runner, оптимизируем и подменяем на CID
    inline_images = process_and_download_images(content_div, temp_dir)

    # 3. Полностью чистим HTML статьи (сохраняя ссылки в словах)
    cleaned_html = clean_article_body(content_div)

    return cleaned_html, inline_images, file_attachments, title, pub_date


def send_email(html_body, inline_images, file_attachments, article_title, pub_date):
    # Используем multipart/related для правильного отображения картинок с CID
    msg_root = MIMEMultipart("related")
    msg_root["From"] = GMAIL_USER
    msg_root["To"] = RECIPIENT_EMAIL
    # Тема письма строго ХТ
    msg_root["Subject"] = "ХТ"

    msg_alternative = MIMEMultipart("alternative")
    msg_root.attach(msg_alternative)

    # Формируем чистый шаблон: Заголовок + Дата + Очищенное тело статьи
    full_html = f"""
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #111; max-width: 800px; margin: 0 auto; padding: 15px; }}
          h1 {{ font-size: 20px; font-weight: bold; margin-bottom: 5px; color: #000; }}
          .date {{ font-size: 13px; color: #666; margin-bottom: 20px; }}
          img {{ max-width: 100%; height: auto; display: block; margin: 15px 0; }}
          a {{ color: #0056b3; text-decoration: underline; }}
        </style>
      </head>
      <body>
        <h1>{article_title}</h1>
        {f'<div class="date">{pub_date}</div>' if pub_date else ''}
        <hr style="border: 0; border-top: 1px solid #ccc; margin-bottom: 20px;">
        {html_body}
      </body>
    </html>
    """

    msg_alternative.attach(MIMEText(full_html, "html", "utf-8"))

    # Встраиваем скачанные оптимизированные изображения прямо в тело письма
    for cid, img_path in inline_images:
        try:
            with open(img_path, "rb") as f:
                img_part = MIMEImage(f.read(), _subtype="jpeg")
                img_part.add_header("Content-ID", f"<{cid}>")
                img_part.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
                msg_root.attach(img_part)
        except Exception as e:
            print(f"[!] Ошибка прикрепления CID-картинки {cid}: {e}")

    # Прикрепляем дополнительные файлы (PDF/ZIP и т.д.)
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

    # Отправка по SMTP
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

            # Проверка URL статьи
            if not re.search(r"hronikatm\.com/\d{4}/\d{2}/", url):
                continue

            if url in sent_urls:
                continue

            print(f"\n[+] Обработка статьи: {entry.title} ({url})")

            try:
                html_body, inline_images, file_attachments, title, pub_date = get_article_data(entry, temp_dir)

                if html_body:
                    send_email(html_body, inline_images, file_attachments, title, pub_date)
                    sent_urls.add(url)
                    new_sent_count += 1
                    print(f"[✓] Успешно отправлено письмо с темой 'ХТ': {title}")
            except Exception as e:
                print(f"[!] Ошибка при обработке статьи {url}: {e}")

    save_sent_urls(sent_urls)
    print(f"\nЗавершено. Отправлено новых статей: {new_sent_count}")


if __name__ == "__main__":
    main()
