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

# ដោះស្រាយបញ្ហា Error របស់ Tesseract (ប្រាប់ទីតាំងផ្ទាល់នៅក្នុង Linux)
pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

try:
    from PyPDF2 import PdfReader, PdfWriter, PdfMerger
    from pdf2image import convert_from_path
except ImportError:
    print("!!! កំហុស៖ សូមប្រាកដថាបានតម្លើង Library ទាំងអស់")
    sys.exit(1)

BOT_TOKEN: Final = os.environ.get("BOT_TOKEN", "") 
MAX_FILE_SIZE: Final = 50 * 1024 * 1024 # 50 MB
WEBHOOK_URL: Final = os.environ.get("RENDER_EXTERNAL_URL", "") 
PORT: Final = int(os.environ.get("PORT", "8000")) 

# កំណត់ 'ស្ថានភាព' (States) ថែមដល់ 20 សម្រាប់មុខងារដោះកូដ
(SELECT_ACTION,
 WAITING_PDF_TO_IMG_FORMAT, WAITING_PDF_TO_IMG_FILE,
 WAITING_FOR_MERGE, WAITING_FOR_SPLIT_FILE, WAITING_FOR_SPLIT_RANGE,
 WAITING_FOR_COMPRESS,
 WAITING_FOR_IMG_TO_PDF,
 WAITING_FOR_IMG_TO_TEXT_FILE,
 SELECT_AUDIO_OUTPUT_FORMAT, WAITING_FOR_AUDIO_FILE,
 SELECT_VIDEO_OUTPUT_FORMAT, WAITING_FOR_VIDEO_FILE,
 SELECT_ARCHIVE_ACTION, WAITING_FOR_FILES_TO_ZIP, WAITING_FOR_ARCHIVE_TO_EXTRACT,
 WAITING_FOR_ENCRYPT_FILE, WAITING_FOR_PASSWORD,
 WAITING_FOR_DECRYPT_FILE, WAITING_FOR_DECRYPT_PASSWORD # <== ថែម ២ នេះ
) = range(20)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

def is_ffmpeg_installed():
    return True 

# === មុខងារចាក់សោរ និង ដោះសោរ PDF ===

async def encrypt_pdf_task(chat_id, file_path, password, msg, context):
    output_path = f"encrypted_{chat_id}.pdf"
    try:
        reader = PdfReader(file_path)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(password)
        with open(output_path, "wb") as f: 
            writer.write(f)
        await context.bot.edit_message_text(f"🔒 ដាក់លេខកូដបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Secured_Document.pdf")
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការដាក់លេខកូដឯកសារ។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

async def decrypt_pdf_task(chat_id, file_path, password, msg, context):
    output_path = f"decrypted_{chat_id}.pdf"
    try:
        reader = PdfReader(file_path)
        
        # ពិនិត្យមើលថាឯកសារពិតជាមានជាប់សោរឬអត់
        if not reader.is_encrypted:
            await context.bot.edit_message_text("❌ ឯកសារនេះមិនមានជាប់លេខកូដទេ។", chat_id=chat_id, message_id=msg.message_id)
            return
            
        # ព្យាយាមដោះសោរ
        result = reader.decrypt(password)
        if result == 0: 
            raise ValueError("លេខកូដសម្ងាត់ (Password) មិនត្រឹមត្រូវទេ!")
            
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
            
        with open(output_path, "wb") as f: 
            writer.write(f)
            
        await context.bot.edit_message_text(f"🔓 ដោះលេខកូដបានជោគជ័យ! កំពុងផ្ញើ...", chat_id=chat_id, message_id=msg.message_id)
        await context.bot.send_document(chat_id=chat_id, document=open(output_path, 'rb'), filename="Unlocked_Document.pdf")
    except Exception as e:
        await context.bot.edit_message_text(f"មានបញ្ហាក្នុងការដោះលេខកូដ។\nកំហុស: {e}", chat_id=chat_id, message_id=msg.message_id)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(output_path): os.remove(output_path)
        if msg: 
            try: await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception: pass

# === មុខងារផ្សេងៗរក្សាដដែល (សង្ខេបដើម្បីងាយអាន ប៉ុន្តែបងត្រូវប្រើកូដទាំងអស់) ===
# ខ្ញុំដាក់តែផ្នែកដែលកែប្រែនៅទីនេះ ឯកូដដទៃទៀតដូចជា pdf_to_img_task ជាដើម បងរក្សាទុកនៅដដែល។

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("📄 PDF ទៅជា រូបភាព", callback_data='pdf_to_img')],
        [InlineKeyboardButton("🖇️ បញ្ចូល PDF ច្រើនចូលគ្នា", callback_data='merge_pdf')],
        [InlineKeyboardButton("✂️ បំបែក PDF ជាទំព័រៗ", callback_data='split_pdf')],
        [InlineKeyboardButton("📦 បន្ថយទំហំ PDF", callback_data='compress_pdf')],
        [InlineKeyboardButton("🔒 ដាក់លេខកូដ PDF", callback_data='encrypt_pdf'),
         InlineKeyboardButton("🔓 ដោះលេខកូដ PDF", callback_data='decrypt_pdf')], # <== ថែមប៊ូតុងថ្មី
        [InlineKeyboardButton("🖼️ រូបភាព ទៅជា PDF", callback_data='img_to_pdf')],
        [InlineKeyboardButton("📖 រូបភាព ទៅជា អក្សរ (OCR)", callback_data='img_to_text')],
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

# [បញ្ចូលកូដ Task ចាស់ៗរបស់បងនៅត្រង់នេះ ដូចជា pdf_to_img_task, img_to_text_task...]

# --- Handlers សម្រាប់មុខងារ ដោះកូដ ---
async def start_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = "🔓 សូមផ្ញើឯកសារ PDF ដែលជាប់សោរ មកឱ្យខ្ញុំដើម្បីដោះ។"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    return WAITING_FOR_DECRYPT_FILE

async def receive_pdf_for_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ កំហុស៖ ឯកសារមានទំហំធំពេក។ (មិនលើស {int(MAX_FILE_SIZE / 1024 / 1024)}MB)។")
        return WAITING_FOR_DECRYPT_FILE
    
    file = await doc.get_file()
    file_path = f"temp_{file.file_id}.pdf"
    await file.download_to_drive(file_path)
    
    context.user_data['decrypt_file_path'] = file_path
    await update.message.reply_text("✅ ទទួលបានឯកសារ។\n\n🔑 ឥឡូវនេះ សូមវាយបញ្ចូល **លេខកូដសម្ងាត់ចាស់ (Password)** ដើម្បីដោះសោរ៖")
    return WAITING_FOR_DECRYPT_PASSWORD

async def receive_password_for_decrypt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    file_path = context.user_data.get('decrypt_file_path')
    
    try: await update.message.delete() # លុបសារលាក់ Password
    except Exception: pass
        
    msg = await update.message.reply_text("យល់ព្រម! កំពុងធ្វើការដោះសោរឯកសាររបស់អ្នក...")
    asyncio.create_task(decrypt_pdf_task(update.effective_chat.id, file_path, password, msg, context))
    context.user_data.clear()
    return ConversationHandler.END

# --- ត្រូវចាំកែ ConversationHandler ខាងក្រោមផង ---
def main() -> None:
    if not BOT_TOKEN:
        print("!!! កំហុស៖ BOT_TOKEN មិនត្រូវបានកំណត់។")
        sys.exit(1)
        
    if not WEBHOOK_URL:
        print("!!! កំហុស៖ RENDER_EXTERNAL_URL មិនត្រូវបានកំណត់។")
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).read_timeout(30).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            # បន្ថែម Command ដោះកូដ
            # (កុំភ្លេចបញ្ចូល command ចាស់ៗផ្សេងទៀតនៅទីនេះ)
        ],
        states={
            SELECT_ACTION: [
                # (បញ្ចូល callback ចាស់ៗនៅទីនេះ)
                CallbackQueryHandler(start_encrypt, pattern='^encrypt_pdf$'),
                CallbackQueryHandler(start_decrypt, pattern='^decrypt_pdf$'), # <== ថែមនេះ
                CallbackQueryHandler(start, pattern='^main_menu$'),
            ],
            # (បញ្ចូល state ចាស់ៗនៅទីនេះ)
            WAITING_FOR_ENCRYPT_FILE: [MessageHandler(filters.Document.PDF, receive_pdf_for_encrypt)],
            WAITING_FOR_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password_for_encrypt)],
            
            # ថែម State សម្រាប់មុខងារដោះកូដ
            WAITING_FOR_DECRYPT_FILE: [MessageHandler(filters.Document.PDF, receive_pdf_for_decrypt)],
            WAITING_FOR_DECRYPT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password_for_decrypt)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], # (បញ្ចូលមុខងារ cancel ចាស់)
        allow_reentry=True
    )
    
    application.add_handler(conv_handler)
    # [កូដ Webhook ផ្សេងៗខាងក្រោម រក្សាទុកនៅដដែល]
