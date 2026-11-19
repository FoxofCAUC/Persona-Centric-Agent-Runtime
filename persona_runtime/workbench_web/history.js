(() => {
  "use strict";

  const switchView = byId("sample-mode");
  const search = byId("session-search");
  const resetDemo = document.querySelector('[data-menu="reset"]');
  let browsing = true;
  let generation = 0;
  let people = [];
  let sessions = [];
  let person = null;
  let session = null;
  let conversation = null;
  let draftRevision = 0;
  let acting = false;
  let poll = null;
  let acknowledgement = null;
  let controlsKey = "";
  const unconfirmed = new Map();
  const readingPlaces = new Map();
  const composer = byId("composer");
  const statusBar = document.createElement("div");
  statusBar.className = "guide-actions";
  statusBar.style.marginBottom = "8px";
  const statusText = document.createElement("small");
  statusText.className = "hint";
  statusText.setAttribute("role", "status");
  const actions = document.createElement("div");
  actions.className = "inline-actions";
  statusBar.append(statusText, actions);
  composer.before(statusBar);

  function scoped(view) {
    return browsing && view === conversation && current(view.revision);
  }

  function api() {
    return window.pywebview.api;
  }

  function messageKey(item) {
    return JSON.stringify(item.id);
  }

  function atLatest() {
    return timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop < 30;
  }

  function rememberReadingPlace() {
    const view = conversation;
    if (!view) return;
    readingPlaces.delete(view.sid);
    readingPlaces.set(view.sid, {
      pid: view.pid, pages: view.olderPages,
      top: atLatest() ? null : timeline.scrollTop,
    });
    if (readingPlaces.size > 8) readingPlaces.delete(readingPlaces.keys().next().value);
  }

  function shown(view) {
    return scoped(view) && document.visibilityState === "visible" && !dialog.open &&
      !(sidebarQuery.matches && viewport.drawerOpen);
  }

  function canSend(view) {
    const state = view.info;
    return !!(state?.available && state.selected && state.writable && state.configured &&
      !state.pending && !unconfirmed.has(view.sid));
  }

  function updateSend() {
    const view = conversation;
    byId("send").disabled = !view || !canSend(view) || acting || !message.value.trim();
  }

  function updateStatus() {
    if (!browsing) return;
    const view = conversation;
    const info = view?.info;
    statusBar.hidden = !view;
    if (!view) return;
    message.disabled = !view.draftReady;
    const pending = info?.pending;
    const target = info?.turn || info?.batch;
    if (!info) statusText.textContent = "正在读取会话状态。";
    else if (!info.writable) statusText.textContent = "历史会话，可从左侧打开当前会话或新建会话。";
    else if (!info.selected) statusText.textContent = "正在浏览此人物的会话。";
    else if (!info.available) statusText.textContent = "工作台服务正在准备。";
    else if (pending?.state === "not_accepted") statusText.textContent = "原请求尚未受理。";
    else if (pending || unconfirmed.has(view.sid)) statusText.textContent = "有一条消息等待核对受理结果。";
    else if (info.turn) statusText.textContent = info.turn.status === "running" ? "回复进行中。" : "消息已排队。";
    else if (info.batch) statusText.textContent = info.batch.state === "interrupted_buffer"
      ? "有一批输入等待处理。" : "消息已收下。";
    else if (!info.configured) statusText.textContent = "配置模型连接后即可发送消息。";
    else if (info.latest?.stopped) statusText.textContent = "上一条回复已停止，已显示的内容已保留。";
    else if (info.latest?.connection_error) statusText.textContent = "上一条回复遇到了模型连接问题。";
    else if (info.latest?.status === "failed") statusText.textContent = "上一条回复未完成。";
    else if (info.latest?.status === "interrupted") statusText.textContent = "上一条回复已停止或待恢复。";
    else statusText.textContent = "";
    const atBottom = atLatest();
    const key = JSON.stringify([view.revision, acting, atBottom, info?.selected, info?.writable,
      info?.available, info?.configured, pending?.id, pending?.state, unconfirmed.has(view.sid),
      target?.id, info?.batch?.state, info?.latest?.connection_error]);
    if (controlsKey === key) {
      updateSend();
      return;
    }
    controlsKey = key;
    actions.replaceChildren();
    if (info?.available && info.writable && !info.selected) {
      actions.append(control("设为当前人物", () => activateConversation(false)));
    }
    if (target && info.available && info.selected) {
      actions.append(control(info.turn ? "停止回复" : "取消这批输入", () => stopReply(view, target, !!info.turn)));
    }
    if (!info?.turn && info?.batch?.state === "interrupted_buffer" && canSend(view)) {
      actions.append(control("继续这批输入", () => resumeBatch(view, info.batch.id)));
    }
    if (pending || unconfirmed.has(view.sid)) {
      actions.append(control("核对受理结果", () => checkSubmission(view)));
    }
    if (pending?.state === "not_accepted" && info.selected && info.writable && info.configured) {
      actions.append(control("重新提交原消息", () => retrySubmission(view)));
    }
    if (info && (!info.configured || info.latest?.connection_error)) {
      actions.append(control(info.configured ? "检查连接" : "配置连接", () => window.pcarSetup.connection()));
    }
    if (!atBottom) actions.append(control("回到最新", () => { timeline.scrollTop = timeline.scrollHeight; }));
    for (const button of actions.querySelectorAll("button")) button.disabled = acting;
    updateSend();
  }

  function drawThread(view, follow = atLatest()) {
    const lastTop = timeline.scrollTop;
    timeline.classList.remove("first");
    const thread = document.createElement("div");
    thread.className = "conversation-inner";
    if (view.before) thread.append(olderButton());
    const seen = new Set();
    const records = [...view.older, ...view.messages, ...view.members].filter(item => {
      const key = messageKey(item);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    thread.append(messageFragment(records));
    if (!records.length && !view.before) {
      const empty = document.createElement("p");
      empty.className = "date-divider";
      empty.textContent = "这段会话暂无消息。";
      thread.append(empty);
    }
    timeline.replaceChildren(thread);
    timeline.scrollTop = follow ? timeline.scrollHeight : lastTop;
    view.thread = thread;
  }

  function applyPage(view, page) {
    const oldest = page.messages[0]?.turn;
    const position = oldest ? view.messages.findIndex(item => item.turn === oldest) : -1;
    if (position > 0) {
      view.older.push(...view.messages.slice(0, position));
    } else if (position < 0) {
      view.older = [];
      view.before = page.before;
    }
    view.messages = page.messages;
  }

  async function confirmPresentation() {
    if (!acknowledgement) return;
    const currentAck = acknowledgement;
    await api().chat_ack(currentAck.ticket, currentAck.visible);
    if (acknowledgement === currentAck) acknowledgement = null;
    if (scoped(currentAck.view)) currentAck.view.version = null;
  }

  async function paintOffer(view, offer) {
    let bubble = null;
    if (shown(view) && atLatest() && view.thread?.isConnected) {
      const fragment = messageFragment([{text: offer.text, side: "persona", at: ""}]);
      bubble = fragment.firstElementChild;
      view.thread.append(fragment);
      timeline.scrollTop = timeline.scrollHeight;
    }
    if (bubble) {
      await new Promise(resolve => {
        // A hidden WebView can pause animation frames, so the guard settles its lease.
        const deadline = setTimeout(() => resolve(false), 200);
        requestAnimationFrame(() => requestAnimationFrame(() => {
          clearTimeout(deadline);
          resolve(true);
        }));
      }).then(painted => {
        if (!painted) { bubble.remove(); bubble = null; }
      });
    }
    const viewport = timeline.getBoundingClientRect();
    const bounds = bubble?.getBoundingClientRect();
    const visible = !!(bounds && bubble.isConnected && shown(view) &&
      bounds.bottom > viewport.top && bounds.top < viewport.bottom);
    acknowledgement = {ticket: offer.ticket, visible, view};
    if (bubble && !visible) bubble.remove();
    await confirmPresentation();
  }

  async function refreshLive() {
    await confirmPresentation();
    const view = conversation;
    if (!view || !scoped(view)) return;
    const info = await api().chat_state(view.pid, view.sid);
    if (!scoped(view)) return;
    const version = JSON.stringify(info.version);
    const members = info.members.map(messageKey).join("|");
    if (version !== view.version) {
      const page = await api().history_page(view.pid, view.sid);
      if (!scoped(view)) return;
      applyPage(view, page);
    }
    if (version !== view.version || members !== view.memberKeys) {
      view.members = info.members;
      view.memberKeys = members;
      view.version = version;
      drawThread(view);
    }
    view.info = info;
    updateStatus();
    if (!acting && info.available && info.selected && info.writable && shown(view) && atLatest()) {
      const offer = await api().chat_offer(view.pid, view.sid);
      if (offer) await paintOffer(view, offer);
    }
  }

  function pollConversation() {
    if (acting || poll || (!conversation && !acknowledgement)) return;
    poll = refreshLive().catch(() => {
      if (browsing && conversation) statusText.textContent = "暂时无法同步会话状态。";
    }).finally(() => { poll = null; });
  }

  async function runAction(view, operation, failure, keepScope = false) {
    if (acting) return;
    acting = true;
    updateStatus();
    try {
      if (poll) await poll;
      await confirmPresentation();
      if (keepScope || (!view && browsing) || (view && scoped(view))) await operation();
    } catch {
      if ((!view && browsing) || (view && scoped(view))) notify(failure);
    } finally {
      acting = false;
      updateStatus();
      pollConversation();
    }
  }

  function activateConversation(fresh, focusComposer = false) {
    const item = person;
    const selected = fresh ? null : session;
    if (!item) return;
    const view = conversation;
    runAction(view, async () => {
      if (person !== item || (selected && session !== selected)) return;
      const result = await api().chat_activate(item.id, selected?.id ?? null, fresh);
      if (browsing && person === item && (!view || scoped(view))) {
        await selectPersonHistory(item, result.id);
        if (focusComposer && person?.id === item.id && !composer.hidden) message.focus();
      }
    }, fresh ? "暂时无法新建会话，请先处理当前会话的待回复消息。" : "暂时无法切换当前人物。");
  }

  function newRequestId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, "0")).join("");
  }

  async function sendMessage(view, text, revision) {
    const id = newRequestId();
    await api().chat_save_draft(view.pid, view.sid, text, revision);
    unconfirmed.set(view.sid, id);
    const result = await api().chat_send(view.pid, view.sid, text, id, revision);
    if (result.state === "accepted") {
      unconfirmed.delete(view.sid);
      if (scoped(view) && draftRevision === revision && message.value === text) message.value = "";
      if (scoped(view) && result.draft_warning) notify("消息已受理，草稿暂未清除。");
      view.version = null;
    }
  }

  function checkSubmission(view) {
    runAction(view, async () => {
      const revision = draftRevision;
      const result = await api().chat_check(view.pid, view.sid, unconfirmed.get(view.sid) ?? null);
      if (result.state === "accepted" || result.state === "not_accepted" || result.state === "none") {
        unconfirmed.delete(view.sid);
        view.version = null;
      }
      if (scoped(view) && result.state === "accepted" && draftRevision === revision) {
        const draft = await api().chat_draft(view.pid, view.sid);
        if (scoped(view) && draftRevision === revision) message.value = draft;
      }
      if (scoped(view)) notify({
        accepted: "已找到原消息的受理结果。", not_accepted: "暂未找到受理记录。",
        none: "当前会话没有待核对消息。", unconfirmed: "暂时无法核对，请稍后重试。",
      }[result.state]);
    }, "暂时无法核对送出结果。");
  }

  function retrySubmission(view) {
    runAction(view, async () => {
      const revision = draftRevision;
      const result = await api().chat_retry(view.pid, view.sid);
      if (result.state === "accepted") {
        unconfirmed.delete(view.sid);
        view.version = null;
        const draft = await api().chat_draft(view.pid, view.sid);
        if (scoped(view) && revision === draftRevision) message.value = draft;
      }
    }, "暂时无法重新提交原消息。");
  }

  function stopReply(view, target, turn) {
    runAction(view, async () => {
      await api().chat_cancel(view.pid, view.sid, turn ? "turn" : "batch", target.id);
      view.version = null;
      if (scoped(view)) notify(turn ? "已停止当前回复。" : "已取消这批输入。");
    }, "停止状态暂未确认，请核对当前会话。");
  }

  function resumeBatch(view, batchId) {
    runAction(view, async () => {
      await api().chat_resume_batch(view.pid, view.sid, batchId);
      view.version = null;
    }, "暂时无法继续这批输入。");
  }

  function control(label, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button";
    button.textContent = label;
    button.addEventListener("click", action);
    return button;
  }

  function placeholder(title, hint, retry) {
    timeline.classList.add("first");
    const panel = document.createElement("div");
    panel.className = "chat-empty";
    const heading = document.createElement("strong");
    heading.textContent = title;
    const description = document.createElement("p");
    description.textContent = hint;
    panel.append(heading, description);
    if (retry) panel.append(control("重试", retry));
    timeline.replaceChildren(panel);
  }

  function current(revision) {
    return browsing && revision === generation;
  }

  function drawPeople() {
    const list = byId("people");
    list.replaceChildren();
    if (!people.length) emptyList(list, "暂无本机角色");
    for (const item of people) {
      list.append(listButton(item.name, item.id === person?.id, () => selectPersonHistory(item), true));
    }
  }

  function sessionLabel(item) {
    const date = new Date(item.created_at);
    return (item.selected ? "当前会话 · " : "") +
      (Number.isNaN(date.valueOf()) ? item.created_at : date.toLocaleString("zh-CN", {hour12: false}));
  }

  function drawSessions() {
    const list = byId("sessions");
    list.replaceChildren();
    byId("new-session").hidden = !person;
    byId("profile-button").hidden = !person;
    byId("search-box").hidden = !sessions.length;
    const query = search.value.trim();
    const matching = sessions.filter(item => sessionLabel(item).includes(query));
    if (!matching.length) emptyList(list, query ? "没有匹配的会话" : "暂无桌面会话");
    for (const item of matching) {
      list.append(listButton(sessionLabel(item), item.id === session?.id, () => selectSession(item)));
    }
  }

  function messageFragment(messages) {
    const fragment = document.createDocumentFragment();
    for (const item of messages) {
      const bubble = document.createElement("p");
      bubble.className = "bubble " + (item.side === "user" ? "own" : "reply");
      bubble.setAttribute("aria-label", item.side === "user" ? "你的消息" : "角色回复");
      bubble.textContent = item.text;
      bubble.title = item.at;
      fragment.append(bubble);
    }
    return fragment;
  }

  function olderButton() {
    const button = control("加载更早记录", loadOlder);
    button.style.justifySelf = "center";
    return button;
  }

  async function loadOlder(event) {
    const button = event.currentTarget;
    const view = conversation;
    if (!view || !view.before) return;
    const cursor = view.before;
    button.disabled = true;
    try {
      const page = await api().history_page(view.pid, view.sid, cursor);
      if (!scoped(view)) return;
      const height = timeline.scrollHeight;
      const top = timeline.scrollTop;
      view.older.unshift(...page.messages);
      view.before = page.before;
      view.olderPages += 1;
      drawThread(view, false);
      timeline.scrollTop = top + timeline.scrollHeight - height;
    } catch {
      if (scoped(view)) notify("暂时无法加载更早的记录。");
    } finally {
      button.disabled = false;
    }
  }

  async function selectSession(item) {
    rememberReadingPlace();
    const revision = ++generation;
    session = item;
    conversation = null;
    composer.hidden = true;
    statusBar.hidden = true;
    drawSessions();
    closeDrawer();
    placeholder("正在读取聊天历史", "请稍候。");
    try {
      const pid = person.id;
      const page = await api().history_page(pid, item.id);
      if (!current(revision)) return;
      const view = {revision, pid, sid: item.id, older: [], olderPages: 0, messages: page.messages,
        members: [], before: page.before, version: null, draftReady: false};
      const saved = readingPlaces.get(item.id);
      const resume = saved?.pid === pid && saved.top !== null;
      if (resume) {
        while (view.before && view.olderPages < saved.pages) {
          const older = await api().history_page(pid, item.id, view.before);
          if (!current(revision)) return;
          view.older.unshift(...older.messages);
          view.before = older.before;
          view.olderPages += 1;
        }
      }
      conversation = view;
      drawThread(view, !resume);
      if (resume) timeline.scrollTop = saved.top;
      composer.hidden = false;
      message.value = "";
      message.placeholder = "输入消息";
      updateStatus();
      try {
        const draft = await api().chat_draft(pid, item.id);
        if (scoped(view)) message.value = draft;
      } catch {
        if (scoped(view)) notify("暂时无法恢复草稿。");
      }
      if (scoped(view)) {
        view.draftReady = true;
        updateStatus();
        pollConversation();
      }
    } catch {
      if (current(revision)) {
        placeholder("未能读取这段会话", "可重试或切换到其他会话。", () => selectSession(item));
      }
    }
  }

  async function selectPersonHistory(item, preferredSession = null) {
    rememberReadingPlace();
    const revision = ++generation;
    person = item;
    session = null;
    conversation = null;
    composer.hidden = true;
    statusBar.hidden = true;
    sessions = [];
    search.value = "";
    byId("current-person").textContent = item.name;
    drawPeople();
    drawSessions();
    closeDrawer();
    placeholder("正在读取会话", "请稍候。");
    try {
      const result = await window.pywebview.api.history_sessions(item.id);
      if (!current(revision)) return;
      sessions = result;
      drawSessions();
      if (sessions.length) {
        const lastPlace = [...readingPlaces.entries()].findLast(([, place]) => place.pid === item.id);
        const expected = preferredSession ?? lastPlace?.[0];
        await selectSession(sessions.find(candidate => candidate.id === expected) || sessions[0]);
      } else {
        renderGuide(revision);
      }
    } catch {
      if (current(revision)) {
        placeholder("未能读取会话", "可重试或从左侧选择其他人物。", () => selectPersonHistory(item));
      }
    }
  }

  function renderGuide(revision = generation) {
    if (!current(revision) || session || !window.pcarSetup) return;
    window.pcarSetup.guide(person, people, () => current(revision) && !session);
  }

  async function openHistory(targetId) {
    rememberReadingPlace();
    const revision = ++generation;
    const preferred = typeof targetId === "string" ? targetId : person?.id;
    const preferredSession = session?.id;
    people = [];
    sessions = [];
    person = null;
    session = null;
    conversation = null;
    composer.hidden = true;
    statusBar.hidden = true;
    byId("current-person").textContent = "聊天";
    drawPeople();
    drawSessions();
    placeholder("正在读取本机角色", "请稍候。");
    try {
      let result;
      for (let attempt = 0; attempt < 18; attempt += 1) {
        try {
          result = await window.pywebview.api.history_people();
        } catch (error) {
          if (attempt === 17) throw error;
        }
        if (!current(revision)) return;
        if (result && !result.waiting) break;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      if (!current(revision)) return;
      if (!result || result.waiting) throw new Error("history database not ready");
      people = result.people;
      drawPeople();
      if (people.length) {
        const target = people.find(item => item.id === (preferred || result.selected)) || people[0];
        await selectPersonHistory(target, target.id === preferred ? preferredSession : null);
      } else {
        renderGuide(revision);
      }
    } catch {
      if (current(revision)) {
        placeholder("暂时无法读取本机历史", "请确认实例已就绪后重试。", openHistory);
      }
    }
  }

  function visibility() {
    byId("composer").hidden = browsing;
    message.maxLength = browsing ? 24000 : 1500;
    if (browsing) byId("toast").hidden = true;
    document.querySelector(".demo-label").textContent = browsing ? "桌面聊天" : "交互 Demo";
    switchView.textContent = browsing ? "体验演示" : "返回本机聊天";
    byId("new-person").hidden = false;
    byId("new-session").hidden = browsing && !person;
    byId("profile-button").hidden = browsing && !person;
    resetDemo.hidden = browsing;
  }

  const refresh = control("刷新人物与会话", () => { showMenu(false); openHistory(); });
  refresh.className = "rail-row";
  byId("app-menu").prepend(refresh);
  visibility();
  placeholder("正在打开本机历史", "请稍候。");

  switchView.addEventListener("click", event => {
    event.stopImmediatePropagation();
    rememberReadingPlace();
    browsing = !browsing;
    generation += 1;
    refresh.hidden = !browsing;
    visibility();
    if (browsing) openHistory();
    else {
      conversation = null;
      statusBar.hidden = true;
      byId("new-person").hidden = false;
      openSample();
    }
  }, true);
  search.addEventListener("input", event => {
    if (!browsing) return;
    event.stopImmediatePropagation();
    drawSessions();
  }, true);
  byId("new-session").addEventListener("click", event => {
    if (!browsing) return;
    event.stopImmediatePropagation();
    activateConversation(true);
  }, true);
  message.addEventListener("input", event => {
    if (!browsing) return;
    event.stopImmediatePropagation();
    const view = conversation;
    if (!view) return;
    const revision = ++draftRevision;
    api().chat_save_draft(view.pid, view.sid, message.value, revision).catch(() => {
      if (scoped(view) && revision === draftRevision) notify("暂时无法保存本机草稿。");
    });
    updateSend();
  }, true);
  composer.addEventListener("submit", event => {
    if (!browsing) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const view = conversation;
    const text = message.value;
    const revision = draftRevision;
    if (view?.draftReady && canSend(view) && text.trim()) {
      runAction(view, () => sendMessage(view, text, revision), "本机提交暂未确认，草稿已保留。", true);
    }
  }, true);
  timeline.addEventListener("scroll", () => {
    if (browsing && conversation) updateStatus();
  }, {passive: true});
  window.setInterval(pollConversation, 650);

  window.pcarHistory = Object.freeze({
    active: () => browsing,
    selection: () => ({person, session}),
    refresh: openHistory,
    select: selectPersonHistory,
    start: () => activateConversation(false, true),
    rename: (id, name) => {
      const item = people.find(candidate => candidate.id === id);
      if (!item) return;
      item.name = name;
      if (person?.id === id) byId("current-person").textContent = name;
      drawPeople();
    },
    configurationChanged: () => { if (session) pollConversation(); else renderGuide(); },
  });

  window.addEventListener("pywebviewready", openHistory, { once: true });
  if (window.pywebview?.api) openHistory();
})();
