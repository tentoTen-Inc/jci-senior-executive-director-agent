// 共通の部品（docs/admin-ui-design.md §2・C案「LINE なじみ」）
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";

const cx = (...xs: (string | false | null | undefined)[]) => xs.filter(Boolean).join(" ");

// ---------------------------------------------------------------- ページ見出し
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-extrabold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-ink-sub">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- カード
export function Panel({
  title,
  action,
  children,
  className,
  flush,
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  /** 中身を端まで広げる（表など） */
  flush?: boolean;
}) {
  return (
    <section className={cx("mb-4 rounded-card bg-white", className)}>
      {(title || action) && (
        <div className="flex items-center justify-between gap-2 px-5 pb-2 pt-4">
          {title && <h2 className="text-base font-extrabold">{title}</h2>}
          {action && <div className="text-sm">{action}</div>}
        </div>
      )}
      <div className={flush ? "pb-2" : "px-5 pb-5 pt-1"}>{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------- ボタン
type Variant = "primary" | "secondary" | "ghost" | "danger";
const VARIANT: Record<Variant, string> = {
  primary: "bg-line text-white hover:bg-line-dark disabled:bg-line/40",
  secondary: "bg-gray-100 text-ink hover:bg-gray-200 disabled:text-ink-faint",
  ghost: "bg-transparent text-line-ink hover:bg-line-soft disabled:text-ink-faint",
  danger: "bg-transparent text-danger hover:bg-danger-soft disabled:text-ink-faint",
};

export function Button({
  variant = "secondary",
  size = "md",
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }) {
  return (
    <button
      type="button"
      className={cx(
        "inline-flex items-center justify-center gap-1 whitespace-nowrap rounded-full font-bold transition disabled:cursor-not-allowed",
        size === "sm" ? "px-3 py-1 text-xs" : "px-4 py-2 text-sm",
        VARIANT[variant],
        className
      )}
      {...rest}
    />
  );
}

// ---------------------------------------------------------------- タグ
type Tone = "green" | "gray" | "red" | "amber" | "blue";
const TONE: Record<Tone, string> = {
  green: "bg-line-soft text-line-ink",
  gray: "bg-gray-100 text-ink-sub",
  red: "bg-danger-soft text-danger",
  amber: "bg-hold-soft text-hold",
  blue: "bg-info-soft text-info",
};

export function Badge({ tone = "gray", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={cx("inline-block whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-bold", TONE[tone])}>
      {children}
    </span>
  );
}

// ---------------------------------------------------------------- 数字
export function Stat({
  label,
  value,
  unit,
  sub,
  to,
  highlight,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  sub?: ReactNode;
  to?: string;
  /** 対応が必要なときに緑で目立たせる */
  highlight?: boolean;
}) {
  const body = (
    <div
      className={cx(
        "h-full rounded-card px-5 py-4 transition",
        highlight ? "bg-line text-white" : "bg-white",
        to && (highlight ? "hover:bg-line-dark" : "hover:bg-gray-50")
      )}
    >
      <div className={cx("text-xs", highlight ? "text-white/85" : "text-ink-sub")}>{label}</div>
      <div className="num mt-1 text-3xl font-extrabold">
        {value}
        {unit && <span className="ml-0.5 text-sm font-bold">{unit}</span>}
      </div>
      {sub && <div className={cx("mt-1 text-xs", highlight ? "text-white/85" : "text-ink-sub")}>{sub}</div>}
    </div>
  );
  return to ? <Link to={to} className="block">{body}</Link> : body;
}

// ---------------------------------------------------------------- 進み具合
export function Progress({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100" aria-label={`${value}/${max}`}>
      <div className="h-full rounded-full bg-line" style={{ width: `${pct}%` }} />
    </div>
  );
}

// ---------------------------------------------------------------- 切替（タブ）
export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex flex-wrap gap-1.5" role="tablist">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={o.value === value}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded-full px-3.5 py-1.5 text-sm font-bold transition",
            o.value === value ? "bg-ink text-white" : "bg-gray-100 text-ink-sub hover:bg-gray-200"
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- 空のとき・読み込み中
export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-2xl bg-gray-50 px-5 py-8 text-center">
      <div className="font-bold">{title}</div>
      {children && <div className="mt-1 text-sm text-ink-sub">{children}</div>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function Loading({ label = "読み込み中…" }: { label?: string }) {
  return <div className="py-10 text-center text-sm text-ink-sub">{label}</div>;
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return <div className="mb-4 rounded-2xl bg-danger-soft px-4 py-3 text-sm text-danger">{children}</div>;
}

// ---------------------------------------------------------------- 入力欄（ラベル付き）
export function Field({ label, hint, children, className }: { label: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cx("block", className)}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-faint">{hint}</span>}
    </label>
  );
}

// ---------------------------------------------------------------- 日付の表示（日本時間）
const WEEK = "日月火水木金土";
export function fmtDateTime(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const base = `${d.getMonth() + 1}/${d.getDate()}(${WEEK[d.getDay()]})`;
  return withTime ? `${base} ${d.toTimeString().slice(0, 5)}` : base;
}

// ---------------------------------------------------------------- 確認ダイアログ・通知
type ConfirmOpts = { title: string; body?: ReactNode; confirmLabel?: string; danger?: boolean };
type ToastTone = "ok" | "error";
type Ctx = {
  confirm: (o: ConfirmOpts) => Promise<boolean>;
  toast: (message: string, tone?: ToastTone) => void;
};
const UiContext = createContext<Ctx | null>(null);

export function UiProvider({ children }: { children: ReactNode }) {
  const [dialog, setDialog] = useState<(ConfirmOpts & { resolve: (v: boolean) => void }) | null>(null);
  const [toasts, setToasts] = useState<{ id: number; message: string; tone: ToastTone }[]>([]);
  const seq = useRef(0);

  const confirm = useCallback(
    (o: ConfirmOpts) => new Promise<boolean>((resolve) => setDialog({ ...o, resolve })),
    []
  );
  const toast = useCallback((message: string, tone: ToastTone = "ok") => {
    const id = ++seq.current;
    setToasts((t) => [...t, { id, message, tone }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "error" ? 7000 : 3500);
  }, []);

  const close = (v: boolean) => {
    dialog?.resolve(v);
    setDialog(null);
  };
  useEffect(() => {
    if (!dialog) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <UiContext.Provider value={{ confirm, toast }}>
      {children}
      {dialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={() => close(false)}>
          <div
            role="dialog"
            aria-modal="true"
            className="w-full max-w-md rounded-card bg-white p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="text-lg font-extrabold">{dialog.title}</div>
            {dialog.body && <div className="mt-2 text-sm leading-relaxed text-ink-sub">{dialog.body}</div>}
            <div className="mt-6 flex justify-end gap-2">
              <Button onClick={() => close(false)}>やめる</Button>
              <Button
                variant={dialog.danger ? "secondary" : "primary"}
                className={dialog.danger ? "!bg-danger !text-white hover:!bg-danger/90" : ""}
                onClick={() => close(true)}
                autoFocus
              >
                {dialog.confirmLabel ?? "OK"}
              </Button>
            </div>
          </div>
        </div>
      )}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(92vw,380px)] flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={cx(
              "pointer-events-auto rounded-2xl px-4 py-3 text-sm font-bold text-white shadow-lg",
              t.tone === "error" ? "bg-danger" : "bg-ink"
            )}
          >
            {t.message}
          </div>
        ))}
      </div>
    </UiContext.Provider>
  );
}

export function useUi(): Ctx {
  const ctx = useContext(UiContext);
  if (!ctx) throw new Error("UiProvider がありません");
  return ctx;
}
