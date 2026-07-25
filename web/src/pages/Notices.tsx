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

type DeliverResult = {
  targets: number;
  sent: number;
  blocked: number;
  deferred: number;
  failed: number;
};
type NoticeAction = {
  action_id: string;
  title: string;
  due: string | null;
  assignees: string[];
  done_by: string[];
  status: string;
  reminder_count: number;
  reminded_at: string | null;
};

export default function Notices() {
  const [items, setItems] = useState<Notice[]>([]);
  const [tab, setTab] = useState("");
  const [sel, setSel] = useState<Notice | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ subject: "", from_name: "", body_text: "" });
  const [committees, setCommittees] = useState<string[]>([]);
  const [scopeKind, setScopeKind] = useState("all");
  const [scopeValue, setScopeValue] = useState("");
  const [draft, setDraft] = useState("");
  const [actions, setActions] = useState<NoticeAction[]>([]);

  const load = (status: string) =>
    api<Notice[]>(`/notices${status ? `?status=${status}` : ""}`)
      .then(setItems)
      .catch((e) => setMsg(e.message));
  useEffect(() => {
    load(tab);
  }, [tab]);

  useEffect(() => {
    api<{ committee: string | null }[]>("/members")
      .then((ms) =>
        setCommittees([...new Set(ms.map((m) => m.committee).filter((c): c is string => !!c))])
      )
      .catch(() => {});
  }, []);

  // 選択した連絡が変わったら、告知文のドラフトを（生成済みなら）読み込む
  useEffect(() => {
    setDraft(sel?.digest?.announcement ?? "");
  }, [sel?.notice_id, sel?.digest?.announcement]);

  const loadActions = (id: string) =>
    api<NoticeAction[]>(`/notices/${id}/actions`)
      .then(setActions)
      .catch(() => setActions([]));
  useEffect(() => {
    if (sel) loadActions(sel.notice_id);
    else setActions([]);
  }, [sel?.notice_id]);

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

  function scopePayload() {
    return {
      kind: scopeKind,
      value: scopeKind === "all" ? [] : scopeValue.split(",").map((s) => s.trim()),
    };
  }

  async function createActions(notice: Notice) {
    setBusy(true);
    try {
      const list = await api<NoticeAction[]>(`/notices/${notice.notice_id}/actions`, {
        method: "POST",
        body: JSON.stringify({ target_scope: scopePayload() }),
      });
      setActions(list);
      setMsg(`対応タスクを${list.length}件に更新しました。`);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remind(notice: Notice, action: NoticeAction) {
    const pending = action.assignees.length - action.done_by.length;
    if (!confirm(`未対応の${pending}名へ催促を送ります。よろしいですか？`)) return;
    setBusy(true);
    try {
      const res = await api<DeliverResult & { pending: number }>(
        `/notices/${notice.notice_id}/actions/${action.action_id}/remind`,
        { method: "POST" }
      );
      setMsg(
        `催促しました: 未対応${res.pending}名 / 送信${res.sent} 保留${res.deferred} ブロック${res.blocked} 失敗${res.failed}`
      );
      loadActions(notice.notice_id);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function deliver(notice: Notice, force: boolean) {
    const scopeLabel = scopeKind === "all" ? "全員" : `${scopeKind}: ${scopeValue}`;
    if (!confirm(`この告知文を「${scopeLabel}」へLINE配信します。よろしいですか？`)) return;
    setBusy(true);
    try {
      const res = await api<DeliverResult & { notice: Notice }>(
        `/notices/${notice.notice_id}/deliver`,
        {
          method: "POST",
          body: JSON.stringify({ target_scope: scopePayload(), body_text: draft, force }),
        }
      );
      setSel(res.notice);
      setMsg(
        `配信しました: 対象${res.targets}名 / 送信${res.sent} 保留${res.deferred} ブロック${res.blocked} 失敗${res.failed}`
      );
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
                  <div className="mt-2 text-xs text-slate-500">
                    告知文（LINE配信用・送信前に編集できます）
                  </div>
                  <textarea
                    className="border rounded p-2 w-full text-sm h-24 bg-white"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                  <div className="text-xs text-slate-400">
                    ※ AIの助言です。配信対象と文面は専務理事が確認してください。
                  </div>

                  <div className="mt-3 pt-3 border-t">
                    <div className="text-xs text-slate-500 mb-1">配信対象</div>
                    <div className="flex flex-wrap gap-2 items-center">
                      <select
                        className="border rounded p-1 text-sm"
                        value={scopeKind}
                        onChange={(e) => {
                          setScopeKind(e.target.value);
                          setScopeValue("");
                        }}
                      >
                        <option value="all">全員</option>
                        <option value="committee">委員会</option>
                        <option value="officer">役職</option>
                        <option value="custom">会員ID指定</option>
                      </select>
                      {scopeKind === "committee" && (
                        <select
                          className="border rounded p-1 text-sm"
                          value={scopeValue}
                          onChange={(e) => setScopeValue(e.target.value)}
                        >
                          <option value="">選択してください</option>
                          {committees.map((c) => (
                            <option key={c} value={c}>
                              {c}
                            </option>
                          ))}
                        </select>
                      )}
                      {(scopeKind === "officer" || scopeKind === "custom") && (
                        <input
                          className="border rounded p-1 text-sm w-56"
                          placeholder={
                            scopeKind === "officer" ? "例: 理事長, 専務理事" : "例: m1, m2"
                          }
                          value={scopeValue}
                          onChange={(e) => setScopeValue(e.target.value)}
                        />
                      )}
                      <button
                        className="bg-brand text-white rounded px-3 py-1 text-xs"
                        disabled={busy || !draft.trim()}
                        onClick={() => deliver(sel, sel.status === "delivered")}
                      >
                        {sel.status === "delivered" ? "再配信する" : "この内容でLINE配信"}
                      </button>
                    </div>
                    {sel.status === "delivered" && (
                      <p className="text-xs text-green-700 mt-1">
                        配信済みです（再配信すると同じ対象に再送されます）。
                      </p>
                    )}
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

          {/* 対応状況の追跡（F5-5） */}
          <div className="mt-4 pt-3 border-t">
            <div className="flex items-center gap-2 mb-2">
              <span className="font-semibold text-navy text-sm">対応タスク</span>
              <button
                className="bg-slate-200 text-navy rounded px-2 py-1 text-xs"
                disabled={busy}
                onClick={() => createActions(sel)}
              >
                {actions.length ? "AI抽出のアクションを反映" : "アクションをタスク化"}
              </button>
              <span className="text-xs text-slate-400">
                （対象は上で選んだ範囲。会員はLINEの「対応しました」で完了できます）
              </span>
            </div>
            {actions.length === 0 ? (
              <p className="text-sm text-slate-500">タスクはまだありません。</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-slate-500">
                    <th className="py-1">やること</th>
                    <th>期限</th>
                    <th>対応</th>
                    <th>催促</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {actions.map((a) => {
                    const done = a.done_by.length;
                    const total = a.assignees.length;
                    return (
                      <tr key={a.action_id} className="border-t">
                        <td className="py-1">{a.title}</td>
                        <td className="text-slate-500">{jdate(a.due)}</td>
                        <td>
                          <span
                            className={
                              done === total ? "text-green-700" : "text-amber-700 font-medium"
                            }
                          >
                            {done}/{total}
                          </span>
                        </td>
                        <td className="text-slate-500">
                          {a.reminder_count > 0 ? `${a.reminder_count}回` : "-"}
                        </td>
                        <td>
                          {done < total && (
                            <button
                              className="bg-slate-200 text-navy rounded px-2 py-0.5 text-xs"
                              disabled={busy}
                              onClick={() => remind(sel, a)}
                            >
                              未対応者に催促
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
