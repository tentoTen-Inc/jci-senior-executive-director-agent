import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "../components/Card";

type Row = {
  member_id: string;
  name: string;
  committee: string | null;
  officer_role: string | null;
  member_type: string;
  linked: boolean;
  invite_issued: boolean;
  invite_active: boolean;
  invite_used: boolean;
};

type Contact = {
  mobile: string | null;
  email: string | null;
  home_tel: string | null;
  work: string | null;
};
type MemberDetail = {
  member_id: string;
  name: string;
  kana: string | null;
  committee: string | null;
  committee_role: string | null;
  officer_role: string | null;
  member_type: string;
  status: string;
  contact: Contact;
  line_user_id: string | null;
};

const MEMBER_TYPES = [
  { key: "regular", label: "正会員" },
  { key: "external_auditor", label: "外部監事" },
  { key: "office", label: "事務局" },
  { key: "ob", label: "OB" },
  { key: "support", label: "賛助会員" },
];

type History = {
  name: string;
  counted: number;
  present: number;
  answered: number;
  attendance_rate: number;
  items: { event_id: string; title: string; datetime_start: string; status: string }[];
};

function Badge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`text-xs px-2 py-0.5 rounded-full ${
        ok ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-500"
      }`}
    >
      {label}
    </span>
  );
}

export default function Members() {
  const [rows, setRows] = useState<Row[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [hist, setHist] = useState<History | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [edit, setEdit] = useState<MemberDetail | null>(null);
  const [adding, setAdding] = useState(false);
  const [newMember, setNewMember] = useState({ name: "", kana: "", committee: "" });

  const load = () =>
    api<Row[]>("/members/invite-status").then(setRows).catch((e) => setErr(e.message));

  useEffect(() => {
    load();
  }, []);

  async function issueInvite(id: string) {
    try {
      const r = await api<{ code: string }>(`/members/${id}/invite`, { method: "POST" });
      setMsg(`招待コードを発行: ${r.code}`);
      load();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function openEdit(id: string) {
    try {
      setEdit(await api<MemberDetail>(`/members/${id}`));
      setHist(null);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function saveEdit() {
    if (!edit) return;
    try {
      await api(`/members/${edit.member_id}`, {
        method: "PUT",
        body: JSON.stringify({
          name: edit.name,
          kana: edit.kana,
          committee: edit.committee,
          committee_role: edit.committee_role,
          officer_role: edit.officer_role,
          member_type: edit.member_type,
          status: edit.status,
          contact: edit.contact,
        }),
      });
      setMsg(`${edit.name} を更新しました。`);
      setEdit(null);
      load();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function addMember() {
    if (!newMember.name.trim()) {
      setMsg("氏名を入力してください。");
      return;
    }
    try {
      const m = await api<MemberDetail>("/members/new", {
        method: "POST",
        body: JSON.stringify({
          name: newMember.name.trim(),
          kana: newMember.kana.trim() || null,
          committee: newMember.committee.trim() || null,
        }),
      });
      setMsg(`${m.name} を追加しました。招待コードを発行して連携してください。`);
      setNewMember({ name: "", kana: "", committee: "" });
      setAdding(false);
      load();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function showHistory(id: string) {
    try {
      setHist(await api<History>(`/members/${id}/attendance-history`));
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  if (err) return <Card title="会員管理"><p className="text-red-600 text-sm">{err}</p></Card>;

  return (
    <div>
      <h1 className="text-lg font-semibold text-navy mb-3">会員管理</h1>
      {msg && <div className="mb-2 text-sm text-brand">{msg}</div>}

      <Card>
        {adding ? (
          <div className="flex flex-wrap gap-2 items-center text-sm mb-3 pb-3 border-b">
            <input
              className="border rounded p-1 w-40"
              placeholder="氏名"
              value={newMember.name}
              onChange={(e) => setNewMember({ ...newMember, name: e.target.value })}
            />
            <input
              className="border rounded p-1 w-40"
              placeholder="ふりがな"
              value={newMember.kana}
              onChange={(e) => setNewMember({ ...newMember, kana: e.target.value })}
            />
            <input
              className="border rounded p-1 w-40"
              placeholder="所属委員会"
              value={newMember.committee}
              onChange={(e) => setNewMember({ ...newMember, committee: e.target.value })}
            />
            <button className="bg-brand text-white rounded px-3 py-1" onClick={addMember}>
              追加
            </button>
            <button className="text-slate-500 underline text-xs" onClick={() => setAdding(false)}>
              やめる
            </button>
          </div>
        ) : (
          <button
            className="bg-slate-200 text-navy rounded px-3 py-1 text-sm mb-3"
            onClick={() => setAdding(true)}
          >
            会員を追加
          </button>
        )}
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500">
              <th className="py-1">氏名</th>
              <th>委員会/役職</th>
              <th>LINE連携</th>
              <th>招待コード</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.member_id} className="border-t">
                <td className="py-1">{r.name}</td>
                <td className="text-slate-500">{r.officer_role || r.committee || "-"}</td>
                <td><Badge ok={r.linked} label={r.linked ? "連携済" : "未連携"} /></td>
                <td>
                  {r.invite_used ? (
                    <Badge ok label="使用済" />
                  ) : r.invite_active ? (
                    <Badge ok label="発行済(有効)" />
                  ) : (
                    <Badge ok={false} label="未発行" />
                  )}
                </td>
                <td className="space-x-2 whitespace-nowrap">
                  {!r.linked && (
                    <button
                      className="text-xs bg-brand text-white rounded px-2 py-1"
                      onClick={() => issueInvite(r.member_id)}
                    >
                      招待発行
                    </button>
                  )}
                  <button
                    className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
                    onClick={() => openEdit(r.member_id)}
                  >
                    編集
                  </button>
                  <button
                    className="text-xs bg-slate-200 text-navy rounded px-2 py-1"
                    onClick={() => showHistory(r.member_id)}
                  >
                    出欠履歴
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="text-slate-500 text-sm">会員がいません。</p>}
      </Card>

      {edit && (
        <Card title={`会員を編集: ${edit.name}`}>
          <div className="grid gap-2 md:grid-cols-2 text-sm">
            <label className="block">
              <span className="text-xs text-slate-500">氏名</span>
              <input
                className="border rounded p-1 w-full"
                value={edit.name}
                onChange={(e) => setEdit({ ...edit, name: e.target.value })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">ふりがな</span>
              <input
                className="border rounded p-1 w-full"
                value={edit.kana ?? ""}
                onChange={(e) => setEdit({ ...edit, kana: e.target.value || null })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">所属委員会</span>
              <input
                className="border rounded p-1 w-full"
                value={edit.committee ?? ""}
                onChange={(e) => setEdit({ ...edit, committee: e.target.value || null })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">委員会内の役割</span>
              <input
                className="border rounded p-1 w-full"
                placeholder="委員長/副委員長/委員"
                value={edit.committee_role ?? ""}
                onChange={(e) => setEdit({ ...edit, committee_role: e.target.value || null })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">役職（五役等）</span>
              <input
                className="border rounded p-1 w-full"
                placeholder="理事長/専務理事 等"
                value={edit.officer_role ?? ""}
                onChange={(e) => setEdit({ ...edit, officer_role: e.target.value || null })}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">会員区分</span>
              <select
                className="border rounded p-1 w-full"
                value={edit.member_type}
                onChange={(e) => setEdit({ ...edit, member_type: e.target.value })}
              >
                {MEMBER_TYPES.map((t) => (
                  <option key={t.key} value={t.key}>{t.label}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">在籍状態</span>
              <select
                className="border rounded p-1 w-full"
                value={edit.status}
                onChange={(e) => setEdit({ ...edit, status: e.target.value })}
              >
                <option value="active">在籍</option>
                <option value="inactive">退会/休会</option>
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">携帯</span>
              <input
                className="border rounded p-1 w-full"
                value={edit.contact.mobile ?? ""}
                onChange={(e) =>
                  setEdit({ ...edit, contact: { ...edit.contact, mobile: e.target.value || null } })
                }
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">メール</span>
              <input
                className="border rounded p-1 w-full"
                value={edit.contact.email ?? ""}
                onChange={(e) =>
                  setEdit({ ...edit, contact: { ...edit.contact, email: e.target.value || null } })
                }
              />
            </label>
          </div>
          <div className="mt-3 flex gap-2 items-center">
            <button className="bg-brand text-white rounded px-3 py-1 text-sm" onClick={saveEdit}>
              保存
            </button>
            <button className="text-slate-500 underline text-xs" onClick={() => setEdit(null)}>
              閉じる
            </button>
            <span className="text-xs text-slate-400">
              LINE連携: {edit.line_user_id ? "連携済（この画面では変更しません）" : "未連携"}
            </span>
          </div>
        </Card>
      )}

      {hist && (
        <Card title={`出欠履歴: ${hist.name}`}>
          <p className="text-sm mb-2">
            出席率 <b>{Math.round(hist.attendance_rate * 100)}%</b>（出席 {hist.present} /
            対象 {hist.counted}、回答 {hist.answered}）
            <button className="ml-3 text-xs text-slate-500 underline" onClick={() => setHist(null)}>
              閉じる
            </button>
          </p>
          <table className="w-full text-sm">
            <tbody>
              {hist.items.map((it) => (
                <tr key={it.event_id} className="border-t">
                  <td className="py-1">{it.datetime_start.slice(0, 10)}</td>
                  <td>{it.title}</td>
                  <td className="font-semibold">{it.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
