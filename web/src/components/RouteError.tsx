import { Link, useRouteError } from "react-router-dom";

/** 画面の表示中にエラーが起きたとき（真っ白・英語のエラー表示の代わり）。 */
export default function RouteError() {
  const error = useRouteError() as Error | undefined;
  return (
    <div className="mx-auto max-w-lg px-4 py-16 text-center">
      <div className="text-xl font-extrabold">画面を表示できませんでした</div>
      <p className="mt-2 text-sm text-ink-sub">
        時間をおいて開き直してください。続く場合は管理者にお知らせください。
      </p>
      {error?.message && <p className="mt-3 text-xs text-ink-faint">{error.message}</p>}
      <div className="mt-6 flex justify-center gap-2">
        <button type="button" className="rounded-full bg-gray-100 px-4 py-2 text-sm font-bold" onClick={() => window.location.reload()}>
          開き直す
        </button>
        <Link to="/" className="rounded-full bg-line px-4 py-2 text-sm font-bold text-white">ホームへ</Link>
      </div>
    </div>
  );
}
