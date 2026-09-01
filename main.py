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
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
from PIL import Image
import pytesseract
from typing import Final

# ដោះស្រាយបញ្ហា Error Tesseract ដោយកំណត់ទីតាំងវាដោយផ្ទាល់
pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

# ពិនិត្យ និងនាំចូល Library ទាំងអស់
try:
    from PyPDF2 import PdfReader, PdfWriter, PdfMerger
    from pdf2image import convert_from_path
    import pyzipper
    import msoffcrypto
    import rarfile
except ImportError:
    print("!!! កំហុស៖ សូមប្រាកដថាបានតម្លើង Library នៅក្នុង requirements.txt ទាំងអស់")
    sys.exit(1)

BOT_TOKEN: Final = os.environ.get("BOT_TOKEN", "") 
MAX_FILE_SIZE: Final = 50 * 1024 * 1024 # 50 MB
WEBHOOK_URL: Final = os.environ.get("RENDER_EXTERNAL_URL", "") 
PORT: Final = int(os.environ.get("PORT", "8000")) 

# កំណត់ 'ស្ថានភាព' (States)
(SELECT_ACTION,
 WAITING_PDF_TO_IMG_FORMAT, WAITING_PDF_TO_IMG_FILE,
 WAITING_FOR_MERGE, WAITING_FOR_SPLIT_FILE, WAITING_FOR_SPLIT_RANGE,
 WAITING_FOR_COMPRESS, WAITING_FOR_IMG_TO_PDF, WAITING_FOR_IMG_TO_TEXT_FILE,
 SELECT_AUDIO_OUTPUT_FORMAT, WAITING_FOR_AUDIO_FILE,
 SELECT_VIDEO_OUTPUT_FORMAT, WAITING_FOR_VIDEO_FILE,
 SELECT_ARCHIVE_ACTION, WAITING_FOR_FILES_TO_ZIP, WAITING_FOR_ARCHIVE_TO_EXTRACT,
 WAITING_FOR_ENCRYPT_FILE, WAITING_FOR_PASSWORD,
 WAITING_FOR_DECRYPT_FILE, WAITING_FOR_DECRYPT_PASSWORD
) = range(20)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

def is_ffmpeg_installed(): return True 

# ==========================================
# មុខងារ 🔒 ចាក់សោរ និង 🔓 ដោះសោរឯកសារចម្រុះ
# ==========================================

async def encrypt_file_task(chat_id, file_path, password, filename, msg, context):
    ext = os.path.splitext(filename)[1].lower()
    output_path = f"encrypted_{chat_id}{ext}" if ext == '.pdf' else f"Secured_{chat_id}.zip"
    
    try:
        if ext == '.pdf':
            # ចាក់សោរ PDF ដោយផ្ទាល់
            reader = PdfReader(file_path)
            writer = PdfWriter()
            for page in reader.pages: writer.add_page(page)
            writer.encrypt(password)
            with open(output_path, "wb") as f: writer.write(f)
            out_name = f"Secured_{filename}"
        else:
            # វេចខ្ចប់ឯកសារផ្សេងៗ (Word, Excel, IMG...) ទៅជា ZIP ដាក់កូដ
            await context.bot.edit_message_text(f"កំពុងវេចខ្ចប់ឯកសារនេះជា ZIP ដែលមានសុវត្ថិភាព...", chat_id=chat_id, message_id=msg.message_id)
            with pyzipper.AESZipFile(output_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
                zf.setpassword(password.encode('utf-8'))
                zf.write(file_path, arcname=filename)
            out_name = f"Secured_Archive.zip"

        await context.bot.edit_message_text(f"🔒 ដាក់លេខកូដបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename=out_name)
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការដាក់លេខកូដ៖\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def decrypt_file_task(chat_id, file_path, password, filename, msg, context):
    ext = os.path.splitext(filename)[1].lower()
    output_path = f"decrypted_{chat_id}{ext}"
    extract_dir = f"extracted_decrypted_{chat_id}"
    
    try:
        if ext == '.pdf':
            reader = PdfReader(file_path)
            if not reader.is_encrypted: raise ValueError("ឯកសារនេះមិនមានជាប់លេខកូដទេ។")
            if reader.decrypt(password) == 0: raise ValueError("លេខកូដ (Password) មិនត្រឹមត្រូវទេ!")
            writer = PdfWriter()
            for page in reader.pages: writer.add_page(page)
            with open(output_path, "wb") as f: writer.write(f)
            await context.bot.edit_message_text(f"🔓 ដោះកូដ PDF បានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
            await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename=f"Unlocked_{filename}")
            
        elif ext in ['.docx', '.xlsx', '.pptx', '.doc', '.xls', '.ppt']:
            await context.bot.edit_message_text(f"កំពុងដោះសោរឯកសារ MS Office...", chat_id=chat_id, message_id=msg.message_id)
            office_file = open(file_path, "rb")
            office = msoffcrypto.OfficeFile(office_file)
            office.load_key(password=password)
            with open(output_path, "wb") as f: office.decrypt(f)
            office_file.close()
            await context.bot.edit_message_text(f"🔓 ដោះកូដ Office បានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
            await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename=f"Unlocked_{filename}")
            
        elif ext == '.zip':
            os.makedirs(extract_dir, exist_ok=True)
            with pyzipper.AESZipFile(file_path, 'r') as zf:
                zf.extractall(path=extract_dir, pwd=password.encode('utf-8'))
            await send_extracted_files(chat_id, extract_dir, msg, context)
            
        elif ext == '.rar':
            os.makedirs(extract_dir, exist_ok=True)
            with rarfile.RarFile(file_path, 'r') as rf:
                rf.extractall(path=extract_dir, pwd=password)
            await send_extracted_files(chat_id, extract_dir, msg, context)
        else:
            raise ValueError("ប្រព័ន្ធមិនទាន់គាំទ្រការដោះកូដសម្រាប់ឯកសារប្រភេទនេះទេ។ សូមផ្ញើ PDF, MS Office, ZIP, ឬ RAR។")
            
    except Exception as e:
        err_msg = str(e)
        if "Bad password" in err_msg or "password incorrect" in err_msg.lower(): err_msg = "លេខកូដសម្ងាត់មិនត្រឹមត្រូវទេ!"
        await context.bot.edit_message_text(f"បរាជ័យក្នុងការដោះសោរ៖\nកំហុស: {err_msg}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if os.path.exists(extract_dir): shutil.rmtree(extract_dir)
        if msg and ext not in ['.zip', '.rar']: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def send_extracted_files(chat_id, extract_dir, msg, context):
    extracted_files = os.listdir(extract_dir)
    if not extracted_files: raise ValueError("ឯកសារ Archive គឺទទេ។")
    await context.bot.edit_message_text(f"🔓 ដោះកូដ និងពន្លាបាន {len(extracted_files)} ឯកសារ។ កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
    for filename in extracted_files:
        full_path = os.path.join(extract_dir, filename)
        if os.path.isfile(full_path):
            await context.bot.send_document(chat_id=chat_id, document=open(full_path, 'rb'))
    await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)

# ==========================================
# មុខងារ Background Tasks ចាស់ៗ រក្សាទុកដដែល
# ==========================================
async def pdf_to_img_task(chat_id, file_path, msg, context, fmt):
    try:
        images = convert_from_path(file_path, dpi=200, fmt=fmt)
        await context.bot.edit_message_text(f"បំប្លែងបាន {len(images)} ទំព័រ។ កំពុងផ្ញើរូបភាព...", chat_id=chat_id, message_id=msg.message_id)
        for i, image in enumerate(images):
            out_path = f"page_{i+1}_{chat_id}.{fmt}"
            image.save(out_path, fmt.upper())
            await context.bot.send_photo(chat_id=chat_id, photo=open(out_path, 'rb'))
            os.remove(out_path)
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def merge_pdf_task(chat_id, file_paths, msg, context):
    output_path = f"merged_{chat_id}.pdf"
    try:
        merger = PdfMerger()
        for path in file_paths: merger.append(path)
        merger.write(output_path)
        merger.close()
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Merged.pdf")
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
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
        for part in page_range_str.split(','):
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                for i in range(start, end + 1): pages_to_extract.add(i-1)
            else:
                pages_to_extract.add(int(part)-1)
        for i in sorted(list(pages_to_extract)):
            if 0 <= i < len(reader.pages): writer.add_page(reader.pages[i])
        writer.write(output_path)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Split.pdf")
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
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
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Compressed.pdf")
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def img_to_pdf_task(chat_id, file_paths, msg, context):
    output_path = f"converted_{chat_id}.pdf"
    try:
        image_list = [Image.open(path).convert('RGB') for path in file_paths]
        image_list[0].save(output_path, "PDF", resolution=100.0, save_all=True, append_images=image_list[1:])
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Image_to_PDF.pdf")
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        for path in file_paths:
            if os.path.exists(path): os.remove(path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def img_to_text_task(chat_id, file_path, msg, context):
    try:
        text = pytesseract.image_to_string(Image.open(file_path), lang='khm+eng')
        if not text.strip(): await context.bot.send_message(chat_id=chat_id, text="មិនអាចរកឃើញអក្សរទេ")
        else: await context.bot.send_message(chat_id=chat_id, text=f"**លទ្ធផល៖**\n\n```\n{text}\n```", parse_mode='Markdown')
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def media_conversion_task(chat_id, file_path, output_format, msg, context, media_type='audio'):
    output_path = f"converted_{chat_id}.{output_format}"
    try:
        ffmpeg.input(file_path).output(output_path).run(overwrite_output=True)
        if media_type == 'audio': await context.bot.send_audio(chat_id=chat_id, audio=open(output_path, 'rb'))
        else: await context.bot.send_video(chat_id=chat_id, video=open(output_path, 'rb'))
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def create_zip_task(chat_id, file_paths, msg, context):
    output_path = f"archive_{chat_id}.zip"
    try:
        with zipfile.ZipFile(output_path, 'w') as zipf:
            for file_path in file_paths: zipf.write(file_path, os.path.basename(file_path))
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="archive.zip")
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
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
        os.makedirs(extract_dir, exist_ok=True)
        if file_path.endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as zip_ref: zip_ref.extractall(extract_dir)
        else:
            with tarfile.open(file_path, 'r:*') as tar_ref: tar_ref.extractall(extract_dir)
        await send_extracted_files(chat_id, extract_dir, msg, context)
    except Exception as e: await context.bot.edit_message_text(f"កំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.isdir(extract_dir): shutil.rmtree(extract_dir)

# ==========================================
# Handlers សម្រាប់ទទួលបញ្ជាពី User
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("📄 PDF ទៅជា រូបភាព", callback_data='pdf_to_img'), InlineKeyboardButton("🖼️ រូបភាព ទៅជា PDF", callback_data='img_to_pdf')],
        [InlineKeyboardButton("🖇️ បញ្ចូល PDF ចូលគ្នា", callback_data='merge_pdf'), InlineKeyboardButton("✂️ បំបែក PDF ជាទំព័រៗ", callback_data='split_pdf')],
        [InlineKeyboardButton("📦 បន្ថយទំហំ PDF", callback_data='compress_pdf'), InlineKeyboardButton("📖 រូបភាព ទៅជា អក្សរ", callback_data='img_to_text')],
        [InlineKeyboardButton("🔒 ចាក់សោរឯកសារ (គ្រប់ប្រភេទ)", callback_data='encrypt_pdf')],
        [InlineKeyboardButton("🔓 ដោះសោរឯកសារ (គ្រប់ប្រភេទ)", callback_data='decrypt_pdf')],
        [InlineKeyboardButton("🎵 បំប្លែងសម្លេង", callback_data='audio_converter'), InlineKeyboardButton("🎬 បំប្លែងវីដេអូ", callback_data='video_converter')],
        [InlineKeyboardButton("🗜️ គ្រប់គ្រងឯកសារ Archive", callback_data='archive_manager')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = '👋 សួស្តី! ខ្ញុំគឺជា Bot ជំនួយការឯកសារ។ សូមជ្រើសរើសមុខងារ៖'
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else: await update.message.reply_text(text, reply_markup=reply_markup)
    return SELECT_ACTION

async def start_encrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = "🔒 សូមផ្ញើឯកសារណាមួយ (PDF, Excel, Word, Image...) ដើម្បីដាក់លេខកូដការពារ។"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text)
    else: await update.message.reply_text(text)
    return WAITING_FOR_ENCRYPT_FILE

async def receive_file_for_encrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("❌ ឯកសារធំពេក (លើស 50MB)។")
        return WAITING_FOR_ENCRYPT_FILE
    file = await doc.get_file()
    file_path = f"temp_{file.file_id}_{doc.file_name}"
    await file.download_to_drive(file_path)
    context.user_data['encrypt_file_path'] = file_path
    context.user_data['encrypt_filename'] = doc.file_name
    await update.message.reply_text(f"✅ បានទទួលឯកសារ {doc.file_name}។\n\n🔑 សូមវាយបញ្ចូល **លេខកូដសម្ងាត់ (Password)** ដែលចង់ដាក់៖")
    return WAITING_FOR_PASSWORD

async def receive_password_for_encrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    file_path = context.user_data.get('encrypt_file_path')
    filename = context.user_data.get('encrypt_filename')
    try: await update.message.delete()
    except Exception: pass
    msg = await update.message.reply_text("កំពុងចាក់សោរ...")
    asyncio.create_task(encrypt_file_task(update.effective_chat.id, file_path, password, filename, msg, context))
    context.user_data.clear()
    return ConversationHandler.END

async def start_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = "🔓 សូមផ្ញើឯកសារដែលជាប់សោរ (PDF, MS Office, ZIP, RAR) ដើម្បីដោះ។"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text)
    else: await update.message.reply_text(text)
    return WAITING_FOR_DECRYPT_FILE

async def receive_file_for_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("❌ ឯកសារធំពេក (លើស 50MB)។")
        return WAITING_FOR_DECRYPT_FILE
    file = await doc.get_file()
    file_path = f"temp_{file.file_id}_{doc.file_name}"
    await file.download_to_drive(file_path)
    context.user_data['decrypt_file_path'] = file_path
    context.user_data['decrypt_filename'] = doc.file_name
    await update.message.reply_text(f"✅ បានទទួលឯកសារ {doc.file_name}។\n\n🔑 សូមវាយបញ្ចូល **លេខកូដចាស់ (Password)** ដើម្បីដោះសោរ៖")
    return WAITING_FOR_DECRYPT_PASSWORD

async def receive_password_for_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    file_path = context.user_data.get('decrypt_file_path')
    filename = context.user_data.get('decrypt_filename')
    try: await update.message.delete()
    except Exception: pass
    msg = await update.message.reply_text("កំពុងដោះសោរ...")
    asyncio.create_task(decrypt_file_task(update.effective_chat.id, file_path, password, filename, msg, context))
    context.user_data.clear()
    return ConversationHandler.END

# ----------------- រក្សាទុក Handlers ចាស់ៗ -----------------
async def start_pdf_to_img(update, context):
    query = update.callback_query; await query.answer()
    keyboard = [[InlineKeyboardButton("➡️ JPG", callback_data='fmt_jpeg'), InlineKeyboardButton("➡️ PNG", callback_data='fmt_png')]]
    await query.edit_message_text(text="សូមជ្រើសរើសប្រភេទរូបភាព៖", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ACTION

async def start_conversion_with_format(update, context):
    query = update.callback_query; context.user_data['format'] = "jpeg" if query.data == 'fmt_jpeg' else "png"
    await query.answer(); await query.edit_message_text(f"សូមផ្ញើ PDF មក (មិនលើស 50MB)")
    return WAITING_PDF_TO_IMG_FILE

async def receive_pdf_for_img(update, context):
    doc = update.message.document; file = await doc.get_file()
    file_path = f"temp_{file.file_id}.pdf"
    await file.download_to_drive(file_path)
    msg = await update.message.reply_text("កំពុងបំប្លែង..."); asyncio.create_task(pdf_to_img_task(update.effective_chat.id, file_path, msg, context, context.user_data.get('format', 'jpeg')))
    return ConversationHandler.END

async def start_merge(update, context):
    query = update.callback_query; await query.answer(); context.user_data['merge_files'] = []
    await query.edit_message_text("ផ្ញើឯកសារ PDF ម្ដងមួយៗ។ រួចរាល់វាយ /done")
    return WAITING_FOR_MERGE

async def receive_pdf_for_merge(update, context):
    doc = update.message.document; file = await doc.get_file(); file_path = f"temp_{file.file_id}.pdf"
    await file.download_to_drive(file_path); context.user_data['merge_files'].append(file_path)
    await update.message.reply_text(f"ទទួលឯកសារទី {len(context.user_data['merge_files'])}។ វាយ /done បើចង់បញ្ចប់"); return WAITING_FOR_MERGE

async def done_merging(update, context):
    msg = await update.message.reply_text("កំពុងបញ្ចូល..."); asyncio.create_task(merge_pdf_task(update.effective_chat.id, context.user_data['merge_files'], msg, context))
    return ConversationHandler.END

async def start_split(update, context):
    query = update.callback_query; await query.answer(); await query.edit_message_text("សូមផ្ញើ PDF មួយដើម្បីបំបែក។")
    return WAITING_FOR_SPLIT_FILE

async def receive_pdf_for_split(update, context):
    doc = update.message.document; file = await doc.get_file(); file_path = f"temp_{file.file_id}.pdf"; await file.download_to_drive(file_path)
    context.user_data['split_file_path'] = file_path; await update.message.reply_text("សូមវាយលេខទំព័រ (ឧ. 2-5 ឬ 1,3)")
    return WAITING_FOR_SPLIT_RANGE

async def receive_split_range(update, context):
    msg = await update.message.reply_text("កំពុងបំបែក..."); asyncio.create_task(split_pdf_task(update.effective_chat.id, context.user_data.get('split_file_path'), update.message.text, msg, context))
    return ConversationHandler.END

async def start_compress(update, context):
    query = update.callback_query; await query.answer(); await query.edit_message_text("សូមផ្ញើ PDF ដែលចង់បន្ថយទំហំ។")
    return WAITING_FOR_COMPRESS

async def receive_pdf_for_compress(update, context):
    doc = update.message.document; file = await doc.get_file(); file_path = f"temp_{file.file_id}.pdf"; await file.download_to_drive(file_path)
    msg = await update.message.reply_text("កំពុងបន្ថយទំហំ..."); asyncio.create_task(compress_pdf_task(update.effective_chat.id, file_path, msg, context))
    return ConversationHandler.END

async def start_img_to_pdf(update, context):
    query = update.callback_query; await query.answer(); context.user_data['img_to_pdf_files'] = []
    await query.edit_message_text("ផ្ញើរូបភាពម្ដងមួយៗ។ ពេលរួចរាល់វាយ /done")
    return WAITING_FOR_IMG_TO_PDF

async def receive_img_for_pdf(update, context):
    file_obj = update.message.photo[-1] if update.message.photo else update.message.document
    file = await file_obj.get_file(); file_path = f"temp_{file.file_id}.jpg"; await file.download_to_drive(file_path)
    context.user_data['img_to_pdf_files'].append(file_path); await update.message.reply_text(f"ទទួលបានរូបភាពទី {len(context.user_data['img_to_pdf_files'])}។ វាយ /done ពេលចប់")
    return WAITING_FOR_IMG_TO_PDF

async def done_img_to_pdf(update, context):
    msg = await update.message.reply_text("កំពុងបំប្លែង..."); asyncio.create_task(img_to_pdf_task(update.effective_chat.id, context.user_data['img_to_pdf_files'], msg, context))
    return ConversationHandler.END

async def start_img_to_text(update, context):
    query = update.callback_query; await query.answer(); await query.edit_message_text("សូមផ្ញើរូបភាពមួយដើម្បីទាញអក្សរ។")
    return WAITING_FOR_IMG_TO_TEXT_FILE

async def receive_img_for_text(update, context):
    file_obj = update.message.photo[-1] if update.message.photo else update.message.document
    file = await file_obj.get_file(); file_path = f"temp_{file.file_id}.jpg"; await file.download_to_drive(file_path)
    msg = await update.message.reply_text("កំពុងអានអក្សរពីរូបភាព..."); asyncio.create_task(img_to_text_task(update.effective_chat.id, file_path, msg, context))
    return ConversationHandler.END

# ---- Archive & Media ---- (រក្សាកូដខ្លីៗ)
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("ប្រតិបត្តិការត្រូវបានបោះបង់។")
    return ConversationHandler.END

def main() -> None:
    if not BOT_TOKEN or not WEBHOOK_URL:
        print("!!! កំហុស៖ សូមឆែក BOT_TOKEN និង RENDER_EXTERNAL_URL")
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).read_timeout(30).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_ACTION: [
                CallbackQueryHandler(start_encrypt, pattern='^encrypt_pdf$'),
                CallbackQueryHandler(start_decrypt, pattern='^decrypt_pdf$'),
                CallbackQueryHandler(start_pdf_to_img, pattern='^pdf_to_img$'),
                CallbackQueryHandler(start_conversion_with_format, pattern='^fmt_'),
                CallbackQueryHandler(start_merge, pattern='^merge_pdf$'),
                CallbackQueryHandler(start_split, pattern='^split_pdf$'),
                CallbackQueryHandler(start_compress, pattern='^compress_pdf$'),
                CallbackQueryHandler(start_img_to_pdf, pattern='^img_to_pdf$'),
                CallbackQueryHandler(start_img_to_text, pattern='^img_to_text$'),
            ],
            WAITING_FOR_ENCRYPT_FILE: [MessageHandler(filters.Document.ALL, receive_file_for_encrypt)],
            WAITING_FOR_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password_for_encrypt)],
            WAITING_FOR_DECRYPT_FILE: [MessageHandler(filters.Document.ALL, receive_file_for_decrypt)],
            WAITING_FOR_DECRYPT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password_for_decrypt)],
            
            # States ចាស់ៗ
            WAITING_PDF_TO_IMG_FILE: [MessageHandler(filters.Document.PDF, receive_pdf_for_img)],
            WAITING_FOR_MERGE: [MessageHandler(filters.Document.PDF, receive_pdf_for_merge), CommandHandler('done', done_merging)],
            WAITING_FOR_SPLIT_FILE: [MessageHandler(filters.Document.PDF, receive_pdf_for_split)],
            WAITING_FOR_SPLIT_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_split_range)],
            WAITING_FOR_COMPRESS: [MessageHandler(filters.Document.PDF, receive_pdf_for_compress)],
            WAITING_FOR_IMG_TO_PDF: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_img_for_pdf), CommandHandler('done', done_img_to_pdf)],
            WAITING_FOR_IMG_TO_TEXT_FILE: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_img_for_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    application.add_handler(conv_handler)
    
    print(f">>> Bot កំពុងដំណើរការដោយ Webhook នៅលើ Host: 0.0.0.0, Port: {PORT}")
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=WEBHOOK_URL + '/' + BOT_TOKEN)

if __name__ == "__main__": main()
