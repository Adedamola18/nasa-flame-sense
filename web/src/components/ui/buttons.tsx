"use client";

import { useTranslations } from "next-intl";
import type { ButtonHTMLAttributes, ReactNode } from "react";

/*
 * The whole button set. Six kinds, square corners, no shadows.
 * Ember is reserved for the primary action and for fire.
 */

const focus = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-flare";

const variants = {
  primary: "bg-ember text-on-ember hover:bg-ember-hover px-4",
  secondary: "border border-line hover:border-mute px-4",
  quiet: "px-0.5 text-mute hover:text-ink",
} as const;

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof variants };

export function Button({ variant = "secondary", className = "", type = "button", ...props }: ButtonProps) {
  return (
    <button
      type={type}
      className={`inline-flex h-9 cursor-pointer items-center gap-2 text-[13px] font-medium whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focus} ${variants[variant]} ${className}`}
      {...props}
    />
  );
}

export function ListenToggle({ playing, onToggle }: { playing: boolean; onToggle: () => void }) {
  const t = useTranslations("Listen");
  return (
    <button
      type="button"
      aria-pressed={playing}
      onClick={onToggle}
      className={`inline-flex h-9 cursor-pointer items-center gap-2 border bg-night/80 pr-3 pl-2.5 text-[13px] font-medium transition-colors ${focus} ${
        playing ? "border-ember text-ember" : "border-line hover:border-mute"
      }`}
    >
      <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
        <path d={playing ? "M0 0h3.5v12H0zM6.5 0H10v12H6.5z" : "M0 0l10 6-10 6z"} fill="currentColor" />
      </svg>
      {playing ? t("pause") : t("listen")}
    </button>
  );
}

export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex gap-0.5 border border-line p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
          className={`h-[30px] cursor-pointer px-2 font-data text-xs font-medium tracking-wide sm:px-[11px] ${focus} ${
            o.value === value ? "bg-line text-ink" : "text-mute hover:text-ink"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Chip({
  pressed,
  onToggle,
  color = "var(--color-ember)",
  children,
}: {
  pressed: boolean;
  onToggle: () => void;
  color?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onToggle}
      className={`inline-flex h-7 cursor-pointer items-center gap-1.5 border px-2.5 text-xs ${focus} ${
        pressed ? "border-mute text-ink" : "border-line text-mute"
      }`}
    >
      <span className="size-[7px] rounded-full" style={{ background: color }} />
      {children}
    </button>
  );
}
