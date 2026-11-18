"use client";

import type { ReactNode } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { LOCALE_KEY } from "@/lib/api";

export type Locale = "fa" | "en";

const fa = {
  appName: "ونداد",
  appTagline: "کارخانه تولید محتوای هوشمند",
  language: "زبان",
  persian: "فارسی",
  english: "English",

  authHeroTitle: "تولید ویدیوی برند، از ایده تا انتشار",
  authHeroText:
    "پروفایل برند را انتخاب کنید، موضوع را بنویسید و فرآیند تولید را با Agentهای ونداد اجرا کنید.",
  authFeature1: "سناریوسازی فارسی با هوش مصنوعی",
  authFeature2: "پایش زنده مراحل Agent",
  authFeature3: "خروجی ویدیویی قابل بازبینی و دانلود",
  loginTab: "ورود",
  registerTab: "ثبت‌نام",
  loginTitle: "ورود به حساب کاربری",
  loginSubtitle: "برای ادامه، ایمیل و رمز عبور خود را وارد کنید.",
  registerTitle: "ساخت حساب جدید",
  registerSubtitle: "پس از ثبت‌نام، حساب شما تا تأیید مدیر غیرفعال می‌ماند.",
  email: "ایمیل",
  password: "رمز عبور",
  confirmPassword: "تکرار رمز عبور",
  fullName: "نام و نام خانوادگی",
  emailPlaceholder: "name@example.com",
  passwordPlaceholder: "حداقل ۸ کاراکتر شامل حرف و عدد",
  namePlaceholder: "مثلاً سارا محمدی",
  login: "ورود",
  register: "ثبت‌نام",
  loggingIn: "در حال ورود…",
  registering: "در حال ثبت‌نام…",
  switchToRegister: "حساب ندارید؟ ثبت‌نام کنید",
  switchToLogin: "حساب دارید؟ وارد شوید",
  approvalNotice: "حساب‌های جدید پس از تأیید مدیر فعال می‌شوند.",
  secureNotice: "ارتباط با سرور امن است و رمز عبور شما ذخیره‌ی خام نمی‌شود.",

  validationName: "نام و نام خانوادگی را کامل وارد کنید.",
  validationEmail: "ایمیل را وارد کنید.",
  validationEmailInvalid: "قالب ایمیل معتبر نیست.",
  validationPassword: "رمز عبور را وارد کنید.",
  validationPasswordLength: "رمز عبور باید حداقل ۸ کاراکتر باشد.",
  validationPasswordMix: "رمز عبور باید شامل حرف و عدد باشد.",
  validationPasswordMismatch: "تکرار رمز عبور مطابقت ندارد.",

  pendingTitle: "حساب شما در انتظار تأیید است",
  pendingBody:
    "ثبت‌نام شما با موفقیت انجام شد. مدیر سامانه باید حساب شما را تأیید کند؛ این صفحه به‌صورت خودکار به‌روزرسانی می‌شود.",
  pendingStatus: "وضعیت فعلی",
  pendingRefresh: "بررسی مجدد",
  pendingRefreshing: "در حال بررسی…",
  pendingLogout: "خروج از حساب",
  pendingPolling: "بررسی خودکار هر چند ثانیه",
  pendingApproved: "حساب شما تأیید شد. در حال ورود به داشبورد…",

  rejectedTitle: "دسترسی این حساب رد شده است",
  rejectedBody:
    "درخواست دسترسی شما توسط مدیر رد شده است. برای پیگیری با مدیر سامانه تماس بگیرید.",
  rejectedLogout: "خروج از حساب",

  dashboardGreeting: "سلام",
  dashboardSubtitle: "یک پروفایل برند انتخاب کنید و فرآیند تولید را آغاز کنید.",
  logout: "خروج",
  roleAdmin: "مدیر",
  roleMember: "کاربر",
  signedInAs: "وارد شده به‌عنوان",

  profilesTitle: "پروفایل‌های برند",
  profilesSubtitle: "پلتفرم محتوایی موردنظر را انتخاب کنید.",
  addProfile: "افزودن پروفایل",
  noProfiles: "هنوز پروفایلی ثبت نشده است.",
  noProfilesHint: "برای شروع، نخستین پروفایل برند را بسازید.",
  selectedBadge: "انتخاب‌شده",
  select: "انتخاب",
  brandName: "شناسه برند",
  brandDisplayName: "نام نمایشی",
  brandTone: "لحن",
  brandLanguage: "زبان محتوا",
  brandDescription: "توضیحات",
  brandIndustry: "صنعت",
  brandDomain: "دامنه",
  createProfile: "ساخت پروفایل",
  creatingProfile: "در حال ساخت…",
  cancel: "انصراف",
  close: "بستن",
  required: "الزامی",

  productionTitle: "اجرای فرآیند تولید",
  productionSubtitle:
    "موضوع ویدیو را مشخص کنید؛ سناریو، تأیید و تولید ویدیو به‌صورت خودکار اجرا می‌شود.",
  topicLabel: "موضوع ویدیو",
  topicPlaceholder: "مثلاً معرفی خدمات مشاوره حقوقی آنلاین",
  durationLabel: "مدت تقریبی (ثانیه)",
  characterLabel: "تصویر مرجع شخصیت",
  characterHint: "برای مدل تصویر-به-ویدیو، انتخاب شخصیت الزامی است.",
  providerLabel: "سرویس تولید ویدیو",
  providerAuto: "انتخاب خودکار توسط سرور",
  startProcess: "شروع فرآیند",
  startingProcess: "در حال آغاز…",
  duplicateSubmission: "این فرآیند همین حالا در حال اجراست.",

  workflowId: "شناسه اجرای Workflow",
  projectId: "شناسه پروژه",
  noWorkflowYet: "هنوز اجرایی ثبت نشده است.",
  openWorkflow: "اجرای جاری",

  timelineTitle: "پیشرفت Agentها",
  timelineEmpty: "پس از آغاز فرآیند، مراحل اینجا نمایش داده می‌شوند.",
  stepAttempt: "تلاش",
  stepDuration: "مدت",

  statusIdle: "آماده",
  statusScenario: "تولید سناریو",
  statusVideo: "تولید ویدیو",
  statusCompleted: "تکمیل‌شده",
  statusFailed: "ناموفق",
  statusCancelled: "لغو‌شده",

  scenarioTitle: "سناریوی تولیدشده",
  scenarioEmpty: "سناریو هنوز آماده نیست.",
  scenarioApproved: "سناریو تأیید شد",
  scenesCount: "تعداد صحنه",

  videoTitle: "ویدیوی نهایی",
  videoNotReady: "ویدیو پس از تکمیل فرآیند در همین بخش پخش می‌شود.",
  videoUnavailable: "خروجی ویدیویی برای این پروژه ثبت نشده است.",
  downloadVideo: "دانلود ویدیو",
  downloading: "در حال آماده‌سازی…",
  metadataTitle: "مشخصات خروجی",
  metaVideoTitle: "عنوان ویدیو",
  metaCta: "فراخوان اقدام",
  metaProvider: "سرویس‌دهنده",
  metaCost: "هزینه ثبت‌شده",
  metaDuration: "مدت",
  metaResolution: "ابعاد",
  qcTitle: "بازبینی کیفیت",
  qcEmpty: "اطلاعات بازبینی کیفیت توسط سرویس‌دهنده ارائه نشده است.",
  seconds: "ثانیه",

  retry: "تلاش مجدد",
  cancelWorkflow: "لغو فرآیند",
  cancelRequested: "درخواست لغو ثبت شد.",
  refresh: "به‌روزرسانی",
  retryScenario: "تولید مجدد سناریو",
  retryVideo: "تولید مجدد ویدیو",

  errorCredentials: "ایمیل یا رمز عبور نادرست است.",
  errorEmailTaken: "حسابی با این ایمیل قبلاً ثبت شده است.",
  errorLoad: "دریافت اطلاعات ناموفق بود.",
  errorStart: "آغاز فرآیند ناموفق بود.",
  errorPoll: "به‌روزرسانی وضعیت ناموفق بود.",
  errorVideo: "دریافت ویدیو ناموفق بود.",
  errorCancel: "لغو فرآیند ناموفق بود.",
  errorRetry: "تلاش مجدد ناموفق بود.",
  errorProfile: "ساخت پروفایل ناموفق بود.",
  errorCharacters: "دریافت تصاویر شخصیت ناموفق بود.",
  errorProviders: "دریافت سرویس‌دهنده‌ها ناموفق بود.",

  noProfileSelected: "ابتدا یک پروفایل برند انتخاب کنید.",
  noCharacterSelected: "برای این سرویس‌دهنده، انتخاب تصویر شخصیت الزامی است.",
  topicTooShort: "موضوع باید حداقل ۳ کاراکتر باشد.",
  durationRange: "مدت باید بین ۵ تا ۱۲۰ ثانیه باشد.",

  autoApproveNote:
    "سناریوی تولیدشده به‌صورت خودکار تأیید و تولید ویدیو آغاز می‌شود؛ هر مرحله از Workflow واقعی سرور اجرا می‌شود.",
  adminTitle: "تأیید حساب‌های کاربری",
  adminSubtitle: "حساب‌های در انتظار تأیید را بررسی و تأیید یا رد کنید.",
  approvals: "درخواست‌های تأیید",
  approve: "تأیید",
  reject: "رد",
  noPendingUsers: "هیچ حساب در انتظار تأییدی وجود ندارد.",
  pendingCount: "{count} در انتظار",
  decisionSaved: "تصمیم ثبت شد.",
  loading: "در حال بارگذاری…",
  noCharacters: "تصویر مرجع شخصیت معتبری یافت نشد.",
  noProviders: "سرویس تولید ویدیویی ثبت نشده است.",
  characterInvalid: "این تصویر مرجع معتبر نیست.",
  accountEmail: "ایمیل حساب",
  accountCreated: "تاریخ ثبت‌نام",
  newProcess: "فرآیند جدید",
  activeProcess: "فرآیند در جریان",
  approvalRequired: "دسترسی شما پس از تأیید مدیر فعال می‌شود.",
  requestFailed: "درخواست ناموفق بود.",
  dismiss: "بستن",
  retryHint: "پس از رفع خطا می‌توانید سناریو را دوباره تولید کنید.",
  noScenes: "صحنه‌ای برای نمایش وجود ندارد.",
  videoMetaUnavailable: "اطلاعات تکمیلی خروجی در دسترس نیست.",

  footerNote:
    "ونداد · پنل عملیات محتوا · دسترسی‌ها در سرور کنترل می‌شوند",
  unknown: "نامشخص",
  copy: "کپی",
  copied: "کپی شد",
  yes: "بله",
  no: "خیر",

  "project.draft": "پیش‌نویس",
  "project.script_generating": "در حال تولید سناریو",
  "project.script_ready": "سناریو آماده بازبینی",
  "project.script_failed": "تولید سناریو ناموفق",
  "project.approved": "سناریو تأییدشده",
  "project.scenes_prepared": "صحنه‌ها آماده شد",
  "project.video_generation_pending": "در انتظار تولید ویدیو",
  "project.video_generating": "در حال تولید ویدیو",
  "project.video_generated": "ویدیو تولید شد",
  "project.assembling": "در حال مونتاژ",
  "project.completed": "تکمیل‌شده",
  "project.failed": "ناموفق",
  "project.cancelled": "لغو‌شده",

  "execution.pending": "در انتظار",
  "execution.queued": "در صف",
  "execution.running": "در حال اجرا",
  "execution.waiting": "در انتظار سرویس‌دهنده",
  "execution.completed": "تکمیل‌شده",
  "execution.succeeded": "موفق",
  "execution.failed": "ناموفق",
  "execution.cancelled": "لغو‌شده",
  "execution.timed_out": "منقضی",

  "step.pending": "در انتظار",
  "step.queued": "در صف",
  "step.running": "در حال اجرا",
  "step.completed": "تکمیل‌شده",
  "step.failed": "ناموفق",
  "step.skipped": "رد شده",
  "step.cancelled": "لغو‌شده",
  "step.retrying": "تلاش مجدد",

  "node.start": "آغاز فرآیند",
  "node.finish": "پایان",
  "node.generate_scenario": "تولید سناریو",
  "node.script_generation": "تولید متن سناریو",
  "node.record_script": "ثبت سناریو",
  "node.prepare": "آماده‌سازی صحنه‌ها",
  "node.submit": "ارسال به سرویس ویدیو",
  "node.wait": "انتظار اولیه",
  "node.poll": "پایش وضعیت ویدیو",
  "node.retrieve": "دریافت ویدیوها",
  "node.assemble": "مونتاژ ویدیوی نهایی",
} as const;

export type MessageKey = keyof typeof fa;

const en: Record<MessageKey, string> = {
  appName: "Vandad",
  appTagline: "Intelligent content production",
  language: "Language",
  persian: "فارسی",
  english: "English",

  authHeroTitle: "Brand video production, from idea to delivery",
  authHeroText:
    "Pick a brand profile, describe the topic, and let Vandad's agents run the production pipeline.",
  authFeature1: "AI-generated Persian scenarios",
  authFeature2: "Live agent execution timeline",
  authFeature3: "Reviewable, downloadable video output",
  loginTab: "Sign in",
  registerTab: "Register",
  loginTitle: "Sign in to your account",
  loginSubtitle: "Enter your email and password to continue.",
  registerTitle: "Create an account",
  registerSubtitle:
    "New accounts stay inactive until an administrator approves them.",
  email: "Email",
  password: "Password",
  confirmPassword: "Confirm password",
  fullName: "Full name",
  emailPlaceholder: "name@example.com",
  passwordPlaceholder: "At least 8 characters with letters and digits",
  namePlaceholder: "e.g. Sara Mohammadi",
  login: "Sign in",
  register: "Register",
  loggingIn: "Signing in…",
  registering: "Registering…",
  switchToRegister: "No account? Register",
  switchToLogin: "Already registered? Sign in",
  approvalNotice: "New accounts activate after administrator approval.",
  secureNotice:
    "Traffic is encrypted and your password is never stored in plain text.",

  validationName: "Enter your full name.",
  validationEmail: "Enter your email address.",
  validationEmailInvalid: "That email address is not valid.",
  validationPassword: "Enter your password.",
  validationPasswordLength: "Password must be at least 8 characters.",
  validationPasswordMix: "Password must contain both letters and digits.",
  validationPasswordMismatch: "Passwords do not match.",

  pendingTitle: "Your account is awaiting approval",
  pendingBody:
    "Registration succeeded. An administrator must approve your account; this screen refreshes automatically.",
  pendingStatus: "Current status",
  pendingRefresh: "Check again",
  pendingRefreshing: "Checking…",
  pendingLogout: "Sign out",
  pendingPolling: "Automatically checked every few seconds",
  pendingApproved: "Your account was approved. Opening the dashboard…",

  rejectedTitle: "Access for this account was rejected",
  rejectedBody:
    "An administrator rejected your access request. Contact the administrator to follow up.",
  rejectedLogout: "Sign out",

  dashboardGreeting: "Hello",
  dashboardSubtitle: "Select a brand profile and start the production pipeline.",
  logout: "Sign out",
  roleAdmin: "Administrator",
  roleMember: "Member",
  signedInAs: "Signed in as",

  profilesTitle: "Brand profiles",
  profilesSubtitle: "Choose the content platform to work with.",
  addProfile: "Add profile",
  noProfiles: "No profiles yet.",
  noProfilesHint: "Create your first brand profile to get started.",
  selectedBadge: "Selected",
  select: "Select",
  brandName: "Brand key",
  brandDisplayName: "Display name",
  brandTone: "Tone",
  brandLanguage: "Content language",
  brandDescription: "Description",
  brandIndustry: "Industry",
  brandDomain: "Domain",
  createProfile: "Create profile",
  creatingProfile: "Creating…",
  cancel: "Cancel",
  close: "Close",
  required: "Required",

  productionTitle: "Run the production pipeline",
  productionSubtitle:
    "Describe the video topic; scenario generation, approval and rendering run automatically.",
  topicLabel: "Video topic",
  topicPlaceholder: "e.g. Introducing our online legal consultation service",
  durationLabel: "Approximate duration (seconds)",
  characterLabel: "Character reference image",
  characterHint: "Image-to-video models require a character reference.",
  providerLabel: "Video provider",
  providerAuto: "Let the server choose",
  startProcess: "Start process",
  startingProcess: "Starting…",
  duplicateSubmission: "This process is already running.",

  workflowId: "Workflow execution ID",
  projectId: "Project ID",
  noWorkflowYet: "No execution recorded yet.",
  openWorkflow: "Active execution",

  timelineTitle: "Agent progress",
  timelineEmpty: "Steps appear here once the process starts.",
  stepAttempt: "Attempt",
  stepDuration: "Duration",

  statusIdle: "Ready",
  statusScenario: "Generating scenario",
  statusVideo: "Rendering video",
  statusCompleted: "Completed",
  statusFailed: "Failed",
  statusCancelled: "Cancelled",

  scenarioTitle: "Generated scenario",
  scenarioEmpty: "The scenario is not ready yet.",
  scenarioApproved: "Scenario approved",
  scenesCount: "Scenes",

  videoTitle: "Final video",
  videoNotReady:
    "The video will play here once the pipeline completes.",
  videoUnavailable: "No video output was recorded for this project.",
  downloadVideo: "Download video",
  downloading: "Preparing…",
  metadataTitle: "Output metadata",
  metaVideoTitle: "Video title",
  metaCta: "Call to action",
  metaProvider: "Provider",
  metaCost: "Recorded cost",
  metaDuration: "Duration",
  metaResolution: "Resolution",
  qcTitle: "Quality review",
  qcEmpty: "The provider did not expose quality-review details.",
  seconds: "s",

  retry: "Retry",
  cancelWorkflow: "Cancel process",
  cancelRequested: "Cancellation requested.",
  refresh: "Refresh",
  retryScenario: "Regenerate scenario",
  retryVideo: "Regenerate video",

  errorCredentials: "Email or password is incorrect.",
  errorEmailTaken: "An account with this email already exists.",
  errorLoad: "Could not load the data.",
  errorStart: "Could not start the process.",
  errorPoll: "Could not refresh the status.",
  errorVideo: "Could not load the video.",
  errorCancel: "Could not cancel the process.",
  errorRetry: "Retry failed.",
  errorProfile: "Could not create the profile.",
  errorCharacters: "Could not load character images.",
  errorProviders: "Could not load providers.",

  noProfileSelected: "Select a brand profile first.",
  noCharacterSelected:
    "This provider requires a character reference image.",
  topicTooShort: "The topic must be at least 3 characters.",
  durationRange: "Duration must be between 5 and 120 seconds.",

  autoApproveNote:
    "The generated scenario is approved automatically and rendering starts; every step is a real server workflow execution.",
  adminTitle: "Account approvals",
  adminSubtitle: "Review accounts waiting for approval and approve or reject them.",
  approvals: "Approval requests",
  approve: "Approve",
  reject: "Reject",
  noPendingUsers: "No accounts are waiting for approval.",
  pendingCount: "{count} pending",
  decisionSaved: "Decision saved.",
  loading: "Loading…",
  noCharacters: "No valid character reference image was found.",
  noProviders: "No video provider is registered.",
  characterInvalid: "This reference image is not valid.",
  accountEmail: "Account email",
  accountCreated: "Registered at",
  newProcess: "New process",
  activeProcess: "Process running",
  approvalRequired: "Your access activates after an administrator approves it.",
  requestFailed: "The request failed.",
  dismiss: "Dismiss",
  retryHint: "You can regenerate the scenario after the problem is fixed.",
  noScenes: "There are no scenes to display.",
  videoMetaUnavailable: "Additional output details are not available.",

  footerNote:
    "Vandad · content operations · authorization is enforced by the server",
  unknown: "Unknown",
  copy: "Copy",
  copied: "Copied",
  yes: "Yes",
  no: "No",

  "project.draft": "Draft",
  "project.script_generating": "Generating scenario",
  "project.script_ready": "Scenario ready for review",
  "project.script_failed": "Scenario generation failed",
  "project.approved": "Scenario approved",
  "project.scenes_prepared": "Scenes prepared",
  "project.video_generation_pending": "Waiting to render",
  "project.video_generating": "Rendering video",
  "project.video_generated": "Video generated",
  "project.assembling": "Assembling",
  "project.completed": "Completed",
  "project.failed": "Failed",
  "project.cancelled": "Cancelled",

  "execution.pending": "Pending",
  "execution.queued": "Queued",
  "execution.running": "Running",
  "execution.waiting": "Waiting on provider",
  "execution.completed": "Completed",
  "execution.succeeded": "Succeeded",
  "execution.failed": "Failed",
  "execution.cancelled": "Cancelled",
  "execution.timed_out": "Timed out",

  "step.pending": "Pending",
  "step.queued": "Queued",
  "step.running": "Running",
  "step.completed": "Completed",
  "step.failed": "Failed",
  "step.skipped": "Skipped",
  "step.cancelled": "Cancelled",
  "step.retrying": "Retrying",

  "node.start": "Start",
  "node.finish": "Finish",
  "node.generate_scenario": "Generate scenario",
  "node.script_generation": "Generate script",
  "node.record_script": "Persist script",
  "node.prepare": "Prepare scenes",
  "node.submit": "Submit to video provider",
  "node.wait": "Initial wait",
  "node.poll": "Poll video status",
  "node.retrieve": "Retrieve videos",
  "node.assemble": "Assemble final video",
};

const catalogs: Record<Locale, Record<MessageKey, string>> = { fa, en };

type I18nValue = {
  locale: Locale;
  dir: "rtl" | "ltr";
  isRtl: boolean;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, params?: Record<string, string | number>) => string;
  formatNumber: (value: number) => string;
  formatDateTime: (value: string | null | undefined) => string;
  projectStatus: (status: string) => string;
  executionStatus: (status: string) => string;
  stepStatus: (status: string) => string;
  nodeLabel: (nodeKey: string) => string;
};

const I18nContext = createContext<I18nValue | null>(null);

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>("fa");

  useEffect(() => {
    const saved = window.localStorage.getItem(LOCALE_KEY);
    if (saved === "en" || saved === "fa") setLocaleState(saved);
  }, []);

  useEffect(() => {
    const dir = locale === "fa" ? "rtl" : "ltr";
    document.documentElement.lang = locale;
    document.documentElement.dir = dir;
    document.body.dataset.locale = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    window.localStorage.setItem(LOCALE_KEY, next);
  }, []);

  const value = useMemo<I18nValue>(() => {
    const catalog = catalogs[locale];
    const numberFormat = new Intl.NumberFormat(
      locale === "fa" ? "fa-IR" : "en-US",
    );
    const dateFormat = new Intl.DateTimeFormat(
      locale === "fa" ? "fa-IR" : "en-US",
      { dateStyle: "medium", timeStyle: "short" },
    );
    const t: I18nValue["t"] = (key, params) => {
      let message: string = catalog[key] ?? fa[key] ?? key;
      if (params) {
        for (const [name, raw] of Object.entries(params)) {
          message = message.replaceAll(`{${name}}`, String(raw));
        }
      }
      return message;
    };
    const statusOf = (prefix: "project" | "execution" | "step", status: string) => {
      const key = `${prefix}.${status}` as MessageKey;
      return catalog[key] ?? status;
    };
    return {
      locale,
      dir: locale === "fa" ? "rtl" : "ltr",
      isRtl: locale === "fa",
      setLocale,
      t,
      formatNumber: (value: number) => numberFormat.format(value),
      formatDateTime: (value) => {
        if (!value) return "—";
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return "—";
        return dateFormat.format(parsed);
      },
      projectStatus: (status) => statusOf("project", status),
      executionStatus: (status) => statusOf("execution", status),
      stepStatus: (status) => statusOf("step", status),
      nodeLabel: (nodeKey) => {
        const key = `node.${nodeKey}` as MessageKey;
        return catalog[key] ?? nodeKey.replaceAll("_", " ");
      },
    };
  }, [locale, setLocale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used inside LocaleProvider");
  }
  return context;
}
