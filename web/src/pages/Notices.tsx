import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type Digest = {
  summary: string;
  announcement: string;
  audience_hint: string | null;
  deadline: string | null;
  actions: string[];
  model: string | null;
  generated_at: string | null;
};
type Attachment = { name: string; mime: string | null; text_excerpt: string | null };
type Notice = {
  notice_id: string;
  source: string;
  received_at: string;
  from_addr: string | null;
  from_name: string | null;
  subject: string;
  body_text: string;
  attachments: Attachment[];
  digest: Digest | null;
  status: string;
};

const TABS: { key: string; label: string }[] = [
  { key: "new", label: "新着" },
  { key: "reviewed", label: "要約済" },
  { key: "delivered", label: "配信済" },
  { key: "archived", label: "アーカイブ" },
  { key: "", label: "すべて" },
];

const STATUS_STYLE: Record<string, string> = {
  new: "bg-amber-100 text-amber-800",
  reviewed: "bg-blue-50 text-brand",
  delivered: "bg-green-50 text-green-700",
  archived: "bg-slate-100 text-slate-500",
};

function jdate(iso: string | null): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleDateString("ja-JP", { month: "numeric", day: "numeric" });
}

export default function Notices() {
  const [items, setItems] = useState<Notice[]>([]);
  const [tab, setTab] = useState("");
  const [sel, setSel] = useState<Notice | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ subject: "", from_name: "", body_text: "" });

  const load = (status: string) =>
    api<Notice[]>(`/notices${status ? `?status=${status}` : ""}`)
      .then(setItems)
      .catch((e) => setMsg(e.message));
  useEffect(() => {
    load(tab);
  }, [tab]);

  async function submit() {
    if (!form.subject.trim() || !form.body_text.trim()) {
      setMsg("件名と本文を入力してください。");
      return;
    }
    try {
      const created = await api<Notice>("/notices", {
        method: "POST",
        body: JSON.stringify(form),
      });
      setForm({ subject: "", from_name: "", body_text: "" });
      setSel(created);
      setMsg(`取込しました: ${created.subject}`);
      load(tab);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function act(id: string, path: string) {
    setBusy(true);
    try {
      const fresh = await api<Notice>(`/notices/${id}/${path}`, { method: "POST" });
      setSel(fresh);
      load(tab);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">対外連絡</h1>
      {msg && <div className="mb-2 text-sm text-brand">{msg}</div>}

      <Card title="メールを取込（本文を貼り付け）">
        <div className="flex flex-wrap gap-2 text-sm">
          <input
            className="border rounded p-1 flex-1 min-w-[220px]"
            placeholder="件名"
            value={form.subject}
            onChange={(e) => setForm({ ...form, subject: e.target.value })}
          />
          <input
            className="border rounded p-1 w-48"
            placeholder="差出人（例: ブロック協議会）"
            value={form.from_name}
            onChange={(e) => setForm({ ...form, from_name: e.target.value })}
          />
        </div>
        <textarea
          className="border rounded p-2 w-full mt-2 text-sm h-28"
          placeholder="メール本文をそのまま貼り付けてください（原文として保存されます）"
          value={form.body_text}
          onChange={(e) => setForm({ ...form, body_text: e.target.value })}
        />
        <div className="mt-2">
          <button className="bg-brand text-white rounded px-3 py-1 text-sm" onClick={submit}>
            取込
          </button>
          <span className="text-xs text-slate-400 ml-2">
            ※ 同じ内容を再度貼り付けても重複登録されません。Gmail自動取込は後続対応。
          </span>
        </div>
      </Card>

      <Card title="一覧">
        <div className="flex gap-2 mb-2 text-sm">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`px-2 py-1 rounded ${
                tab === t.key ? "bg-brand text-white" : "bg-slate-100 text-slate-600"
              }`}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500 text-xs">
              <th className="py-1">受信</th>
              <th>件名</th>
              <th>差出人</th>
              <th>期限</th>
              <th>状態</th>
            </tr>
          </thead>
          <tbody>
            {items.map((n) => (
              <tr
                key={n.notice_id}
                className="border-t hover:bg-slate-50 cursor-pointer"
                onClick={() => setSel(n)}
              >
                <td className="py-1 text-slate-500 whitespace-nowrap">{jdate(n.received_at)}</td>
                <td className="font-medium">{n.subject}</td>
                <td className="text-slate-500">{n.from_name || n.from_addr || "-"}</td>
                <td className="text-slate-500">{jdate(n.digest?.deadline ?? null)}</td>
                <td>
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs ${
                      STATUS_STYLE[n.status] ?? "bg-slate-100"
                    }`}
                  >
                    {n.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {items.length === 0 && <p className="text-slate-500 text-sm">該当する連絡はありません。</p>}
      </Card>

      {sel && (
        <Card title={`詳細: ${sel.subject}`}>
          <div className="flex flex-wrap gap-2 mb-3">
            <button
              className="bg-slate-200 text-navy rounded px-2 py-1 text-xs"
              disabled={busy}
              onClick={() => act(sel.notice_id, "digest")}
            >
              {sel.digest ? "要約を再生成" : "AIで要約・告知文を生成"}
            </button>
            <button
              className="bg-slate-200 text-navy rounded px-2 py-1 text-xs"
              disabled={busy}
              onClick={() => act(sel.notice_id, "archive")}
            >
              アーカイブ
            </button>
            <button className="text-slate-500 underline text-xs" onClick={() => setSel(null)}>
              閉じる
            </button>
          </div>

          {/* 生成物と原文を並置（誤要約対策・F5-6） */}
          <div className="grid gap-3 md:grid-cols-2 text-sm">
            <div className="bg-slate-50 rounded p-3">
              <div className="font-semibold text-navy mb-1">AI要約（助言）</div>
              {sel.digest ? (
                <>
                  <div>{sel.digest.summary}</div>
                  <div className="mt-2">
                    <span className="text-xs text-slate-500">対象(推定): </span>
                    {sel.digest.audience_hint || "不明"}
                    <span className="text-xs text-slate-500 ml-3">期限: </span>
                    {jdate(sel.digest.deadline)}
                  </div>
                  {sel.digest.actions.length > 0 && (
                    <>
                      <div className="mt-2 text-xs text-slate-500">必要なアクション</div>
                      <ul className="list-disc ml-5">
                        {sel.digest.actions.map((a, i) => (
                          <li key={i}>{a}</li>
                        ))}
                      </ul>
                    </>
                  )}
                  <div className="mt-2 text-xs text-slate-500">告知文（LINE配信用）</div>
                  <div className="bg-white rounded p-2 whitespace-pre-wrap">
                    {sel.digest.announcement}
                  </div>
                  <div className="text-xs text-slate-400 mt-2">
                    ※ AIの助言です。配信対象と文面は専務理事が確認してください。
                  </div>
                </>
              ) : (
                <p className="text-slate-500">未生成です。「AIで要約・告知文を生成」を押してください。</p>
              )}
            </div>

            <div className="rounded p-3 border">
              <div className="font-semibold text-navy mb-1">原文（出典）</div>
              <div className="text-xs text-slate-500">
                受信 {new Date(sel.received_at).toLocaleString("ja-JP")} / 差出人{" "}
                {sel.from_name || sel.from_addr || "-"} / 取込 {sel.source}
              </div>
              <div className="mt-2 whitespace-pre-wrap max-h-72 overflow-y-auto">
                {sel.body_text}
              </div>
              {sel.attachments.length > 0 && (
                <>
                  <div className="mt-2 text-xs text-slate-500">添付</div>
                  <ul className="list-disc ml-5">
                    {sel.attachments.map((a, i) => (
                      <li key={i}>{a.name}</li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
