import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { Badge, Button, EmptyState, Panel, fmtDateTime, useUi } from "./ui";

// LINE 連携の申請（LIFF）と、日程調整の回答から分かった連携候補（docs/line-link-design.md）

export type LinkItem = {
  line_user_id: string;
  display_name: string | null;
  member_id: string;
  member_name: string | null;
  requested_at?: string | null;
  source?: string;
};
export type LinkData = { requests: LinkItem[]; candidates: LinkItem[]; liff_url: string | null };

export function LinkRequests({
  onLinked,
  onData,
  reloadKey,
  className,
}: {
  /** 承認・却下のあと（名簿を読み直すため） */
  onLinked?: () => void;
  /** 読み込んだ内容を親にも渡す（名簿の「承認待ち」表示に使う） */
  onData?: (d: LinkData) => void;
  /** 値が変わると読み直す */
  reloadKey?: number;
  className?: string;
}) {
  const { confirm, toast } = useUi();
  const [data, setData] = useState<LinkData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api<LinkData>("/line-link")
        .then((d) => {
          setData(d);
          setErr(null);
          onData?.(d);
        })
        .catch((e) => setErr((e as Error).message)),
    [onData]
  );
  useEffect(() => {
    load();
  }, [load, reloadKey]);

  async function act(item: LinkItem, action: "approve" | "reject") {
    const approve = action === "approve";
    const ok = await confirm(
      approve
        ? {
            title: `${item.member_name ?? "会員"} さんの LINE 連携を承認しますか？`,
            body: (
              <>
                LINE の表示名「{item.display_name ?? "（なし）"}」が本人であることを確かめてください。
                承認すると、本人に連携完了のお知らせが届きます。
              </>
            ),
            confirmLabel: "承認する",
          }
        : {
            title: `${item.member_name ?? "会員"} さんへの申請を却下しますか？`,
            body: (
              <>
                LINE の表示名「{item.display_name ?? "（なし）"}」からの申請です。本人でないと思われるときだけ却下してください。
              </>
            ),
            confirmLabel: "却下する",
            danger: true,
          }
    );
    if (!ok) return;
    setBusy(item.line_user_id);
    try {
      await api(`/line-link/${item.line_user_id}/${action}`, {
        method: "POST",
        body: approve ? JSON.stringify({ member_id: item.member_id }) : undefined,
      });
      toast(approve ? `${item.member_name} さんの LINE 連携を承認しました` : `${item.member_name} さんへの申請を却下しました`);
      await load();
      onLinked?.();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  }

  const rows = data
    ? [
        ...data.requests.map((r) => ({ ...r, kind: "request" as const })),
        ...data.candidates.map((r) => ({ ...r, kind: "candidate" as const })),
      ]
    : [];

  return (
    <Panel
      className={className}
      title={
        <span className="flex items-center gap-2">
          LINE 連携の承認待ち
          {rows.length > 0 && <Badge tone="green">{rows.length}件</Badge>}
        </span>
      }
      flush
    >
      <p className="px-5 pb-3 text-xs text-ink-sub">
        会員が LINE で自分の名前を選んで申請したものと、日程調整で名前を選んで回答したものです。LINE の表示名を見て、本人なら承認してください。
      </p>
      {err ? (
        <div className="px-5 pb-3 text-sm text-danger">{err}</div>
      ) : !data ? (
        <div className="px-5 pb-4 text-sm text-ink-sub">読み込み中…</div>
      ) : rows.length === 0 ? (
        <div className="px-5 pb-3">
          <EmptyState title="承認待ちはありません">新しい申請が届くと、こことホームに出ます。</EmptyState>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="tbl">
            <thead>
              <tr>
                <th>会員</th>
                <th>LINE の表示名</th>
                <th>きっかけ</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.line_user_id}>
                  <td className="whitespace-nowrap font-bold">{r.member_name ?? "（名簿にない会員）"}</td>
                  <td>{r.display_name ?? "（なし）"}</td>
                  <td className="text-xs text-ink-sub">
                    {r.kind === "request" ? (
                      <>
                        <Badge tone="green">申請</Badge>
                        {r.requested_at && <span className="num ml-2">{fmtDateTime(r.requested_at)}</span>}
                      </>
                    ) : (
                      r.source ?? "日程調整の回答"
                    )}
                  </td>
                  <td className="whitespace-nowrap text-right">
                    <div className="inline-flex gap-1.5">
                      <Button size="sm" variant="primary" disabled={busy === r.line_user_id} onClick={() => act(r, "approve")}>
                        承認
                      </Button>
                      {r.kind === "request" && (
                        <Button size="sm" variant="danger" disabled={busy === r.line_user_id} onClick={() => act(r, "reject")}>
                          却下
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
