// 管理API クライアント。IAP 配下では Cookie 認証のため credentials を含める。
// ローカル開発では Vite プロキシ経由で FastAPI に届く。

/** サーバーの日本語のエラー（{"detail": "..."}）をそのまま画面に出す。 */
async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string" && body.detail) return body.detail;
  } catch {
    /* JSON でなければ下の汎用文 */
  }
  if (res.status >= 500) return "サーバーで問題が起きました。時間をおいてやり直してください。";
  return `うまくいきませんでした（${res.status}）`;
}

export async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(opts.headers ?? {}) },
    ...opts,
  });
  if (res.status === 401) {
    throw new Error("ログインの有効期限が切れました。画面を開き直してください。");
  }
  if (!res.ok) {
    throw new Error(await errorMessage(res));
  }
  return res.json() as Promise<T>;
}
