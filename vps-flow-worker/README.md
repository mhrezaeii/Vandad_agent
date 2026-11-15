# کارگر Google Flow روی VPS خارجی — راهنمای استقرار

این بسته، تولید کلیپ‌های رایگان Flow را روی یک **VPS خارجی** اجرا می‌کند
(Flow از ایران فیلتر است؛ از IP خارجِ VPS آزاد است). یک اکانت گوگل شرکت،
یک سشن کوکی، سهمیه رایگان روزانه — بدون چرخش چند-اکانتی.

## معماری

```
مرورگر شما (لاگین Flow) ──► Export کوکی‌ها ──► VPS (worker + Playwright)
                                                  │  پرامپت‌های سناریو
                                                  ▼
                                        کلیپ‌های MP4 (رایگان)
                                                  │
              بازگشت به سیستم ──► assets/clips/<project>/ ──► مونتاژ خودکار
              (گویندگی فارسی + زیرنویس + اند‌کارت برند)
```

## راه‌اندازی روی VPS (اوبونتو)

```bash
# ۱) بسته را روی VPS کپی کنید (scp/sftp پوشه vps-flow-worker)
cd vps-flow-worker
bash install.sh

# ۲) کوکی‌ها: در مرورگر خودتان وارد https://labs.google/flow شوید
#    (با اکانت گوگل شرکت) → افزونه Cookie-Editor → Export (کلیپ‌بورد)
#    → در فایل cookies.json ذخیره → روی VPS آپلود →:
./.venv/bin/python flow_worker.py import-cookies cookies.json

# ۳) وضعیت:
./.venv/bin/python flow_worker.py status

# ۴) تولید کلیپ‌ها (prompts-vandiyar.txt همراه بسته است):
./.venv/bin/python flow_worker.py generate --prompt-file prompts-vandiyar.txt --out clips/
```

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
