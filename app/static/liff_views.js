// LIFF の画面（docs/liff-menu-design.md §3）。/liff/poll の ?view= で呼ばれる。
// 書き込みは［送信］系のボタンを押したときだけ。権限はサーバーが確かめる。
(() => {
  const STATUS_CLASS = { "出席": "ok", "Web出席": "maybe", "欠席": "ng" };

  function views(ctx) {
    const { el, app, bar, btn, fail } = ctx;

    async function api(method, path, body) {
      const res = await fetch(path, {
        method,
        headers: { "Authorization": `Bearer ${liff.getIDToken()}`, "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 401) { liff.logout(); liff.login({ redirectUri: location.href }); throw new Error("再ログインします…"); }
      if (!res.ok) throw new Error(data.detail || `エラーが発生しました（${res.status}）`);
      return data;
    }

    const header = (title, sub) => [el("div", { class: "meta" }, sub || "猪苗代専務AI"), el("h1", {}, title)];
    const flashNode = (flash) => flash ? el("div", { class: `toast ${flash.ok ? "ok" : "err"}` }, flash.text) : null;

    // ---- 予定と出欠（全員）----
    async function events() {
      document.title = "予定と出欠";
      let view = await api("GET", "/liff/api/me/events");
      let edits = {};
      const draw = (flash) => {
        const kids = [...header("予定と出欠", `${view.member_name} さん`)];
        const f = flashNode(flash); if (f) kids.push(f);
        if (!view.events.length) {
          kids.push(el("div", { class: "card" }, "これから回答が必要な予定はありません。"));
        }
        for (const ev of view.events) {
          const current = edits[ev.event_id] || ev.status;
          const choices = el("div", { class: "choices wide" }, ...view.answers.map((a) => el("button", {
            type: "button", class: STATUS_CLASS[a] || "", "aria-pressed": String(current === a),
            onclick: () => {
              if (a === ev.status) delete edits[ev.event_id]; else edits[ev.event_id] = a;
              draw();
            },
          }, a)));
          const meta = [ev.when, ev.location].filter(Boolean).join("　");
          kids.push(el("div", { class: "card" },
            el("h2", {}, ev.title),
            el("div", { class: "meta" }, meta),
            ev.deadline ? el("div", { class: "hint" }, `回答の締切: ${ev.deadline}`) : null,
            el("div", { class: "hint" }, `いまの回答: ${ev.status}${edits[ev.event_id] ? ` → ${edits[ev.event_id]}（未送信）` : ""}`),
            choices));
        }
        app.replaceChildren(...kids);
        const n = Object.keys(edits).length;
        bar.hidden = n === 0;
        btn.textContent = `この内容で回答する（${n}件）`;
      };
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          view = await api("PUT", "/liff/api/me/attendance", { answers: edits });
          edits = {};
          draw({ ok: true, text: "回答を送信しました。ありがとうございます。" });
        } catch (e) {
          draw({ ok: false, text: e.message });
        } finally { btn.disabled = false; }
      };
      draw();
    }

    // ---- 日程調整（全員）----
    async function polls() {
      document.title = "日程調整";
      const view = await api("GET", "/liff/api/me/polls");
      bar.hidden = true;
      const kids = [...header("日程調整")];
      if (!view.polls.length) kids.push(el("div", { class: "card" }, "いま回答をお願いしている日程調整はありません。"));
      for (const p of view.polls) {
        kids.push(el("div", { class: "card" },
          el("h2", {}, p.title),
          el("div", { class: "meta" }, [p.answered ? "回答済み" : "未回答", p.deadline ? `〆${p.deadline}` : ""].filter(Boolean).join("　")),
          el("div", { class: "actions" }, el("button", {
            type: "button", class: "secondary",
            onclick: () => { location.href = `${location.pathname}?poll=${encodeURIComponent(p.poll_id)}`; },
          }, p.answered ? "回答を見る・変更する" : "回答する"))));
      }
      app.replaceChildren(...kids);
    }

    return { events, polls };
  }

  window.LiffViews = {
    async run(name, ctx) {
      const table = views(ctx);
      const fn = table[name];
      if (!fn) { ctx.fail("この画面は開けません。メニューから開き直してください。"); return; }
      await fn();
    },
  };
})();
