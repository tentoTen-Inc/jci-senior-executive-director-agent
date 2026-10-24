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

    // ---- 委員会の議案（理事・五役）----
    async function proposals() {
      document.title = "委員会の議案";
      let view = await api("GET", "/liff/api/proposals");
      bar.hidden = true;
      const draw = (flash) => {
        const kids = [...header("委員会の議案")];
        const f = flashNode(flash); if (f) kids.push(f);
        if (!view.proposals.length) kids.push(el("div", { class: "card" }, "見られる委員会の議案はまだありません。"));
        for (const p of view.proposals) {
          const feedback = p.feedback.length ? el("details", { class: "hint" },
            el("summary", {}, `審議の記録（${p.feedback.length}件）`),
            ...p.feedback.map((x) => el("div", { class: "fb" }, `${x.meeting}・${x.kind}${x.person ? `（${x.person}）` : ""}: ${x.text}`))) : null;
          const work = p.can_work && view.work_enabled ? el("button", {
            type: "button", class: "primary",
            onclick: async (e) => {
              e.target.disabled = true;
              try {
                await api("POST", `/liff/api/proposals/${encodeURIComponent(p.proposal_id)}/work`);
                draw({ ok: true, text: `「${p.title}」の作業を始めました。トークに戻って、直したい内容を送ってください。` });
                if (liff.isInClient()) setTimeout(() => liff.closeWindow(), 1500);
              } catch (err) { draw({ ok: false, text: err.message }); }
            },
          }, "この資料で作業する") : null;
          kids.push(el("div", { class: "card" },
            el("div", { class: "meta" }, `${p.committee}｜${p.stage}${p.event_date ? `｜実施 ${p.event_date}` : ""}`),
            el("h2", {}, p.title),
            p.deadlines.length ? el("div", { class: "hint" }, p.deadlines.map((d) => `${d.label}〆 ${d.at}`).join("　")) : null,
            feedback,
            el("div", { class: "actions" },
              p.url ? el("a", { href: p.url, target: "_blank", rel: "noopener" }, "議案書を開く") : null,
              work)));
        }
        app.replaceChildren(...kids);
      };
      draw();
    }

    // ---- 出欠の状況（五役・事務局）----
    async function attendance() {
      document.title = "出欠の状況";
      let view = await api("GET", "/liff/api/ops/attendance");
      bar.hidden = true;
      const draw = (flash) => {
        const kids = [...header("出欠の状況")];
        const f = flashNode(flash); if (f) kids.push(f);
        if (!view.events.length) kids.push(el("div", { class: "card" }, "出欠を受け付けている予定はありません。"));
        for (const ev of view.events) {
          const counts = Object.entries(ev.counts).map(([k, v]) => `${k} ${v}`).join("／");
          const n = ev.unanswered.length;
          const remind = n ? el("button", {
            type: "button", class: "primary", disabled: ev.reminded,
            onclick: async (e) => {
              if (!confirm(`「${ev.title}」の未回答の ${n} 人に、出欠のお願いを LINE の個別トークで送ります。よろしいですか？`)) return;
              e.target.disabled = true;
              try {
                const res = await api("POST", `/liff/api/ops/events/${encodeURIComponent(ev.event_id)}/remind`);
                view = res;
                const skipped = res.blocked + res.failed;
                draw({ ok: true, text: `${res.sent} 人に催促を送りました。${skipped ? `（${skipped} 人は送れませんでした）` : ""}` });
              } catch (err) { draw({ ok: false, text: err.message }); }
            },
          }, ev.reminded ? "催促を送りました" : `未回答者に催促（${n}人）`) : null;
          kids.push(el("div", { class: "card" },
            el("h2", {}, ev.title),
            el("div", { class: "meta" }, [ev.when, ev.location].filter(Boolean).join("　")),
            el("div", { class: "hint" }, `対象 ${ev.targets} 人　${counts}`),
            n ? el("div", { class: "hint" }, `未回答: ${ev.unanswered.join("、")}`) : el("div", { class: "hint" }, "全員が回答しています。"),
            remind ? el("div", { class: "actions" }, remind) : null));
        }
        app.replaceChildren(...kids);
      };
      draw();
    }

    // ---- 日程調整を作る（五役・事務局）----
    async function pollNew() {
      document.title = "日程調整を作る";
      const form = await api("GET", "/liff/api/ops/poll-form");
      bar.hidden = true;
      const d = { title: "", target: form.targets[0].value, event_type: form.targets[0].event_type, committee: "",
                  location: "", deadline: "", candidates: [{ date: "", start: "19:00", end: "" }] };
      // 送り先の説明（確認画面と同じ言い方）
      const destText = (x) => x.mode === "individual"
        ? `LINE 連携済みの ${x.linked} 人に個別に送ります（対象 ${x.targets} 人。委員会のグループが見つからないため）`
        : `LINE グループ「${x.group_name}」`;
      let created = null;
      const input = (attrs, key, obj = d) => el("input", { ...attrs, value: obj[key], oninput: (e) => { obj[key] = e.target.value; } });
      const draw = (flash) => {
        const kids = [...header("日程調整を作る")];
        const f = flashNode(flash); if (f) kids.push(f);
        if (created) {
          kids.push(el("div", { class: "card" },
            el("h2", {}, created.title),
            el("div", { class: "hint" }, `対象 ${created.targets} 人　案内先: ${destText(created)}`),
            ...created.candidates.map((c) => el("div", { class: "meta" }, `・${c}`)),
            created.announced ? el("div", { class: "hint" }, "案内しました。") : el("div", { class: "actions" }, el("button", {
              type: "button", class: "primary",
              onclick: async (e) => {
                if (!confirm(`${destText(created)}に日程調整の案内を送ります。送信は取り消せません。よろしいですか？`)) return;
                e.target.disabled = true;
                try {
                  await api("POST", `/liff/api/ops/polls/${encodeURIComponent(created.poll_id)}/announce`);
                  created.announced = true;
                  draw({ ok: true, text: created.mode === "individual" ? `${created.linked} 人に個別に案内しました。` : `「${created.group_name}」に案内しました。` });
                } catch (err) { draw({ ok: false, text: err.message }); e.target.disabled = false; }
              },
            }, created.mode === "individual" ? `${created.linked} 人に個別に案内する` : `「${created.group_name}」に案内する`))));
          app.replaceChildren(...kids);
          return;
        }
        const target = form.targets.find((t) => t.value === d.target);
        const committee = form.committees.find((c) => c.name === d.committee);
        const dest = d.target !== "committee"
          ? (target.group_name ? `案内先: ${target.group_name}` : "案内先のグループが見つかりません（管理画面から作成してください）")
          : committee ? `案内先: ${destText(committee)}` : "委員会を選んでください。";
        kids.push(el("div", { class: "card form" },
          el("label", {}, "1. 会議の名前", input({ type: "text", placeholder: "第3回五役会" }, "title")),
          el("div", { class: "group" }, "対象者",
            el("div", { class: "choices wide" }, ...form.targets.map((t) => el("button", {
              type: "button", class: "ok", "aria-pressed": String(d.target === t.value),
              onclick: () => { d.target = t.value; d.event_type = t.event_type; draw(); },
            }, t.label)))),
          d.target === "committee" ? el("label", {}, "委員会", el("select", { onchange: (e) => { d.committee = e.target.value; draw(); } },
            el("option", { value: "", selected: !d.committee }, "選んでください"),
            ...form.committees.map((c) => el("option", { value: c.name, selected: c.name === d.committee }, c.name)))) : null,
          el("div", { class: "hint" }, dest),
          el("label", {}, "種別", el("select", { onchange: (e) => { d.event_type = e.target.value; } },
            ...form.event_types.map((t) => el("option", { value: t, selected: t === d.event_type }, t)))),
          el("label", {}, "場所（任意）", input({ type: "text", placeholder: "JC会館 or オンライン" }, "location"))));
        kids.push(el("div", { class: "card form" },
          el("h2", {}, "2. 候補日時"),
          ...d.candidates.map((c, i) => el("div", { class: "row" },
            input({ type: "date" }, "date", c), input({ type: "time" }, "start", c),
            el("span", {}, "〜"), input({ type: "time" }, "end", c),
            d.candidates.length > 1 ? el("button", { type: "button", class: "x", onclick: () => { d.candidates.splice(i, 1); draw(); } }, "×") : null)),
          d.candidates.length < 10 ? el("div", { class: "actions" }, el("button", {
            type: "button", onclick: () => { d.candidates.push({ date: "", start: "19:00", end: "" }); draw(); },
          }, "＋候補を足す")) : null,
          el("label", {}, "回答の締切（任意）", input({ type: "datetime-local" }, "deadline"))));
        kids.push(el("div", { class: "actions" }, el("button", {
          type: "button", class: "primary",
          onclick: async (e) => {
            const cands = d.candidates.filter((c) => c.date);
            if (!d.title.trim()) return draw({ ok: false, text: "会議の名前を入力してください。" });
            if (!cands.length || cands.some((c) => !c.start)) return draw({ ok: false, text: "候補日時（日付と開始時刻）を入力してください。" });
            if (d.target === "committee" && !d.committee) return draw({ ok: false, text: "委員会を選んでください。" });
            e.target.disabled = true;
            try {
              created = await api("POST", "/liff/api/ops/polls", {
                title: d.title, target: d.target, committee: d.committee, event_type: d.event_type, location: d.location,
                deadline: d.deadline || null,
                candidates: cands.map((c) => ({ start: `${c.date}T${c.start}`, end: c.end ? `${c.date}T${c.end}` : null })),
              });
              draw({ ok: true, text: "作成しました。内容と送り先を確かめて、案内してください（まだ送っていません）。" });
            } catch (err) { draw({ ok: false, text: err.message }); }
          },
        }, "作成する（まだ案内しません）")));
        app.replaceChildren(...kids);
      };
      draw();
    }

    // ---- 承認待ち（五役・事務局）----
    async function approvals() {
      document.title = "承認待ち";
      let view = await api("GET", "/liff/api/ops/approvals");
      bar.hidden = true;
      const decide = (r, action) => async (e) => {
        const who = `${r.member_name || "（会員未選択）"} ← ${r.display_name || "LINE名なし"}`;
        if (!confirm(`${who} の LINE 連携を${action === "approve" ? "承認" : "却下"}します。よろしいですか？`)) return;
        e.target.disabled = true;
        try {
          view = await api("POST", `/liff/api/ops/link-requests/${encodeURIComponent(r.line_user_id)}/${action}`);
          draw({ ok: true, text: view.message });
        } catch (err) { draw({ ok: false, text: err.message }); }
      };
      const draw = (flash) => {
        const kids = [...header("承認待ち")];
        const f = flashNode(flash); if (f) kids.push(f);
        const reqs = el("div", { class: "card" }, el("h2", {}, `LINE 連携の申請（${view.requests.length}件）`));
        if (!view.requests.length) reqs.append(el("div", { class: "meta" }, "ありません。"));
        for (const r of view.requests) {
          reqs.append(el("div", { class: "cand" },
            el("div", {}, el("div", { class: "label" }, r.member_name || "（会員未選択）"), el("div", { class: "count" }, `LINE名: ${r.display_name || "なし"}`)),
            view.can_decide && r.member_name ? el("div", { class: "actions" },
              el("button", { type: "button", onclick: decide(r, "reject") }, "却下"),
              el("button", { type: "button", class: "primary", onclick: decide(r, "approve") }, "承認")) : null));
        }
        if (!view.can_decide && view.requests.length) reqs.append(el("div", { class: "hint" }, "承認・却下は専務理事が行います。"));
        const review = el("div", { class: "card" }, el("h2", {}, `専務確認待ちの議案（${view.review.length}件）`));
        if (!view.review.length) review.append(el("div", { class: "meta" }, "ありません。"));
        for (const p of view.review) {
          review.append(el("div", { class: "cand" },
            el("div", {}, el("div", { class: "label" }, p.title), el("div", { class: "count" }, p.committee || "")),
            p.url ? el("div", { class: "actions" }, el("a", { href: p.url, target: "_blank", rel: "noopener" }, "開く")) : null));
        }
        review.append(el("div", { class: "hint" }, "議案の確認結果は管理画面から登録します。"));
        kids.push(reqs, review);
        app.replaceChildren(...kids);
      };
      draw();
    }

    return { events, polls, proposals, attendance, "poll-new": pollNew, approvals };
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
