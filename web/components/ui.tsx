"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useI18n } from "@/lib/i18n";

export function cx(
  ...parts: Array<string | false | null | undefined>
): string {
  return parts.filter(Boolean).join(" ");
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label ? <span className="spinner-label">{label}</span> : null}
    </span>
  );
}

type Tone = "ok" | "warn" | "bad" | "idle" | "info";

export function statusTone(status: string): Tone {
  switch (status) {
    case "completed":
    case "succeeded":
    case "approved":
      return "ok";
    case "failed":
    case "timed_out":
    case "rejected":
    case "script_failed":
      return "bad";
    case "cancelled":
      return "idle";
    case "running":
    case "waiting":
    case "queued":
    case "pending":
    case "script_generating":
    case "video_generating":
    case "video_generation_pending":
    case "assembling":
    case "retrying":
      return "warn";
    default:
      return "info";
  }
}

export function StatusPill({
  status,
  label,
  className,
}: {
  status: string;
  label: string;
  className?: string;
}) {
  return (
    <span className={cx("pill", `pill--${statusTone(status)}`, className)}>
      <span className="pill-dot" aria-hidden="true" />
      {label}
    </span>
  );
}

export function CopyField({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  const copy = async () => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Clipboard permissions can be denied; the value stays selectable.
    }
    setCopied(true);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <div className="copy-field">
      <span className="copy-label">{label}</span>
      <div className="copy-row">
        <code dir="ltr">{value || "—"}</code>
        {value ? (
          <button
            type="button"
            className="button button--tiny button--ghost"
            onClick={() => void copy()}
          >
            {copied ? t("copied") : t("copy")}
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function Alert({
  tone = "info",
  children,
  onClose,
}: {
  tone?: "info" | "error" | "success";
  children: ReactNode;
  onClose?: () => void;
}) {
  const { t } = useI18n();
  return (
    <div className={cx("alert", `alert--${tone}`)} role="alert">
      <div className="alert-body">{children}</div>
      {onClose ? (
        <button
          type="button"
          className="alert-close"
          onClick={onClose}
          aria-label={t("dismiss")}
        >
          ×
        </button>
      ) : null}
    </div>
  );
}

export function Modal({
  open,
  title,
  subtitle,
  onClose,
  children,
  footer,
  wide,
}: {
  open: boolean;
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="modal-overlay" role="presentation" onMouseDown={onClose}>
      <div
        className={cx("modal", wide && "modal--wide")}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <div>
            <h2>{title}</h2>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="close"
          >
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer ? <footer className="modal-foot">{footer}</footer> : null}
      </div>
    </div>
  );
}

export function LocaleSwitch() {
  const { locale, setLocale, t } = useI18n();
  return (
    <div className="locale-switch" role="group" aria-label={t("language")}>
      <button
        type="button"
        className={cx(locale === "fa" && "is-active")}
        onClick={() => setLocale("fa")}
      >
        {t("persian")}
      </button>
      <button
        type="button"
        className={cx(locale === "en" && "is-active")}
        onClick={() => setLocale("en")}
      >
        {t("english")}
      </button>
    </div>
  );
}

export function BrandMark({ small }: { small?: boolean }) {
  return (
    <span className={cx("brand-mark", small && "brand-mark--small")} aria-hidden="true">
      <svg viewBox="0 0 32 32" width="100%" height="100%" fill="none">
        <path
          d="M6 9.5 16 4l10 5.5v13L16 28 6 22.5v-13Z"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
        <path
          d="M11 12.5 16 22l5-9.5"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className={cx("field", error && "field--invalid")}>
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {error ? (
        <span className="field-error">{error}</span>
      ) : hint ? (
        <span className="field-hint">{hint}</span>
      ) : null}
    </div>
  );
}
