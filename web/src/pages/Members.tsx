import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { LinkRequests, type LinkData } from "../components/LinkRequests";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  Field,
  Loading,
  PageHeader,
  Panel,
  Progress,
  Segmented,
  fmtDateTime,
  useUi,
} from "../components/ui";

// 会員・LINE 連携（docs/admin-ui-design.md §3・docs/line-link-design.md）

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
type Seat = { committee: string; role: string | null };
type FullMember = {
  member_id: string;
  name: string;
  kana: string | null;
  status: string;
  officer_role: string | null;
  committee: string | null;
  committee_role: string | null;
  committees: Seat[];
};
type Contact = { mobile: string | null; email: string | null; [k: string]: string | null };
type MemberDetail = {
  member_id: string;
  name: string;
  kana: string | null;
  committee: string | null;
  committee_role: string | null;
  committees: Seat[];
  secondments: { org: string; role: string }[];
  officer_role: string | null;
  member_type: string;
  status: string;
  contact: Contact;
  line_user_id: string | null;
};
type History = {
  name: string;
  counted: number;
  present: number;
  answered: number;
  attendance_rate: number;
  items: { event_id: string; title: string; datetime_start: string; status: string }[];
};
type Group = { group_id: string; group_name: string | null; joined_at?: string | null; left_at?: string | null };
type Post = {
  post_id: string;
  group_id: string;
  kind: string;
  poll_id: string | null;
  send_at: string;
  status: "pending" | "sent" | "failed" | "cancelled";
  sent_at: string | null;
  created_at: string | null;
};
type Invite = { code: string; expires_at: string };

const MEMBER_TYPES = [
  { key: "regular", label: "正会員" },
  { key: "external_auditor", label: "外部監事" },
  { key: "office", label: "事務局" },
  { key: "ob", label: "OB" },
  { key: "support", label: "賛助会員" },
  { key: "youth_support", label: "青年賛助会員" },
];
const typeLabel = (k: string) => MEMBER_TYPES.find((t) => t.key === k)?.label ?? "その他";
const GOYAKU_ROLES = ["理事長", "副理事長", "専務理事", "監事"];
const POST_KIND: Record<string, string> = { link_invite: "LINE 連携のお願い", poll_announce: "日程調整の案内" };
const DEFAULT_GROUP = "【JCI猪苗代】";

const PRESENT_TONE: Record<string, "green" | "gray" | "amber" | "blue"> = {
  出席: "green",
  Web出席: "green",
  欠席: "gray",
  委任: "blue",
  未回答: "amber",
};

// ---------------------------------------------------------------- 小さな道具
const pad = (n: number) => String(n).padStart(2, "0");
function localInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
/** 送信時刻の初期値: 21時〜8時なら次の8:00、それ以外は5分後 */
function defaultSendAt(): string {
  const d = new Date();
  const h = d.getHours();
  if (h >= 21 || h < 8) {
    if (h >= 21) d.setDate(d.getDate() + 1);
    d.setHours(8, 0, 0, 0);
  } else {
    d.setMinutes(d.getMinutes() + 5, 0, 0);
  }
  return localInput(d);
}
const norm = (s: string) => s.replace(/[\s　]/g, "");
function findGroup(groups: Group[], name: string): string {
  const n = norm(name);
  const hit =
    groups.find((g) => g.group_name === name) ??
    groups.find((g) => g.group_name && norm(g.group_name) === n) ??
    groups.find((g) => g.group_name && norm(g.group_name).includes(n));
  return hit?.group_id ?? "";
}
function isActiveGroup(g: Group): boolean {
  if (!g.left_at) return true;
  return !!g.joined_at && new Date(g.joined_at) > new Date(g.left_at);
}
const groupLabel = (g: Group | undefined) => g?.group_name ?? "（名前を取得できていないグループ）";

function seats(m: FullMember | undefined, row: Row): Seat[] {
  if (m?.committees?.length) return m.committees;
  if (m?.committee) return [{ committee: m.committee, role: m.committee_role }];
  return row.committee ? row.committee.split("・").map((c) => ({ committee: c, role: null })) : [];
}
const isChair = (s: Seat[]) => s.some((x) => x.role === "委員長" || x.role === "副委員長");

function CopyLink({ label, url, note }: { label: string; url: string; note?: ReactNode }) {
  const { toast } = useUi();
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      toast("リンクをコピーしました");
    } catch {
      toast("コピーできませんでした。「開く」から開いてアドレスをコピーしてください", "error");
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-2xl bg-gray-50 px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-bold">{label}</div>
        {note && <div className="text-xs text-ink-sub">{note}</div>}
      </div>
      <div className="flex gap-1.5">
        <Button size="sm" onClick={copy}>リンクをコピー</Button>
        <a href={url} target="_blank" rel="noreferrer">
          <Button size="sm" variant="ghost">開く</Button>
        </a>
      </div>
    </div>
  );
}

function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        className="h-full w-full max-w-xl overflow-y-auto bg-canvas p-4 sm:p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-2">
          <h2 className="text-xl font-extrabold">{title}</h2>
          <Button size="sm" onClick={onClose}>閉じる</Button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- LINE 連携のまとめ・グループへのお願い
function LinkSummary({
  rows,
  pendingCount,
  liffUrl,
}: {
  rows: Row[];
  pendingCount: number;
  liffUrl: string | null;
}) {
  const { confirm, toast } = useUi();
  const [groups, setGroups] = useState<Group[]>([]);
  const [posts, setPosts] = useState<Post[]>([]);
  const [composing, setComposing] = useState(false);
  const [groupId, setGroupId] = useState("");
  const [sendAt, setSendAt] = useState(defaultSendAt());
  const [busy, setBusy] = useState(false);

  const loadPosts = useCallback(() => api<Post[]>("/scheduled-posts").then(setPosts).catch(() => {}), []);
  useEffect(() => {
    loadPosts();
    api<Group[]>("/line/groups")
      .then((gs) => {
        const active = gs.filter(isActiveGroup);
        setGroups(active);
        setGroupId((cur) => cur || findGroup(active, DEFAULT_GROUP));
      })
      .catch(() => {});
  }, [loadPosts]);

  const linked = rows.filter((r) => r.linked).length;
  const unlinked = rows.length - linked;
  const gName = (id: string) => groupLabel(groups.find((g) => g.group_id === id));
  const pending = posts.filter((p) => p.status === "pending").sort((a, b) => a.send_at.localeCompare(b.send_at));
  const recent = posts
    .filter((p) => p.status === "sent" || p.status === "failed")
    .sort((a, b) => (b.sent_at ?? b.send_at).localeCompare(a.sent_at ?? a.send_at))
    .slice(0, 3);

  function openComposer() {
    setSendAt(defaultSendAt());
    setComposing(true);
  }

  async function schedule() {
    if (!groupId) return toast("送り先のグループを選んでください", "error");
    if (!sendAt) return toast("送る日時を入れてください", "error");
    const name = gName(groupId);
    const ok = await confirm({
      title: `「${name}」に LINE 連携のお願いを送りますか？`,
      body: (
        <>
          送り先: <b className="text-ink">「{name}」</b>
          <br />
          送る日時: {fmtDateTime(sendAt)}（毎時の自動処理で送るため、少し遅れることがあります。送るのは 8時〜21時 です）
          <br />
          いま未連携の会員は {unlinked} 人です。グループの全員にボタン付きのお願いが届きます。送ったあとは取り消せません。
        </>
      ),
      confirmLabel: `「${name}」に送る`,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api("/scheduled-posts", {
        method: "POST",
        body: JSON.stringify({ group_id: groupId, kind: "link_invite", send_at: sendAt }),
      });
      toast(`「${name}」への送信を ${fmtDateTime(sendAt)} に予約しました`);
      setComposing(false);
      loadPosts();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function cancel(p: Post) {
    const ok = await confirm({
      title: "この予約を取り消しますか？",
      body: <>「{gName(p.group_id)}」への{POST_KIND[p.kind] ?? "投稿"}（{fmtDateTime(p.send_at)} 送信予定）</>,
      confirmLabel: "予約を取り消す",
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/scheduled-posts/${p.post_id}/cancel`, { method: "POST" });
      toast("予約を取り消しました");
      loadPosts();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  return (
    <Panel title="LINE 連携" className="lg:col-span-2">
      <div className="flex items-end justify-between gap-3">
        <div>
          <div className="text-xs text-ink-sub">連携済み</div>
          <div className="num text-3xl font-extrabold">
            {linked}
            <span className="text-sm font-bold">/{rows.length}人</span>
          </div>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          {pendingCount > 0 && <Badge tone="amber">承認待ち {pendingCount}件</Badge>}
          {unlinked > 0 ? <Badge>未連携 {unlinked}人</Badge> : <Badge tone="green">全員連携済み</Badge>}
        </div>
      </div>
      <div className="mt-2">
        <Progress value={linked} max={rows.length} />
      </div>

      <ol className="mt-4 space-y-1 text-sm text-ink-sub">
        <li><b className="text-ink">1.</b> グループに「LINE 連携のお願い」を送る</li>
        <li><b className="text-ink">2.</b> 会員がボタンを押して、自分の名前を選んで申請する</li>
        <li><b className="text-ink">3.</b> 「承認待ち」の欄で本人か確かめて承認する</li>
      </ol>

      <div className="mt-4 space-y-3">
        {!composing ? (
          <Button variant="primary" className="w-full sm:w-auto" onClick={openComposer} disabled={!liffUrl}>
            グループに連携のお願いを送る
          </Button>
        ) : (
          <div className="space-y-3 rounded-2xl bg-gray-50 p-4">
            <Field label="送り先の LINE グループ">
              <select className="w-full" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
                <option value="">選んでください</option>
                {groups.map((g) => <option key={g.group_id} value={g.group_id}>{groupLabel(g)}</option>)}
              </select>
            </Field>
            <Field label="送る日時" hint="夜（21時〜8時）は送らないので、その時間なら朝8時にしてあります。">
              <input type="datetime-local" className="w-full" value={sendAt} onChange={(e) => setSendAt(e.target.value)} />
            </Field>
            <div className="flex justify-end gap-2">
              <Button onClick={() => setComposing(false)}>やめる</Button>
              <Button variant="primary" disabled={busy} onClick={schedule}>送信を予約する</Button>
            </div>
          </div>
        )}
        {!liffUrl && (
          <div className="text-xs text-danger">LINE で申請する画面の準備ができていないため、まだお願いを送れません。</div>
        )}
        {liffUrl && (
          <CopyLink label="申請画面のリンク" url={liffUrl}
            note="個別に頼むときは、このリンクを LINE で送ってください。" />
        )}
      </div>

      {(pending.length > 0 || recent.length > 0) && (
        <div className="mt-4 border-t border-gray-100 pt-3">
          <div className="mb-1 text-xs font-bold text-ink-sub">予約した投稿</div>
          <ul className="divide-y divide-gray-100 text-sm">
            {pending.map((p) => (
              <li key={p.post_id} className="flex flex-wrap items-center gap-2 py-2">
                <Badge tone="blue">送信待ち</Badge>
                <span className="min-w-0 flex-1">
                  <span className="font-bold">{POST_KIND[p.kind] ?? "投稿"}</span>
                  <span className="text-ink-sub"> → 「{gName(p.group_id)}」</span>
                  <span className="num block text-xs text-ink-sub">{fmtDateTime(p.send_at)} に送ります</span>
                </span>
                <Button size="sm" variant="danger" onClick={() => cancel(p)}>取り消す</Button>
              </li>
            ))}
            {recent.map((p) => (
              <li key={p.post_id} className="flex flex-wrap items-center gap-2 py-2">
                {p.status === "sent" ? <Badge tone="green">送信済み</Badge> : <Badge tone="red">送れませんでした</Badge>}
                <span className="min-w-0 flex-1">
                  <span className="font-bold">{POST_KIND[p.kind] ?? "投稿"}</span>
                  <span className="text-ink-sub"> → 「{gName(p.group_id)}」</span>
                  <span className="num block text-xs text-ink-sub">{fmtDateTime(p.sent_at ?? p.send_at)}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- 会員の詳細（編集・出欠履歴・招待コード）
type Tab = "edit" | "history" | "invite";

function MemberDrawer({
  row,
  tab,
  onTab,
  onClose,
  onSaved,
}: {
  row: Row;
  tab: Tab;
  onTab: (t: Tab) => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { confirm, toast } = useUi();
  const [edit, setEdit] = useState<MemberDetail | null>(null);
  const [origStatus, setOrigStatus] = useState<string | null>(null);
  const [hist, setHist] = useState<History | null>(null);
  const [invite, setInvite] = useState<Invite | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setEdit(null);
    setHist(null);
    setInvite(null);
    setErr(null);
    api<MemberDetail>(`/members/${row.member_id}`)
      .then((m) => {
        setEdit(m);
        setOrigStatus(m.status);
      })
      .catch((e) => setErr((e as Error).message));
  }, [row.member_id]);

  useEffect(() => {
    if (tab === "history" && !hist)
      api<History>(`/members/${row.member_id}/attendance-history`).then(setHist).catch((e) => setErr((e as Error).message));
  }, [tab, hist, row.member_id]);

  async function save() {
    if (!edit) return;
    if (!edit.name.trim()) return toast("氏名を入力してください", "error");
    if (edit.status === "inactive" && origStatus !== "inactive") {
      const ok = await confirm({
        title: `${edit.name} さんを「退会・休会」にしますか？`,
        body: "名簿の一覧から外れ、出欠のお願いなども届かなくなります。あとで在籍に戻すこともできます。",
        confirmLabel: "退会・休会にする",
        danger: true,
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      await api(`/members/${edit.member_id}`, {
        method: "PUT",
        body: JSON.stringify({
          name: edit.name.trim(),
          kana: edit.kana,
          committee: edit.committee,
          committee_role: edit.committee_role,
          officer_role: edit.officer_role,
          member_type: edit.member_type,
          status: edit.status,
          contact: edit.contact,
        }),
      });
      toast(`${edit.name} さんの情報を保存しました`);
      setOrigStatus(edit.status);
      onSaved();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function issue() {
    if (row.invite_active) {
      const ok = await confirm({
        title: "新しい招待コードを発行しますか？",
        body: "まだ使われていない有効なコードがあります。新しく発行しても前のコードは期限まで使えます。",
        confirmLabel: "発行する",
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      const r = await api<Invite>(`/members/${row.member_id}/invite`, { method: "POST" });
      setInvite(r);
      toast("招待コードを発行しました");
      onSaved();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function copyCode(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      toast("コードをコピーしました");
    } catch {
      toast("コピーできませんでした", "error");
    }
  }

  const set = (patch: Partial<MemberDetail>) => edit && setEdit({ ...edit, ...patch });
  const setContact = (k: string, v: string) => edit && setEdit({ ...edit, contact: { ...edit.contact, [k]: v || null } });

  return (
    <Drawer title={`${row.name} さん`} onClose={onClose}>
      <div className="mb-4">
        <Segmented value={tab} onChange={onTab}
          options={[
            { value: "edit", label: "情報の編集" },
            { value: "history", label: "出欠履歴" },
            { value: "invite", label: "招待コード" },
          ]} />
      </div>
      {err && <ErrorNote>{err}</ErrorNote>}

      {tab === "edit" && (!edit ? <Loading /> : (
        <Panel>
          <div className="pt-3">
            {(edit.committees?.length > 1 || edit.secondments?.length > 0) && (
              <div className="mb-4 rounded-2xl bg-gray-50 px-4 py-3 text-xs text-ink-sub">
                {edit.committees?.length > 1 && (
                  <div>
                    兼務: {edit.committees.map((c) => `${c.committee}${c.role ? `（${c.role}）` : ""}`).join("、")}
                    <div className="text-ink-faint">下の「所属委員会」は主な所属です。兼務は組織図の取り込みで変わります。</div>
                  </div>
                )}
                {edit.secondments?.length > 0 && (
                  <div className="mt-1">出向: {edit.secondments.map((x) => `${x.org} ${x.role}`).join("、")}</div>
                )}
              </div>
            )}
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="氏名">
                <input className="w-full" value={edit.name} onChange={(e) => set({ name: e.target.value })} />
              </Field>
              <Field label="ふりがな">
                <input className="w-full" value={edit.kana ?? ""} onChange={(e) => set({ kana: e.target.value || null })} />
              </Field>
              <Field label="役職" hint="理事長・副理事長・専務理事・監事 など">
                <input className="w-full" value={edit.officer_role ?? ""} onChange={(e) => set({ officer_role: e.target.value || null })} />
              </Field>
              <Field label="会員区分">
                <select className="w-full" value={edit.member_type} onChange={(e) => set({ member_type: e.target.value })}>
                  {MEMBER_TYPES.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
                </select>
              </Field>
              <Field label="所属委員会（主な所属）">
                <input className="w-full" value={edit.committee ?? ""} onChange={(e) => set({ committee: e.target.value || null })} />
              </Field>
              <Field label="委員会での役割" hint="委員長・副委員長・委員">
                <input className="w-full" value={edit.committee_role ?? ""} onChange={(e) => set({ committee_role: e.target.value || null })} />
              </Field>
              <Field label="携帯電話">
                <input className="w-full" inputMode="tel" value={edit.contact.mobile ?? ""} onChange={(e) => setContact("mobile", e.target.value)} />
              </Field>
              <Field label="メール">
                <input className="w-full" type="email" value={edit.contact.email ?? ""} onChange={(e) => setContact("email", e.target.value)} />
              </Field>
              <Field label="在籍">
                <select className="w-full" value={edit.status} onChange={(e) => set({ status: e.target.value })}>
                  <option value="active">在籍</option>
                  <option value="inactive">退会・休会</option>
                </select>
              </Field>
              <div>
                <span className="field-label">LINE 連携</span>
                <div className="py-2 text-sm">
                  {edit.line_user_id ? <Badge tone="green">連携済み</Badge> : <Badge>未連携</Badge>}
                  <span className="ml-2 text-xs text-ink-faint">ここでは変えられません</span>
                </div>
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button onClick={onClose}>閉じる</Button>
              <Button variant="primary" disabled={busy} onClick={save}>保存する</Button>
            </div>
          </div>
        </Panel>
      ))}

      {tab === "history" && (!hist ? <Loading /> : (
        <Panel flush>
          <div className="grid grid-cols-3 gap-3 px-5 pb-4 pt-4">
            <div>
              <div className="text-xs text-ink-sub">出席率</div>
              <div className="num text-3xl font-extrabold">{Math.round(hist.attendance_rate * 100)}<span className="text-sm font-bold">%</span></div>
            </div>
            <div>
              <div className="text-xs text-ink-sub">出席</div>
              <div className="num text-xl font-extrabold">{hist.present}<span className="text-sm font-bold">/{hist.counted}回</span></div>
            </div>
            <div>
              <div className="text-xs text-ink-sub">回答した数</div>
              <div className="num text-xl font-extrabold">{hist.answered}<span className="text-sm font-bold">回</span></div>
            </div>
          </div>
          {hist.items.length === 0 ? (
            <div className="px-5 pb-3"><EmptyState title="対象になった予定はまだありません" /></div>
          ) : (
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead><tr><th>日付</th><th>予定</th><th>出欠</th></tr></thead>
                <tbody>
                  {hist.items.map((it) => (
                    <tr key={it.event_id}>
                      <td className="num whitespace-nowrap text-ink-sub">{fmtDateTime(it.datetime_start, false)}</td>
                      <td>{it.title}</td>
                      <td><Badge tone={PRESENT_TONE[it.status] ?? "gray"}>{it.status}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      ))}

      {tab === "invite" && (
        <Panel>
          <div className="space-y-3 pt-3 text-sm">
            {row.linked ? (
              <EmptyState title="LINE 連携済みです">招待コードは必要ありません。</EmptyState>
            ) : (
              <>
                <p className="text-ink-sub">
                  ふだんはグループの「LINE 連携のお願い」から申請してもらいます。うまく申請できない方にだけ使ってください。
                  コードを本人に伝え、猪苗代専務AI を友だち追加した個別トークに送ってもらうと連携されます（1回限り・30日間有効）。
                </p>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-ink-sub">いまの状態:</span>
                  {row.invite_used ? <Badge tone="green">使用済み</Badge>
                    : row.invite_active ? <Badge tone="blue">有効なコードあり</Badge>
                      : row.invite_issued ? <Badge>期限切れ</Badge>
                        : <Badge>未発行</Badge>}
                </div>
                {invite && (
                  <div className="rounded-2xl bg-line-soft px-4 py-4">
                    <div className="text-xs text-line-ink">招待コード</div>
                    <div className="num mt-1 flex flex-wrap items-center gap-3">
                      <span className="font-mono text-2xl font-extrabold tracking-[0.2em]">{invite.code}</span>
                      <Button size="sm" onClick={() => copyCode(invite.code)}>コピー</Button>
                    </div>
                    <div className="mt-1 text-xs text-line-ink">有効期限 {fmtDateTime(invite.expires_at)}</div>
                  </div>
                )}
                <div className="flex justify-end">
                  <Button disabled={busy} onClick={issue}>
                    {row.invite_active || invite ? "新しいコードを発行する" : "招待コードを発行する"}
                  </Button>
                </div>
              </>
            )}
          </div>
        </Panel>
      )}
    </Drawer>
  );
}

// ---------------------------------------------------------------- 会員の追加
function AddMember({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const { toast } = useUi();
  const [m, setM] = useState({ name: "", kana: "", committee: "", officer_role: "", member_type: "regular" });
  const [busy, setBusy] = useState(false);

  async function add() {
    if (!m.name.trim()) return toast("氏名を入力してください", "error");
    setBusy(true);
    try {
      const r = await api<{ name: string }>("/members/new", {
        method: "POST",
        body: JSON.stringify({
          name: m.name.trim(),
          kana: m.kana.trim() || null,
          committee: m.committee.trim() || null,
          officer_role: m.officer_role.trim() || null,
          member_type: m.member_type,
        }),
      });
      toast(`${r.name} さんを追加しました。LINE 連携はグループのお願いから申請してもらえます`);
      onAdded();
      onClose();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="会員を追加" action={<Button size="sm" variant="ghost" onClick={onClose}>やめる</Button>}>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Field label="氏名（必須）">
          <input className="w-full" value={m.name} onChange={(e) => setM({ ...m, name: e.target.value })} />
        </Field>
        <Field label="ふりがな">
          <input className="w-full" value={m.kana} onChange={(e) => setM({ ...m, kana: e.target.value })} />
        </Field>
        <Field label="所属委員会">
          <input className="w-full" value={m.committee} onChange={(e) => setM({ ...m, committee: e.target.value })} />
        </Field>
        <Field label="役職">
          <input className="w-full" placeholder="なければ空欄" value={m.officer_role} onChange={(e) => setM({ ...m, officer_role: e.target.value })} />
        </Field>
        <Field label="会員区分">
          <select className="w-full" value={m.member_type} onChange={(e) => setM({ ...m, member_type: e.target.value })}>
            {MEMBER_TYPES.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
          </select>
        </Field>
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>やめる</Button>
        <Button variant="primary" disabled={busy} onClick={add}>追加する</Button>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- 画面
type LineFilter = "all" | "linked" | "unlinked" | "pending";
type RoleFilter = "all" | "goyaku" | "riji" | "chair" | "none";

export default function Members() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [full, setFull] = useState<Record<string, FullMember>>({});
  const [link, setLink] = useState<LinkData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<{ id: string; tab: Tab } | null>(null);
  const [linkReload, setLinkReload] = useState(0);

  const [q, setQ] = useState("");
  const [role, setRole] = useState<RoleFilter>("all");
  const [committee, setCommittee] = useState("");
  const [line, setLine] = useState<LineFilter>("all");

  const load = useCallback(() => {
    api<Row[]>("/members/invite-status").then(setRows).catch((e) => setErr((e as Error).message));
    api<FullMember[]>("/members")
      .then((ms) => setFull(Object.fromEntries(ms.map((m) => [m.member_id, m]))))
      .catch(() => {});
  }, []);
  useEffect(load, [load]);

  const pendingIds = useMemo(
    () => new Set([...(link?.requests ?? []), ...(link?.candidates ?? [])].map((r) => r.member_id)),
    [link]
  );
  const pendingCount = (link?.requests.length ?? 0) + (link?.candidates.length ?? 0);

  const committees = useMemo(() => {
    const set = new Set<string>();
    for (const r of rows ?? []) for (const s of seats(full[r.member_id], r)) set.add(s.committee);
    return [...set].sort((a, b) => a.localeCompare(b, "ja"));
  }, [rows, full]);

  const lineState = (r: Row): Exclude<LineFilter, "all"> =>
    r.linked ? "linked" : pendingIds.has(r.member_id) ? "pending" : "unlinked";

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!rows) return <Loading />;

  const counts = {
    linked: rows.filter((r) => lineState(r) === "linked").length,
    unlinked: rows.filter((r) => lineState(r) === "unlinked").length,
    pending: rows.filter((r) => lineState(r) === "pending").length,
  };
  const query = norm(q).toLowerCase();
  const shown = rows.filter((r) => {
    const m = full[r.member_id];
    const s = seats(m, r);
    if (query && !norm(`${r.name}${m?.kana ?? ""}`).toLowerCase().includes(query)) return false;
    if (role === "goyaku" && !GOYAKU_ROLES.includes(r.officer_role ?? "")) return false;
    if (role === "riji" && !(GOYAKU_ROLES.includes(r.officer_role ?? "") || isChair(s))) return false;
    if (role === "chair" && !isChair(s)) return false;
    if (role === "none" && (r.officer_role || isChair(s))) return false;
    if (committee && !s.some((x) => x.committee === committee)) return false;
    if (line !== "all" && lineState(r) !== line) return false;
    return true;
  });
  const filtered = !!(query || role !== "all" || committee || line !== "all");
  const openRow = open ? rows.find((r) => r.member_id === open.id) : undefined;

  return (
    <div>
      <PageHeader
        title="会員・LINE 連携"
        description="名簿の確認・編集と、会員の LINE 連携の承認をします。"
        actions={!adding && <Button onClick={() => setAdding(true)}>＋ 会員を追加</Button>}
      />

      <div className="grid gap-4 lg:grid-cols-5">
        <LinkSummary rows={rows} pendingCount={pendingCount} liffUrl={link?.liff_url ?? null} />
        <div className="min-w-0 lg:col-span-3">
          <LinkRequests onData={setLink} reloadKey={linkReload} onLinked={load} />
        </div>
      </div>

      {adding && <AddMember onClose={() => setAdding(false)} onAdded={load} />}

      <Panel title={`名簿（${rows.length}人）`} flush>
        <div className="flex flex-wrap items-end gap-3 px-5 pb-3">
          <Field label="名前で探す" className="w-full sm:w-48">
            <input className="w-full" type="search" placeholder="名前・ふりがな" value={q} onChange={(e) => setQ(e.target.value)} />
          </Field>
          <Field label="役職" className="w-[calc(50%-0.375rem)] sm:w-44">
            <select className="w-full" value={role} onChange={(e) => setRole(e.target.value as RoleFilter)}>
              <option value="all">すべて</option>
              <option value="goyaku">五役</option>
              <option value="riji">理事（五役＋委員長・副委員長）</option>
              <option value="chair">委員長・副委員長</option>
              <option value="none">役職なし</option>
            </select>
          </Field>
          <Field label="委員会" className="w-[calc(50%-0.375rem)] sm:w-44">
            <select className="w-full" value={committee} onChange={(e) => setCommittee(e.target.value)}>
              <option value="">すべて</option>
              {committees.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </Field>
          <div>
            <span className="field-label">LINE 連携</span>
            <Segmented value={line} onChange={setLine}
              options={[
                { value: "all", label: "すべて" },
                { value: "linked", label: `連携済み ${counts.linked}` },
                { value: "pending", label: `承認待ち ${counts.pending}` },
                { value: "unlinked", label: `未連携 ${counts.unlinked}` },
              ]} />
          </div>
        </div>

        {shown.length === 0 ? (
          <div className="px-5 pb-3">
            <EmptyState title={filtered ? "条件に合う会員がいません" : "会員が登録されていません"}
              action={filtered ? (
                <Button size="sm" onClick={() => { setQ(""); setRole("all"); setCommittee(""); setLine("all"); }}>条件を外す</Button>
              ) : (
                <Button size="sm" onClick={() => setAdding(true)}>会員を追加</Button>
              )} />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>氏名</th>
                  <th>役職・委員会</th>
                  <th>LINE 連携</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => {
                  const m = full[r.member_id];
                  const s = seats(m, r);
                  const st = lineState(r);
                  return (
                    <tr key={r.member_id}>
                      <td className="whitespace-nowrap">
                        <button type="button" className="font-bold hover:underline" onClick={() => setOpen({ id: r.member_id, tab: "edit" })}>
                          {r.name}
                        </button>
                        {m?.kana && <div className="text-xs text-ink-faint">{m.kana}</div>}
                        {r.member_type !== "regular" && <div className="text-xs text-ink-sub">{typeLabel(r.member_type)}</div>}
                      </td>
                      <td>
                        {r.officer_role && <div className="font-bold">{r.officer_role}</div>}
                        <div className="text-xs text-ink-sub">
                          {s.length ? s.map((x) => `${x.committee}${x.role ? `（${x.role}）` : ""}`).join("・") : r.officer_role ? "" : "—"}
                        </div>
                      </td>
                      <td className="whitespace-nowrap">
                        {st === "linked" ? <Badge tone="green">連携済み</Badge>
                          : st === "pending" ? <Badge tone="amber">承認待ち</Badge>
                            : <Badge>未連携</Badge>}
                      </td>
                      <td className="whitespace-nowrap text-right">
                        <div className="inline-flex gap-0.5">
                          <Button size="sm" variant="ghost" onClick={() => setOpen({ id: r.member_id, tab: "edit" })}>編集</Button>
                          <Button size="sm" variant="ghost" onClick={() => setOpen({ id: r.member_id, tab: "history" })}>出欠履歴</Button>
                          {!r.linked && (
                            <Button size="sm" variant="ghost" className="!text-ink-sub" onClick={() => setOpen({ id: r.member_id, tab: "invite" })}>
                              招待コード
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {open && openRow && (
        <MemberDrawer
          row={openRow}
          tab={open.tab}
          onTab={(tab) => setOpen({ ...open, tab })}
          onClose={() => setOpen(null)}
          onSaved={() => {
            load();
            setLinkReload((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}
