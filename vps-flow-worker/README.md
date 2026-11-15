# کارگر Google Flow روی VPS ویندوزی (Nesta) — راهنمای استقرار

این بسته، تولید کلیپ‌های رایگان Flow را روی **VPS ویندوز ۱۱ خارجی** اجرا
می‌کند (Flow از IP ایران فیلتر است؛ روی VPS آزاد است). یک اکانت گوگل شرکت،
**پروفایل مرورگر ماندگار** (لاگین تعاملی یک‌باره)، سهمیه رایگان روزانه —
بدون چرخش چند-اکانتی.

## معماری

```
RDP به VPS ──► python flow_worker.py login (مرورگر باز می‌شود، یک‌بار لاگین)
                    │  پروفایل ذخیره می‌شود
                    ▼
        python flow_worker.py generate --prompt-file prompts-vandiyar.txt
                    │  هر پرامپت = یک کلیپ MP4 رایگان
                    ▼
        clips/01..06.mp4 ──► کپی به سیستم اصلی ──► مونتاژ خودکار
        (گویندگی فارسی + زیرنویس + overlay + اند‌کارت برند)
```

## راه‌اندازی روی VPS ویندوزی

```bat
rem ۱) پوشه vps-flow-worker را روی VPS کپی کنید (RDP drag-drop یا دانلود از گیت‌هاب)
cd vps-flow-worker
install.bat

rem ۲) لاگین یک‌باره — مرورگر قابل‌مشاهده باز می‌شود، با اکانت گوگل شرکت
rem    وارد labs.google/flow شوید تا داشبورد Flow بیاید، بعد در CMD اینتر کنید:
python flow_worker.py login

rem ۳) وضعیت:
python flow_worker.py status

rem ۴) تولید کلیپ‌ها (۶ پرامپت استوری‌بورد همراه بسته):
python flow_worker.py generate --prompt-file prompts-vandiyar.txt --out clips
```

## بازگرداندن کلیپ‌ها به سیستم اصلی

`clips/01.mp4 ... 06.mp4` را از VPS بگیرید (RDP drag-drop) و در این پوشه
بگذارید:

```
E:\projectsandad_agentssets\clipsa6fe10a-46a7-4406-8afe-b463a7739c2c\n```

سپس بگویید «کلیپ‌ها آماده» — پایپ‌لاین مونتاژ (گویندگی فارسی + زیرنویس +
overlay اسکرین‌شات + اند‌کارت برند) خودکار اجرا می‌شود.

## عیب‌یابی

- هر شکست، اسکرین‌شات کامل در `state/shots/` ذخیره می‌کند — بفرستید تا
  selector ها را برای UI جدید Flow تنظیم کنم (همه در بالای `flow_worker.py`).
- سهمیه روزانه: `set FLOW_DAILY_LIMIT=6` (پیش‌فرض ۶). ریست: حذف `state\quota.json`
- سشن منقضی شد: دوباره `python flow_worker.py login`
- چون VPS ویندوزی است، worker با **مرورگر قابل‌مشاهده** اجرا می‌شود —
  می‌توانید فرایند را زنده ببینید؛ برای headless: `set FLOW_HEADLESS=1`

## بازگرداندن کلیپ‌ها به سیستم اصلی

کلپ‌های `clips/01.mp4 ... 06.mp4` را از VPS بگیرید (scp یا دانلود) و در این
پوشه بگذارید:

```
E:\projects\vandad_agent\assets\clips\ba6fe10a-46a7-4406-8afe-b463a7739c2c\
```

سپس بگویید «کلیپ‌ها آماده» — پایپ‌لاین مونتاژ (گویندگی فارسی + زیرنویس +
overlay اسکرین‌شات + اند‌کارت برند) خودکار اجرا می‌شود.

## عیب‌یابی

- هر شکست، اسکرین‌شات کامل در `state/shots/` ذخیره می‌کند — آن را بفرستید
  تا selector ها را برای UI جدید Flow تنظیم کنم (تمام selector ها در
  بالای `flow_worker.py` در یک دیکشنری جمع‌اند).
- سهمیه روزانه: `FLOW_DAILY_LIMIT` (پیش‌فرض ۶). ریست: حذف `state/quota.json`
- سشن منقضی شد («session signed out»): کوکی‌های تازه export و re-import کنید.
- ریسک شناخته‌شده: اتوماسیون Flow نقض ToS گوگل است — اکانت اختصاصی شرکت
  استفاده شود و انتظار بن‌شدن را داشته باشیم؛ در آن صورت مسیر جایگزین
  (WaveSpeed/8Scale پرداختی) یک env-change فاصله دارد.

## فایل‌ها

- `flow_worker.py` — worker (import-cookies / status / generate / prompt)
- `install.sh` — نصب پیش‌نیازها روی اوبونتو
- `prompts-vandiyar.txt` — ۶ پرامپت استوری‌بورد وندیار (سبک مرجع)
