# -*- coding: utf-8 -*-
import logging
import os
import sys
import asyncio
import ffmpeg
import zipfile
import tarfile
import shutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from PIL import Image
import pytesseract
from typing import Final

# ពិនិត្យ Library
try:
    from PyPDF2 import PdfReader, PdfWriter, PdfMerger
    from pdf2image import convert_from_path
except ImportError:
    # ក្នុង Render buildCommand នឹងដំឡើង Library ទាំងអស់
    # នេះគ្រាន់តែជាការពិនិត្យក្នុងតំបន់ប៉ុណ្ណោះ
    print("!!! កំហុស៖ សូមប្រាកដថាបានតម្លើង Library ទាំងអស់៖ pip install PyPDF2 pdf2image Pillow python-telegram-bot ffmpeg-python")
    sys.exit(1)

# --- ការកំណត់តម្លៃសំខាន់ៗសម្រាប់ Render Deployment ---
# BOT_TOKEN ត្រូវបានយកពី Environment Variable (ដូចដែលបានកំណត់ក្នុង render.yaml)
BOT_TOKEN: Final = os.environ.get("BOT_TOKEN", "") 
MAX_FILE_SIZE: Final = 50 * 1024 * 1024 # កំណត់ទំហំ File អតិបរមា 50 MB

# ទទួលបាន URL និង PORT ពី Render Environment
# RENDER_EXTERNAL_URL គឺជា URL HTTPS ពេញលេញរបស់ Render Service
WEBHOOK_URL: Final = os.environ.get("RENDER_EXTERNAL_URL", "") 
PORT: Final = int(os.environ.get("PORT", "8000")) 

# កំណត់ 'ស្ថានភាព' (States)
(SELECT_ACTION,
 WAITING_PDF_TO_IMG_FORMAT, WAITING_PDF_TO_IMG_FILE,
 WAITING_FOR_MERGE, WAITING_FOR_SPLIT_FILE, WAITING_FOR_SPLIT_RANGE,
 WAITING_FOR_COMPRESS,
 WAITING_FOR_IMG_TO_PDF,
 WAITING_FOR_IMG_TO_TEXT_FILE,
 SELECT_AUDIO_OUTPUT_FORMAT, WAITING_FOR_AUDIO_FILE,
 SELECT_VIDEO_OUTPUT_FORMAT, WAITING_FOR_VIDEO_FILE,
 SELECT_ARCHIVE_ACTION, WAITING_FOR_FILES_TO_ZIP, WAITING_FOR_ARCHIVE_TO_EXTRACT
) = range(16)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# មុខងារនេះត្រូវបានរក្សាទុក ប៉ុន្តែវាគួរតែត្រឡប់ True ព្រោះ FFmpeg ត្រូវបានដំឡើងតាមរយៈ apt-get ក្នុង render.yaml
def is_ffmpeg_installed():
    return True 

# --- អនុគមន៍ដំណើរការនៅខាងក្រោយ (Background Tasks) ---
# (រក្សាទុកអនុគមន៍ដំណើរការនៅខាងក្រោយទាំងអស់របស់អ្នក ដោយសារពួកវាត្រឹមត្រូវ)

async def pdf_to_img_task(chat_id, file_path, msg, context, fmt):
    try:
        images = convert_from_path(file_path, dpi=200, fmt=fmt)
        await context.bot.edit_message_text(f"បំប្លែងបាន {len(images)} ទំព័រ។ កំពុងផ្ញើរូបភាព...", chat_id=chat_id, message_id=msg.message_id)
        for i, image in enumerate(images):
            out_path = f"page_{i+1}_{chat_id}.{fmt}"
            image.save(out_path, fmt.upper())
            await context.bot.send_photo(chat_id=chat_id, photo=open(out_path, 'rb'))
            os.remove(out_path)
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបំប្លែង PDF ទៅជារូបភាព។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def merge_pdf_task(chat_id, file_paths, msg, context):
    output_path = f"merged_{chat_id}.pdf"
    try:
        merger = PdfMerger()
        for path in file_paths:
            merger.append(path)
        merger.write(output_path)
        merger.close()
        await context.bot.edit_message_text("បញ្ចូលឯកសារបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Merged.pdf")
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបញ្ចូលឯកសារ។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        for path in file_paths:
            if os.path.exists(path): os.remove(path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def split_pdf_task(chat_id, file_path, page_range_str, msg, context):
    output_path = f"split_{chat_id}.pdf"
    try:
        writer = PdfWriter()
        reader = PdfReader(file_path)
        pages_to_extract = set()
        parts = page_range_str.split(',')
        for part in parts:
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                for i in range(start, end + 1): pages_to_extract.add(i-1)
            else:
                pages_to_extract.add(int(part)-1)
        for i in sorted(list(pages_to_extract)):
            if 0 <= i < len(reader.pages): writer.add_page(reader.pages[i])
        if not writer.pages: raise ValueError("ទំព័រមិនត្រឹមត្រូវ")
        
        writer.write(output_path)
        await context.bot.edit_message_text("បំបែកឯកសារបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Split.pdf")
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបំបែកឯកសារ។\nសូមប្រាកដថាទម្រង់លេខទំព័រត្រឹមត្រូវ (ឧ. 2-5 ឬ 1,3,8)។", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def compress_pdf_task(chat_id, file_path, msg, context):
    output_path = f"compressed_{chat_id}.pdf"
    try:
        reader = PdfReader(file_path)
        writer = PdfWriter()
        for page in reader.pages:
            page.compress_content_streams()
            writer.add_page(page)
        with open(output_path, "wb") as f: writer.write(f)
        await context.bot.edit_message_text("បន្ថយទំហំឯកសារបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Compressed.pdf")
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបន្ថយទំហំឯកសារ។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def img_to_pdf_task(chat_id, file_paths, msg, context):
    output_path = f"converted_from_img_{chat_id}.pdf"
    try:
        if not file_paths: raise ValueError("មិនមានរូបភាពដើម្បីបំប្លែងទេ")
        image_list = []
        for path in file_paths:
            image_list.append(Image.open(path).convert('RGB'))
        first_image = image_list[0]
        other_images = image_list[1:]
        first_image.save(output_path, "PDF", resolution=100.0, save_all=True, append_images=other_images)
        await context.bot.edit_message_text("បំប្លែងរូបភាពទៅជា PDF បានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Image_to_PDF.pdf")
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបំប្លែងរូបភាពទៅជា PDF ។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        for path in file_paths:
            if os.path.exists(path): os.remove(path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def img_to_text_task(chat_id, file_path, msg, context):
    try:
        image = Image.open(file_path)
        text = pytesseract.image_to_string(image, lang='khm+eng')
        await context.bot.edit_message_text("បំប្លែងរូបភាពទៅជាអក្សរបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        if not text.strip():
            await context.bot.send_message(chat_id=chat_id, text="មិនអាចរកឃើញអក្សរនៅក្នុងរូបភាពនេះទេ ឬរូបភាពគ្មានគុណភាពល្អ។")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"**លទ្ធផលដែលបានបំប្លែង៖**\n\n```\n{text}\n```", parse_mode='Markdown')
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបំប្លែងរូបភាពទៅជាអក្សរ។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def media_conversion_task(chat_id, file_path, output_format, msg, context, media_type='audio'):
    output_path = f"converted_{chat_id}.{output_format}"
    try:
        await context.bot.edit_message_text(f"កំពុងបំប្លែងទៅជា {output_format.upper()}... ការងារនេះអាចត្រូវការពេលវេលាយូរបន្តិចសម្រាប់ឯកសារធំៗ។", chat_id=chat_id, message_id=msg.message_id)
        ffmpeg.input(file_path).output(output_path).run(overwrite_output=True)
        await context.bot.edit_message_text("បំប្លែងបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        if media_type == 'audio':
            await context.bot.send_audio(chat_id=chat_id, audio=open(output_path, 'rb'))
        elif media_type == 'video':
            await context.bot.send_video(chat_id=chat_id, video=open(output_path, 'rb'))
    except ffmpeg.Error as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបំប្លែងឯកសារ។ FFmpeg error:\n`{e.stderr.decode()}`", chat_id=chat_id, message_id=msg.message_id, parse_mode='Markdown')
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាដែលមិនបានរំពឹងទុក។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def create_zip_task(chat_id, file_paths, msg, context):
    output_path = f"archive_{chat_id}.zip"
    try:
        await context.bot.edit_message_text("កំពុងបង្កើតឯកសារ ZIP...", chat_id=chat_id, message_id=msg.message_id)
        with zipfile.ZipFile(output_path, 'w') as zipf:
            for file_path in file_paths:
                zipf.write(file_path, os.path.basename(file_path))
        await context.bot.edit_message_text("បង្កើតឯកសារ ZIP បានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="archive.zip")
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការបង្កើតឯកសារ ZIP។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        for path in file_paths:
            if os.path.exists(path): os.remove(path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def extract_archive_task(chat_id, file_path, msg, context):
    extract_dir = f"extracted_{chat_id}"
    try:
        await context.bot.edit_message_text("កំពុងពន្លាឯកសារ...", chat_id=chat_id, message_id=msg.message_id)
        os.makedirs(extract_dir, exist_ok=True)
        if file_path.endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        elif file_path.endswith('.tar.gz') or file_path.endswith('.tgz'):
            with tarfile.open(file_path, 'r:gz') as tar_ref:
                tar_ref.extractall(extract_dir)
        elif file_path.endswith('.tar'):
            with tarfile.open(file_path, 'r:') as tar_ref:
                tar_ref.extractall(extract_dir)
        else:
            raise ValueError("មិនគាំទ្រទ្រង់ទ្រាយឯកសារនេះទេ។ សូមផ្ញើតែ ZIP ឬ TAR/TAR.GZ")
        extracted_files = os.listdir(extract_dir)
        if not extracted_files: raise ValueError("ឯកសារ Archive គឺទទេ។")
        await context.bot.edit_message_text(f"ពន្លាបាន {len(extracted_files)} ឯកសារ។ កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        for filename in extracted_files:
            full_path = os.path.join(extract_dir, filename)
            if os.path.isfile(full_path):
                 await context.bot.send_document(chat_id=chat_id, document=open(full_path, 'rb'))
        await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការពន្លាឯកសារ។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.isdir(extract_dir): shutil.rmtree(extract_dir)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

# --- អនុគមន៍សម្រាប់គ្រប់គ្រងលំហូរការងារ (រក្សាទុកទាំងអស់ដូចដើម) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("📄 PDF ទៅជា រូបភាព", callback_data='pdf_to_img')],
        [InlineKeyboardButton("🖇️ បញ្ចូល PDF ច្រើនចូលគ្នា", callback_data='merge_pdf')],
        [InlineKeyboardButton("✂️ បំបែក PDF ជាទំព័រៗ", callback_data='split_pdf')],
        [InlineKeyboardButton("📦 បន្ថយទំហំ PDF", callback_data='compress_pdf')],
        [InlineKeyboardButton("🖼️ រូបភាព ទៅជា PDF", callback_data='img_to_pdf')],
        [InlineKeyboardButton("📖 រូបភាព ទៅជា អក្សរ", callback_data='img_to_text')],
        [InlineKeyboardButton("🎵 បំប្លែងឯកសារសម្លេង", callback_data='audio_converter')],
        [InlineKeyboardButton("🎬 បំប្លែងឯកសារវីដេអូ", callback_data='video_converter')],
        [InlineKeyboardButton("🗜️ គ្រប់គ្រងឯកសារ Archive", callback_data='archive_manager')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = '👋 សួស្តី! សូមជ្រើសរើសមុខងារខាងក្រោម៖'
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)
    return SELECT_ACTION

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """
សួស្តី! ខ្ញុំជា Bot សម្រាប់គ្រប់គ្រងឯកសារ។ នេះជាមុខងារដែលខ្ញុំអាចធ្វើបាន៖

📄 **មុខងារ PDF:**
- `/start` រួចចុច "PDF ទៅជា រូបភាព"
- `/merge_pdf` បញ្ចូលឯកសារ PDF

🖼️ **មុខងាររូបភាព:**
- `/img_to_pdf` បំប្លែងរូបភាពទៅជា PDF
- `/img_to_text` ដកស្រង់អក្សរពីរូបភាព

🎵 **មុខងារសម្លេង:**
- `/audio_converter` បំប្លែង Format សម្លេង

🎬 **មុខងារវីដេអូ:**
- `/video_converter` បំប្លែង Format វីដេអូ

🗜️ **មុខងារ Archive:**
- `/archive_manager` គ្រប់គ្រង ZIP/TAR

**បញ្ជាផ្សេងទៀត៖**
- `/cancel` - បោះបង់ប្រតិបត្តិការ
- `/help` - បង្ហាញសារនេះម្ដងទៀត
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def start_pdf_to_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    keyboard = [
        [InlineKeyboardButton("➡️ បំប្លែងទៅជា JPG", callback_data='fmt_jpeg')],
        [InlineKeyboardButton("➡️ បំប្លែងទៅជា PNG", callback_data='fmt_png')],
        [InlineKeyboardButton("⬅️ ត្រឡប់ក្រោយ", callback_data='main_menu')]
    ]
    await query.edit_message_text(text="សូមជ្រើសរើសប្រភេទរូបភាព៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

async def start_conversion_with_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['format'] = "jpeg" if query.data == 'fmt_jpeg' else "png"
    await query.answer()
    await query.edit_message_text(f"✅ បានជ្រើសរើស {context.user_data['format'].upper()}។\n\nឥឡូវ សូមផ្ញើឯកសារ PDF មួយមកឱ្យខ្ញុំ។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_PDF_TO_IMG_FILE

async def receive_pdf_for_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារមានទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_PDF_TO_IMG_FILE
    file = await doc.get_file()
    file_path = f"temp_{file.file_id}.pdf"
    await file.download_to_drive(file_path)
    fmt = context.user_data.get('format', 'jpeg')
    msg = await update.message.reply_text("✅ ទទួលបានឯកសារ! កំពុងបំប្លែង...")
    asyncio.create_task(pdf_to_img_task(update.effective_chat.id, file_path, msg, context, fmt))
    return ConversationHandler.END

async def start_merge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    context.user_data['merge_files'] = []
    await query.edit_message_text(f"✅ សូមផ្ញើឯកសារ PDF ម្ដងមួយៗ។ (ទំហំឯកសារនីមួយៗមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)\nនៅពេលរួចរាល់ សូមវាយ /done ។")
    return WAITING_FOR_MERGE

async def receive_pdf_for_merge(update, context):
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារនេះទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_FOR_MERGE
    file = await doc.get_file()
    file_path = f"temp_{file.file_id}.pdf"
    await file.download_to_drive(file_path)
    if 'merge_files' not in context.user_data: context.user_data['merge_files'] = []
    context.user_data['merge_files'].append(file_path)
    count = len(context.user_data['merge_files'])
    await update.message.reply_text(f"បានទទួលឯកសារទី {count}។\nផ្ញើបន្ថែម ឬវាយ /done ។")
    return WAITING_FOR_MERGE

async def done_merging(update, context):
    if 'merge_files' not in context.user_data or len(context.user_data['merge_files']) < 2:
        await update.message.reply_text("សូមផ្ញើឯកសារ PDF យ៉ាងហោចណាស់ ២។")
        return WAITING_FOR_MERGE
    msg = await update.message.reply_text("យល់ព្រម! កំពុងបញ្ចូលឯកសារ...")
    asyncio.create_task(merge_pdf_task(update.effective_chat.id, context.user_data['merge_files'], msg, context))
    context.user_data.clear()
    return ConversationHandler.END

async def start_split(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text(f"✅ សូមផ្ញើឯកសារ PDF មួយដែលអ្នកចង់បំបែក។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_FOR_SPLIT_FILE

async def receive_pdf_for_split(update, context):
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារមានទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_FOR_SPLIT_FILE
    file = await doc.get_file()
    file_path = f"temp_{file.file_id}.pdf"
    await file.download_to_drive(file_path)
    context.user_data['split_file_path'] = file_path
    await update.message.reply_text("✅ ទទួលបានឯកសារ។\n\nឥឡូវ សូមវាយបញ្ចូលលេខទំព័រ (ឧ. '2-5' ឬ '1,3,8')។")
    return WAITING_FOR_SPLIT_RANGE

async def receive_split_range(update, context):
    page_range = update.message.text
    file_path = context.user_data.get('split_file_path')
    msg = await update.message.reply_text("យល់ព្រម! កំពុងបំបែកឯកសារ...")
    asyncio.create_task(split_pdf_task(update.effective_chat.id, file_path, page_range, msg, context))
    context.user_data.clear()
    return ConversationHandler.END

async def start_compress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text(f"✅ សូមផ្ញើឯកសារ PDF មួយដែលអ្នកចង់បន្ថយទំហំ។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_FOR_COMPRESS

async def receive_pdf_for_compress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារមានទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_FOR_COMPRESS
    file = await doc.get_file()
    file_path = f"temp_{file.file_id}.pdf"
    await file.download_to_drive(file_path)
    msg = await update.message.reply_text("✅ ទទួលបានឯកសារ! កំពុងបន្ថយទំហំ...")
    asyncio.create_task(compress_pdf_task(update.effective_chat.id, file_path, msg, context))
    return ConversationHandler.END

async def start_img_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    context.user_data['img_to_pdf_files'] = []
    await query.edit_message_text("✅ សូមផ្ញើរូបភាពម្ដងមួយៗ។\nនៅពេលរួចរាល់ សូមវាយ /done ។")
    return WAITING_FOR_IMG_TO_PDF

async def receive_img_for_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    file_obj = update.message.photo[-1] if update.message.photo else update.message.document
    if not file_obj:
         await update.message.reply_text("សូមផ្ញើរូបភាពជា File ឬ Photo។")
         return WAITING_FOR_IMG_TO_PDF
         
    file = await file_obj.get_file()
    file_path = f"temp_{file.file_id}.jpg"
    await file.download_to_drive(file_path)
    if 'img_to_pdf_files' not in context.user_data: context.user_data['img_to_pdf_files'] = []
    context.user_data['img_to_pdf_files'].append(file_path)
    count = len(context.user_data['img_to_pdf_files'])
    await update.message.reply_text(f"បានទទួលរូបភាពទី {count}។\nផ្ញើបន្ថែម ឬវាយ /done ។")
    return WAITING_FOR_IMG_TO_PDF

async def done_img_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'img_to_pdf_files' not in context.user_data or len(context.user_data['img_to_pdf_files']) < 1:
        await update.message.reply_text("សូមផ្ញើរូបភាពយ៉ាងហោចណាស់មួយ។")
        return WAITING_FOR_IMG_TO_PDF
    msg = await update.message.reply_text("យល់ព្រម! កំពុងបំប្លែងរូបភាពទៅជា PDF...")
    asyncio.create_task(img_to_pdf_task(update.effective_chat.id, context.user_data['img_to_pdf_files'], msg, context))
    context.user_data.clear()
    return ConversationHandler.END

async def start_img_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text("✅ សូមផ្ញើរូបភាពមួយមកឱ្យខ្ញុំ ដើម្បីបំប្លែងទៅជាអក្សរ។\nដើម្បីបោះបង់ សូមវាយ /cancel")
    return WAITING_FOR_IMG_TO_TEXT_FILE

async def receive_img_for_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    file_obj = update.message.photo[-1] if update.message.photo else update.message.document
    if not file_obj:
        await update.message.reply_text("សូមផ្ញើរូបភាពជា File ឬ Photo។")
        return WAITING_FOR_IMG_TO_TEXT_FILE
    if file_obj.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ រូបភាពមានទំហំធំពេក (មិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)។")
        return WAITING_FOR_IMG_TO_TEXT_FILE
    file = await file_obj.get_file()
    file_path = f"temp_{file.file_id}.jpg"
    await file.download_to_drive(file_path)
    msg = await update.message.reply_text("✅ ទទួលបានរូបភាព! កំពុងបំប្លែងទៅជាអក្សរ...")
    asyncio.create_task(img_to_text_task(update.effective_chat.id, file_path, msg, context))
    return ConversationHandler.END

def create_format_buttons(formats, prefix, columns=3):
    """អនុគមន៍ជំនួយសម្រាប់បង្កើតប៊ូតុង Format ជាក្រឡាចត្រង្គ"""
    buttons = [InlineKeyboardButton(f"{fmt.upper()}", callback_data=f"{prefix}_{fmt.lower()}") for fmt in formats]
    keyboard = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    keyboard.append([InlineKeyboardButton("⬅️ ត្រឡប់ក្រោយ", callback_data='main_menu')])
    return keyboard

async def start_audio_converter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    if not is_ffmpeg_installed():
        await query.edit_message_text("❌ កំហុស៖ FFmpeg មិនត្រូវបានដំឡើងទេ។ មុខងារនេះមិនអាចប្រើបានទេ។")
        return SELECT_ACTION 
    audio_formats = ['AAC', 'AIFF', 'FLAC', 'M4A', 'M4R', 'MMF', 'MP3', 'OGG', 'OPUS', 'WAV', 'WMA']
    keyboard = create_format_buttons(audio_formats, "audio")
    await query.edit_message_text(text="សូមជ្រើសរើសទ្រង់ទ្រាយឯកសារសម្លេងដែលអ្នកចង់បាន៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

async def select_audio_output(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['output_format'] = query.data.split('_')[1]
    await query.edit_message_text(f"✅ បានជ្រើសរើស {context.user_data['output_format'].upper()}។\n\nឥឡូវ សូមផ្ញើឯកសារសម្លេងមកឱ្យខ្ញុំ។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_FOR_AUDIO_FILE

async def receive_audio_for_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    file_obj = update.message.audio or update.message.document
    if not file_obj:
        await update.message.reply_text("សូមផ្ញើឯកសារសម្លេង ឬឯកសារជា Document។")
        return WAITING_FOR_AUDIO_FILE
    if file_obj.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារមានទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_FOR_AUDIO_FILE
    file = await file_obj.get_file()
    file_path = f"temp_{file.file_id}"
    await file.download_to_drive(file_path)
    output_format = context.user_data.get('output_format', 'mp3')
    msg = await update.message.reply_text("✅ ទទួលបានឯកសារ! កំពុងបំប្លែង...")
    asyncio.create_task(media_conversion_task(update.effective_chat.id, file_path, output_format, msg, context, media_type='audio'))
    return ConversationHandler.END

async def start_video_converter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    if not is_ffmpeg_installed():
        await query.edit_message_text("❌ កំហុស៖ FFmpeg មិនត្រូវបានដំឡើងទេ។ មុខងារនេះមិនអាចប្រើបានទេ។")
        return SELECT_ACTION
    video_formats = ['3G2', '3GP', 'AVI', 'FLV', 'MKV', 'MOV', 'MP4', 'MPG', 'OGV', 'WEBM', 'WMV']
    keyboard = create_format_buttons(video_formats, "video")
    await query.edit_message_text(text="សូមជ្រើសរើសទ្រង់ទ្រាយវីដេអូដែលអ្នកចង់បាន៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

async def select_video_output(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['output_format'] = query.data.split('_')[1]
    await query.edit_message_text(f"✅ បានជ្រើសរើស {context.user_data['output_format'].upper()}។\n\nឥឡូវ សូមផ្ញើវីដេអូមកឱ្យខ្ញុំ។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_FOR_VIDEO_FILE

async def receive_video_for_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    file_obj = update.message.video or update.message.document
    if not file_obj:
        await update.message.reply_text("សូមផ្ញើឯកសារវីដេអូ ឬឯកសារជា Document។")
        return WAITING_FOR_VIDEO_FILE
    if file_obj.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារមានទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_FOR_VIDEO_FILE
    file = await file_obj.get_file()
    file_path = f"temp_{file.file_id}"
    await file.download_to_drive(file_path)
    output_format = context.user_data.get('output_format', 'mp4')
    msg = await update.message.reply_text(f"✅ ទទួលបានវីដេអូ! កំពុងបំប្លែង...")
    asyncio.create_task(media_conversion_task(update.effective_chat.id, file_path, output_format, msg, context, media_type='video'))
    return ConversationHandler.END

async def start_archive_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    keyboard = [
        [InlineKeyboardButton("➕ បង្កើតឯកសារ ZIP", callback_data='archive_create')],
        [InlineKeyboardButton("➖ ពន្លាឯកសារ Archive", callback_data='archive_extract')],
        [InlineKeyboardButton("⬅️ ត្រឡប់ក្រោយ", callback_data='main_menu')]
    ]
    await query.edit_message_text(text="សូមជ្រើសរើសសកម្មភាពសម្រាប់ Archive៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

async def start_create_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    context.user_data['zip_files'] = []
    await query.edit_message_text(f"✅ សូមផ្ញើឯកសារម្ដងមួយៗដើម្បីបញ្ចូលទៅក្នុង ZIP។ (ទំហំឯកសារនីមួយៗមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)\nពេលរួចរាល់ សូមវាយ /done ។")
    return WAITING_FOR_FILES_TO_ZIP

async def receive_file_for_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារនេះទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_FOR_FILES_TO_ZIP
    file = await doc.get_file()
    file_path = f"temp_{file.file_unique_id}_{doc.file_name}"
    await file.download_to_drive(file_path)
    if 'zip_files' not in context.user_data: context.user_data['zip_files'] = []
    context.user_data['zip_files'].append(file_path)
    count = len(context.user_data['zip_files'])
    await update.message.reply_text(f"បានទទួលឯកសារទី {count}។\nផ្ញើបន្ថែម ឬវាយ /done ។")
    return WAITING_FOR_FILES_TO_ZIP

async def done_zipping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'zip_files' not in context.user_data or not context.user_data['zip_files']:
        await update.message.reply_text("សូមផ្ញើឯកសារយ៉ាងហោចណាស់មួយ។")
        return WAITING_FOR_FILES_TO_ZIP
    msg = await update.message.reply_text("យល់ព្រម! កំពុងបង្កើតឯកសារ ZIP...")
    asyncio.create_task(create_zip_task(update.effective_chat.id, context.user_data['zip_files'], msg, context))
    context.user_data.clear()
    return ConversationHandler.END

async def start_extract_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text(f"✅ សូមផ្ញើឯកសារ Archive (ZIP ឬ TAR.GZ) ដែលអ្នកចង់ពន្លា។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_FOR_ARCHIVE_TO_EXTRACT

async def receive_archive_to_extract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារមានទំហំធំពេក។ សូមផ្ញើឯកសារដែលមានទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB។")
        return WAITING_FOR_ARCHIVE_TO_EXTRACT
    file = await doc.get_file()
    file_path = f"temp_{file.file_unique_id}_{doc.file_name}"
    await file.download_to_drive(file_path)
    msg = await update.message.reply_text("✅ ទទួលបានឯកសារ! កំពុងពន្លា...")
    asyncio.create_task(extract_archive_task(update.effective_chat.id, file_path, msg, context))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("ប្រតិបត្តិការត្រូវបានបោះបង់។")
    else:
        await update.message.reply_text("ប្រតិបត្តិការត្រូវបានបោះបង់។")
    return ConversationHandler.END

# --- អនុគមន៍ថ្មីសម្រាប់ទទួល Commands ដោយផ្ទាល់ ---

async def start_pdf_to_img_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម PDF to Image តាមរយៈ Command """
    await update.message.reply_text("សូមជ្រើសរើសប្រភេទរូបភាព៖", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ បំប្លែងទៅជា JPG", callback_data='fmt_jpeg')],
        [InlineKeyboardButton("➡️ បំប្លែងទៅជា PNG", callback_data='fmt_png')],
        [InlineKeyboardButton("⬅️ ត្រឡប់ក្រោយ", callback_data='main_menu')]
    ]))
    return SELECT_ACTION

async def start_merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Merge PDF តាមរយៈ Command """
    context.user_data['merge_files'] = []
    await update.message.reply_text(f"✅ សូមផ្ញើឯកសារ PDF ម្ដងមួយៗ។ (ទំហំឯកសារនីមួយៗមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)\nនៅពេលរួចរាល់ សូមវាយ /done ។")
    return WAITING_FOR_MERGE

async def start_split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Split PDF តាមរយៈ Command """
    await update.message.reply_text(f"✅ សូមផ្ញើឯកសារ PDF មួយដែលអ្នកចង់បំបែក។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_FOR_SPLIT_FILE

async def start_compress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Compress PDF តាមរយៈ Command """
    await update.message.reply_text(f"✅ សូមផ្ញើឯកសារ PDF មួយដែលអ្នកចង់បន្ថយទំហំ។ (ទំហំមិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)")
    return WAITING_FOR_COMPRESS

async def start_img_to_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Image to PDF តាមរយៈ Command """
    context.user_data['img_to_pdf_files'] = []
    await update.message.reply_text("✅ សូមផ្ញើរូបភាពម្ដងមួយៗ។\nនៅពេលរួចរាល់ សូមវាយ /done ។")
    return WAITING_FOR_IMG_TO_PDF

async def start_img_to_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Image to Text តាមរយៈ Command """
    await update.message.reply_text("✅ សូមផ្ញើរូបភាពមួយមកឱ្យខ្ញុំ ដើម្បីបំប្លែងទៅជាអក្សរ។\nដើម្បីបោះបង់ សូមវាយ /cancel")
    return WAITING_FOR_IMG_TO_TEXT_FILE

async def start_audio_converter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Audio Converter តាមរយៈ Command """
    if not is_ffmpeg_installed():
        await update.message.reply_text("❌ កំហុស៖ FFmpeg មិនត្រូវបានដំឡើងទេ។ មុខងារនេះមិនអាចប្រើបានទេ។")
        return ConversationHandler.END
    audio_formats = ['AAC', 'AIFF', 'FLAC', 'M4A', 'M4R', 'MMF', 'MP3', 'OGG', 'OPUS', 'WAV', 'WMA']
    keyboard = create_format_buttons(audio_formats, "audio")
    await update.message.reply_text(text="សូមជ្រើសរើសទ្រង់ទ្រាយឯកសារសម្លេងដែលអ្នកចង់បាន៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

async def start_video_converter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Video Converter តាមរយៈ Command """
    if not is_ffmpeg_installed():
        await update.message.reply_text("❌ កំហុស៖ FFmpeg មិនត្រូវបានដំឡើងទេ។ មុខងារនេះមិនអាចប្រើបានទេ។")
        return ConversationHandler.END
    video_formats = ['3G2', '3GP', 'AVI', 'FLV', 'MKV', 'MOV', 'MP4', 'MPG', 'OGV', 'WEBM', 'WMV']
    keyboard = create_format_buttons(video_formats, "video")
    await update.message.reply_text(text="សូមជ្រើសរើសទ្រង់ទ្រាយវីដេអូដែលអ្នកចង់បាន៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

async def start_archive_manager_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ ចាប់ផ្តើម Archive Manager តាមរយៈ Command """
    keyboard = [
        [InlineKeyboardButton("➕ បង្កើតឯកសារ ZIP", callback_data='archive_create')],
        [InlineKeyboardButton("➖ ពន្លាឯកសារ Archive", callback_data='archive_extract')],
        [InlineKeyboardButton("⬅️ ត្រឡប់ក្រោយ", callback_data='main_menu')]
    ]
    await update.message.reply_text(text="សូមជ្រើសរើសសកម្មភាពសម្រាប់ Archive៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

# --- Main Application Runner (កែប្រែសម្រាប់ Render Webhook) ---
def main() -> None:
    # ពិនិត្យ Environment Variables
    if not BOT_TOKEN:
        print("!!! កំហុស៖ BOT_TOKEN មិនត្រូវបានកំណត់។ សូមកំណត់វានៅក្នុង Environment Variable (render.yaml)។")
        sys.exit(1)
        
    if not WEBHOOK_URL:
        print("!!! កំហុស៖ RENDER_EXTERNAL_URL មិនត្រូវបានកំណត់។ ត្រូវប្រាកដថាប្រើ Render Web Service Environment។")
        # មិនអាចដំណើរការ Webhook ដោយគ្មាន URL ពេញលេញបានទេ។
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).read_timeout(30).build()
    
    # --- Conversation Handler (រក្សាទុកដូចដើម) ---
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("pdf_to_img", start_pdf_to_img_command),
            CommandHandler("merge_pdf", start_merge_command),
            CommandHandler("split_pdf", start_split_command),
            CommandHandler("compress_pdf", start_compress_command),
            CommandHandler("img_to_pdf", start_img_to_pdf_command),
            CommandHandler("img_to_text", start_img_to_text_command),
            CommandHandler("audio_converter", start_audio_converter_command),
            CommandHandler("video_converter", start_video_converter_command),
            CommandHandler("archive_manager", start_archive_manager_command),
        ],
        states={
            SELECT_ACTION: [
                CallbackQueryHandler(start_pdf_to_img, pattern='^pdf_to_img$'),
                CallbackQueryHandler(start_conversion_with_format, pattern='^fmt_'),
                CallbackQueryHandler(start_merge, pattern='^merge_pdf$'),
                CallbackQueryHandler(start_split, pattern='^split_pdf$'),
                CallbackQueryHandler(start_compress, pattern='^compress_pdf$'),
                CallbackQueryHandler(start_img_to_pdf, pattern='^img_to_pdf$'),
                CallbackQueryHandler(start_img_to_text, pattern='^img_to_text$'),
                CallbackQueryHandler(start_audio_converter, pattern='^audio_converter$'),
                CallbackQueryHandler(select_audio_output, pattern='^audio_'),
                CallbackQueryHandler(start_video_converter, pattern='^video_converter$'),
                CallbackQueryHandler(select_video_output, pattern='^video_'),
                CallbackQueryHandler(start_archive_manager, pattern='^archive_manager$'),
                CallbackQueryHandler(start_create_zip, pattern='^archive_create$'),
                CallbackQueryHandler(start_extract_archive, pattern='^archive_extract$'),
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            WAITING_PDF_TO_IMG_FILE: [MessageHandler(filters.Document.PDF, receive_pdf_for_img)],
            WAITING_FOR_MERGE: [MessageHandler(filters.Document.PDF, receive_pdf_for_merge), CommandHandler('done', done_merging)],
            WAITING_FOR_SPLIT_FILE: [MessageHandler(filters.Document.PDF, receive_pdf_for_split)],
            WAITING_FOR_SPLIT_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_split_range)],
            WAITING_FOR_COMPRESS: [MessageHandler(filters.Document.PDF, receive_pdf_for_compress)],
            WAITING_FOR_IMG_TO_PDF: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_img_for_pdf), CommandHandler('done', done_img_to_pdf)],
            WAITING_FOR_IMG_TO_TEXT_FILE: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_img_for_text)],
            WAITING_FOR_AUDIO_FILE: [MessageHandler(filters.AUDIO | filters.Document.ALL, receive_audio_for_conversion)],
            WAITING_FOR_VIDEO_FILE: [MessageHandler(filters.VIDEO | filters.Document.ALL, receive_video_for_conversion)],
            WAITING_FOR_FILES_TO_ZIP: [MessageHandler(filters.Document.ALL, receive_file_for_zip), CommandHandler('done', done_zipping)],
            WAITING_FOR_ARCHIVE_TO_EXTRACT: [MessageHandler(filters.Document.ALL, receive_archive_to_extract)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    
    # --- ការដំណើរការ Webhook សម្រាប់ Render ---
    FULL_WEBHOOK_URL = WEBHOOK_URL + '/' + BOT_TOKEN
    
    print(f">>> Bot កំពុងដំណើរការដោយ Webhook នៅលើ Host: 0.0.0.0, Port: {PORT}, URL_PATH: /{BOT_TOKEN}")
    print(f"!!! ត្រូវប្រាកដថាបានកំណត់ Webhook ទៅកាន់ Telegram: {FULL_WEBHOOK_URL}")
    
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=FULL_WEBHOOK_URL
    )

if __name__ == "__main__":
    main()