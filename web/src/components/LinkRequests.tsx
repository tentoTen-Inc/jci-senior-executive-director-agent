import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card } from "./Card";

// LINE 連携の申請（LIFF）と、日程調整の回答から分かった連携候補（docs/line-link-design.md）

type Item = {
  line_user_id: string;
  display_name: string | null;
  member_id: string;
  member_name: string | null;
  requested_at?: string | null;
  source?: string;
};
type Data = { requests: Item[]; candidates: Item[]; liff_url: string | null };

export function LinkRequests({ onLinked }: { onLinked?: () => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = () => api<Data>("/line-link").then(setData).catch((e) => setMsg(e.message));
  useEffect(() => {
    load();
  }, []);

  async function act(item: Item, action: "approve" | "reject") {
    const label = action === "approve" ? "承認" : "却下";
    if (!window.confirm(`${item.member_name} さん ← LINE「${item.display_name ?? ""}」を${label}します。よろしいですか？`)) return;
    try {
      await api(`/line-link/${item.line_user_id}/${action}`, {
        method: "POST",
        body: action === "approve" ? JSON.stringify({ member_id: item.member_id }) : undefined,
      });
      setMsg(`${item.member_name} さんを${label}しました。`);
      await load();
      onLinked?.();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  if (!data) return null;
  const rows = [
    ...data.requests.map((r) => ({ ...r, kind: "申請" })),
    ...data.candidates.map((r) => ({ ...r, kind: r.source ?? "候補" })),
  ];
  return (
    <Card title={`LINE 連携の承認待ち（${rows.length}件）`}>
      {msg && <div className="text-sm mb-2 text-slate-700">{msg}</div>}
      <div className="text-xs text-slate-500 mb-2">
        会員が LINE で自分の名前を選んで申請したもの、日程調整で名前を選んで回答したものです。LINE 名を見て本人なら承認してください。
        {data.liff_url && <> 申請画面: <span className="font-mono">{data.liff_url}</span></>}
      </div>
      {rows.length === 0 ? (
        <div className="text-sm text-slate-500">承認待ちはありません。</div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500 border-b">
              <th className="py-1">会員</th><th>LINE の表示名</th><th>経路</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.line_user_id} className="border-b">
                <td className="py-1">{r.member_name}</td>
                <td>{r.display_name}</td>
                <td className="text-xs text-slate-500">{r.kind}</td>
                <td className="whitespace-nowrap text-right">
                  <button className="bg-brand text-white rounded px-2 py-0.5 mr-1" onClick={() => act(r, "approve")}>承認</button>
                  {r.kind === "申請" && (
                    <button className="border rounded px-2 py-0.5" onClick={() => act(r, "reject")}>却下</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
