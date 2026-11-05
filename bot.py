import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue
import json
import os
import requests
from datetime import datetime
import traceback
import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types

CONFIG_FILE = "bot_config.json"
log_queue = queue.Queue()
status_queue = queue.Queue()
models_queue = queue.Queue()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_queue.put(f"⚠️ فشل حفظ الإعدادات: {e}")

def verify_telegram_token(token: str) -> tuple[bool, str]:
    if not token or ":" not in token:
        return False, "التوكن فارغ أو غير صالح"
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = r.json()
        if data.get("ok"):
            return True, f"متصل كـ @{data['result'].get('username', '')}"
        return False, data.get("description", "توكن مرفوض")
    except Exception as e:
        return False, f"خطأ في الاتصال: {e}"

def verify_gemini_key(api_key: str, model_name: str) -> tuple[bool, str]:
    if not api_key:
        return False, "المفتاح فارغ"
    try:
        client = genai.Client(api_key=api_key, http_options={'api_version': 'v1beta'})
        response = client.models.generate_content(model=model_name, contents='قل: OK')
        if response and response.text:
            return True, f"مفتاح Gemini صالح"
        return False, "رد فارغ من Gemini"
    except Exception as e:
        return False, f"فشل الاتصال: {e}"

def fetch_available_models(api_key: str):
    try:
        client = genai.Client(api_key=api_key, http_options={'api_version': 'v1beta'})
        available = []
        for m in client.models.list():
            if 'generateContent' in m.supported_actions:
                name = m.name.replace("models/", "")
                available.append(name)
        available = sorted(available, key=lambda x: ('flash' not in x.lower(), x))
        models_queue.put(available)
    except Exception as e:
        log_queue.put(f"❌ فشل جلب النماذج: {e}")
        models_queue.put([])

class BotRunner(threading.Thread):
    def __init__(self, token, gemini_key, model_name):
        super().__init__(daemon=True)
        self.token = token
        self.gemini_key = gemini_key
        self.model_name = model_name
        self.application = None
        self.ai_client = genai.Client(api_key=gemini_key, http_options={'api_version': 'v1beta'})
        self.chat_histories = {}
        
        self.system_instruction = (
            "أنت مساعد ذكاء اصطناعي مفيد ومحترف. أجب على أسئلة المستخدم بوضوح ودقة وبشكل طبيعي."
        )

    def run(self):
        try:
            log_queue.put("⏳ جاري بناء تطبيق تيليجرام...")
            self.application = Application.builder().token(self.token).build()
            
            self.application.add_handler(CommandHandler("start", self.cmd_start))
            self.application.add_handler(CommandHandler("clear", self.cmd_clear))
            self.application.add_handler(MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, 
                self.handle_message
            ))

            log_queue.put(f"🟢 البوت يعمل الآن باستخدام النموذج: {self.model_name}...")
            status_queue.put(("running", "متصل (يعمل الآن)")) # حالة التشغيل الفعلي

            self.application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
            
            log_queue.put("⚪ تم إيقاف البوت")
            status_queue.put(("disconnected", "غير متصل"))
            
        except Exception as e:
            log_queue.put(f"❌ خطأ فادح في التشغيل:\n{e}")
            log_queue.put(traceback.format_exc())
            status_queue.put(("disconnected", "خطأ في التشغيل"))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("أهلاً بك! أنا مساعدك الذكي. كيف يمكنني مساعدتك اليوم؟")

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        self.chat_histories.pop(uid, None)
        await update.message.reply_text("✅ تم مسح ذاكرة المحادثة.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        user_text = update.message.text or update.message.caption or ""
        
        log_queue.put(f"📩 وصلت رسالة: {user_text[:20]}...")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        
        max_retries = 3
        retry_delay = 2

        for attempt in range(max_retries):
            try:
                if uid not in self.chat_histories:
                    self.chat_histories[uid] = self.ai_client.aio.chats.create(
                        model=self.model_name,
                        config=types.GenerateContentConfig(system_instruction=self.system_instruction)
                    )
                
                chat = self.chat_histories[uid]
                contents = []

                if update.message.photo:
                    photo_file = await update.message.photo[-1].get_file()
                    img_bytes = await photo_file.download_as_bytearray()
                    contents.append(types.Part.from_bytes(data=bytes(img_bytes), mime_type="image/jpeg"))
                    if not user_text: user_text = "ماذا يوجد في هذه الصورة؟ صفها لي."
                    contents.append(user_text)
                elif update.message.document:
                    doc = update.message.document
                    doc_file = await doc.get_file()
                    doc_bytes = await doc_file.download_as_bytearray()
                    mime = doc.mime_type or "application/octet-stream"
                    if "image" in mime or "pdf" in mime or "text" in mime:
                        contents.append(types.Part.from_bytes(data=bytes(doc_bytes), mime_type=mime))
                        if not user_text: user_text = f"لقد أرسلت لك هذا الملف ({doc.file_name})، اقرأه وصفه لي."
                        contents.append(user_text)
                    else:
                        await update.message.reply_text(f"عذراً، نوع الملف ({mime}) غير مدعوم.")
                        return
                else:
                    contents.append(user_text)

                if not contents: return

                log_queue.put(f"🤖 جاري إرسال الطلب إلى Gemini (محاولة {attempt+1})...")
                response = await chat.send_message(contents)
                answer = response.text if response and response.text else "لم أستطع توليد رد."
                
                await update.message.reply_text(answer)
                log_queue.put("✅ تم إرسال الرد بنجاح.")
                break

            except Exception as e:
                error_str = str(e)
                if "503" in error_str or "UNAVAILABLE" in error_str.upper():
                    if attempt < max_retries - 1:
                        log_queue.put(f"⚠️ السيرفر مزدحم (503). إعادة المحاولة بعد {retry_delay} ثواني...")
                        await asyncio.sleep(retry_delay)
                        continue
                
                error_msg = f"❌ خطأ أثناء الرد: {e}"
                log_queue.put(error_msg)
                log_queue.put(traceback.format_exc())
                try:
                    await update.message.reply_text(f"⚠️ حدث خطأ: {e}")
                except Exception:
                    pass
                break

    def stop(self):
        if self.application:
            self.application.stop_running()

class BotGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("بوت تيليجرام + Gemini (مستقر)")
        self.root.geometry("720x680")
        self.root.resizable(False, False)
        self.runner = None
        self.connected = False
        self._build_ui()
        self._load_saved_config()
        self._poll_queues()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)

        cfg = ttk.LabelFrame(main, text="⚙️ الإعدادات", padding=10)
        cfg.pack(fill="x", pady=(0, 10))

        ttk.Label(cfg, text="Telegram Bot Token:").grid(row=0, column=0, sticky="w", pady=5)
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(cfg, textvariable=self.token_var, width=55, show="*")
        self.token_entry.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(cfg, text="Gemini API Key:").grid(row=1, column=0, sticky="w", pady=5)
        self.gemini_var = tk.StringVar()
        self.gemini_entry = ttk.Entry(cfg, textvariable=self.gemini_var, width=55, show="*")
        self.gemini_entry.grid(row=1, column=1, padx=5, pady=5)

        model_frame = ttk.Frame(cfg)
        model_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        
        ttk.Label(model_frame, text="Gemini Model:").pack(side="left", padx=(0, 5))
        
        self.model_var = tk.StringVar(value="gemini-3.8-flash")
        self.model_combo = ttk.Combobox(
            model_frame, textvariable=self.model_var,
            values=["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
            width=30
        )
        self.model_combo.pack(side="left", padx=5)

        self.fetch_models_btn = ttk.Button(model_frame, text="✨ التحديد التلقائي", command=self.fetch_models)
        self.fetch_models_btn.pack(side="left", padx=5)

        status_frame = ttk.LabelFrame(main, text="📡 الحالة", padding=10)
        status_frame.pack(fill="x", pady=(0, 10))

        self.canvas = tk.Canvas(status_frame, width=24, height=24, highlightthickness=0)
        self.canvas.pack(side="left", padx=(0, 8))
        self.indicator = self.canvas.create_oval(4, 4, 20, 20, fill="gray", outline="")

        self.status_label = ttk.Label(status_frame, text="غير متصل", font=("Segoe UI", 11, "bold"))
        self.status_label.pack(side="left")

        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(0, 10))

        self.test_btn = ttk.Button(btn_frame, text="🔍 اختبار الاتصال", command=self.test_connection)
        self.test_btn.pack(side="left", padx=3)

        self.start_btn = ttk.Button(btn_frame, text="▶️ تشغيل البوت", command=self.start_bot)
        self.start_btn.pack(side="left", padx=3)

        self.stop_btn = ttk.Button(btn_frame, text="⏹️ إيقاف البوت", command=self.stop_bot, state="disabled")
        self.stop_btn.pack(side="left", padx=3)

        self.save_btn = ttk.Button(btn_frame, text="💾 حفظ الإعدادات", command=self.save_settings)
        self.save_btn.pack(side="left", padx=3)

        log_frame = ttk.LabelFrame(main, text="📋 سجل الأحداث", padding=5)
        log_frame.pack(fill="both", expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4"
        )
        self.log_text.pack(fill="both", expand=True)

        self.bottom_label = ttk.Label(main, text="جاهز", foreground="gray", font=("Segoe UI", 9))
        self.bottom_label.pack(anchor="w", pady=(5, 0))

    def _load_saved_config(self):
        cfg = load_config()
        self.token_var.set(cfg.get("telegram_token", ""))
        self.gemini_var.set(cfg.get("gemini_key", ""))
        self.model_var.set(cfg.get("model", "gemini-3.8-flash"))
        if cfg: self.log("تم تحميل الإعدادات المحفوظة.")

    def save_settings(self):
        save_config({
            "telegram_token": self.token_var.get().strip(),
            "gemini_key": self.gemini_var.get().strip(),
            "model": self.model_var.get(),
        })
        self.log("💾 تم حفظ الإعدادات.")
        self.bottom_label.config(text="تم الحفظ")

    def log(self, msg):
        self.log_text.config(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ⬇️ تم إصلاح هذه الدالة هنا ⬇️
    def set_status(self, status_type: str, text: str):
        self.connected = (status_type == "running")
        if status_type == "running":
            self.canvas.itemconfig(self.indicator, fill="#2ecc71") # أخضر
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.test_btn.config(state="disabled")
            self.token_entry.config(state="disabled")
            self.gemini_entry.config(state="disabled")
            self.model_combo.config(state="disabled")
            self.fetch_models_btn.config(state="disabled")
        elif status_type == "testing_ok":
            self.canvas.itemconfig(self.indicator, fill="#2ecc71") # أخضر
            self.start_btn.config(state="normal") # نترك زر التشغيل متاحاً!
            self.stop_btn.config(state="disabled")
            self.test_btn.config(state="normal")
        else: # disconnected
            self.canvas.itemconfig(self.indicator, fill="#e74c3c") # أحمر
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.test_btn.config(state="normal")
            self.token_entry.config(state="normal")
            self.gemini_entry.config(state="normal")
            self.model_combo.config(state="readonly")
            self.fetch_models_btn.config(state="normal")

        self.status_label.config(text=text)

    def fetch_models(self):
        gkey = self.gemini_var.get().strip()
        if not gkey:
            messagebox.showwarning("تنبيه", "أدخل مفتاح Gemini أولاً.")
            return
        self.log("🔄 جارٍ جلب النماذج من حسابك...")
        self.fetch_models_btn.config(state="disabled")
        threading.Thread(target=fetch_available_models, args=(gkey,), daemon=True).start()

    def test_connection(self):
        token = self.token_var.get().strip()
        gkey = self.gemini_var.get().strip()
        model = self.model_var.get().strip()
        if not token or not gkey:
            messagebox.showwarning("تنبيه", "أدخل التوكن ومفتاح Gemini أولاً.")
            return

        self.log(f"🔍 جارٍ اختبار الاتصال (النموذج: {model})...")
        self.bottom_label.config(text="جارٍ الاختبار...")

        def _test():
            ok_t, msg_t = verify_telegram_token(token)
            ok_g, msg_g = verify_gemini_key(gkey, model)
            if ok_t and ok_g:
                log_queue.put(f"✅ تيليجرام: {msg_t}")
                log_queue.put(f"✅ Gemini: {msg_g}")
                status_queue.put(("testing_ok", "متصل (تم الاختبار)")) # حالة الاختبار
            else:
                if not ok_t: log_queue.put(f"❌ تيليجرام: {msg_t}")
                if not ok_g: log_queue.put(f"❌ Gemini: {msg_g}")
                status_queue.put(("disconnected", "فشل الاختبار"))

        threading.Thread(target=_test, daemon=True).start()

    def start_bot(self):
        token = self.token_var.get().strip()
        gkey = self.gemini_var.get().strip()
        model = self.model_var.get().strip()

        if not token or not gkey:
            messagebox.showwarning("تنبيه", "أدخل التوكن ومفتاح Gemini أولاً.")
            return

        self.log(f"🚀 جارٍ تشغيل البوت باستخدام {model}...")
        self.bottom_label.config(text="جارٍ التشغيل...")
        self.start_btn.config(state="disabled")

        def _start():
            ok_t, msg_t = verify_telegram_token(token)
            if not ok_t:
                log_queue.put(f"❌ تيليجرام: {msg_t}")
                status_queue.put(("disconnected", "فشل الاتصال"))
                return

            ok_g, msg_g = verify_gemini_key(gkey, model)
            if not ok_g:
                log_queue.put(f"❌ Gemini: {msg_g}")
                status_queue.put(("disconnected", "فشل الاتصال"))
                return

            log_queue.put(f"✅ {msg_t}")
            log_queue.put(f"✅ {msg_g}")

            self.runner = BotRunner(token, gkey, model)
            self.runner.start()

        threading.Thread(target=_start, daemon=True).start()

    def stop_bot(self):
        if self.runner:
            self.log("⏹️ جارٍ إيقاف البوت...")
            self.runner.stop()
            self.runner = None
        self.set_status("disconnected", "غير متصل")

    def _poll_queues(self):
        while not log_queue.empty():
            try: self.log(log_queue.get_nowait())
            except queue.Empty: break

        while not status_queue.empty():
            try:
                status_type, text = status_queue.get_nowait()
                self.set_status(status_type, text)
                self.bottom_label.config(text=text)
            except queue.Empty: break

        while not models_queue.empty():
            try:
                models = models_queue.get_nowait()
                if models:
                    self.model_combo['values'] = models
                    self.model_var.set(models[0])
                    self.log(f"✅ تم العثور على {len(models)} نموذجًا.")
                    self.log(f"💡 تم تحديد: {models[0]}")
                    self.bottom_label.config(text="تم التحديد التلقائي")
                else:
                    self.log("⚠️ لم يتم العثور على نماذج.")
                self.fetch_models_btn.config(state="normal")
            except queue.Empty: break

        self.root.after(200, self._poll_queues)

if __name__ == "__main__":
    root = tk.Tk()
    app = BotGUI(root)
    root.mainloop()
