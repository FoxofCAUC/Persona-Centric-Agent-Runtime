(() => {
  "use strict";

  const history = window.pcarHistory;
  const core = [
    ["name", "角色名称", "input"],
    ["introduction", "人物介绍"],
    ["initial_background", "初始背景"],
    ["core_traits", "核心特征", "list"],
    ["values_and_boundaries", "价值取向与边界", "list"]
  ];
  const expression = ["expression_style", "表达习惯"];
  const relationship = ["description", "相处关系"];
  const extensionFields = [["appearance", "外貌"], ["hobbies", "兴趣与爱好", "list"], ["world", "世界背景"]];
  const sections = {
    core: "基本设定", expression: "表达习惯", relationship: "相处关系",
    state: "临时状态", character: "补充资料"
  };
  const categories = {
    user_fact: "关于我的事实", preference: "偏好", shared_experience: "共同经历",
    relationship: "相处关系", world_knowledge: "世界知识"
  };
  const errors = {
    service_waiting: "本机服务正在启动。请稍候再试。",
    invalid_connection: "请选择表单中的模型选项。",
    invalid_key: "请输入完整的 API Key，去掉空格和换行。",
    replace_legacy_key: "旧连接的密钥需要重新填写。请输入官方 DeepSeek 的 API Key。",
    connection_required: "请先保存有效的 DeepSeek 配置和 API Key。",
    test_confirmation_required: "请先确认要发起真实连接测试。",
    migration_configuration_required: "此实例还有迁移项目待确认。可在旧版工作台完成迁移配置。",
    en5_desktop_ingress_required: "当前实例的体验配置未开放此连接测试入口。",
    unsupported_package: "请选择 PCAR 的角色 JSON。业务迁移包可在旧版中处理。",
    invalid_package: "无法读取该角色 JSON，请检查文件格式和内容。",
    package_too_large: "角色 JSON 超过 256 KiB，请选择较小的文件。",
    package_changed: "导入的文件已变化，请重新选择文件。",
    invalid_persona: "请检查名称、人物介绍、背景、特征、边界和表达习惯是否完整。",
    profile_too_long: "填写内容超过此项的保存上限，请适当缩短。",
    invalid_material: "请检查标题或场景、正文和关键词。关键词每行一项，最多 30 项。",
    invalid_extensions: "请检查年龄、外貌、兴趣或世界背景的填写。兴趣每行一项。",
    too_many_lore_entries: "此角色已有 100 条设定，建议先整理旧条目。",
    too_many_expression_examples: "此角色已有 50 条表达样例，建议先整理旧条目。",
    character_too_large: "此角色的补充资料已达到容量上限。",
    profile_version_conflict: "人物资料已有更新。你的填写仍在下方，请先核对最新版本。",
    character_version_conflict: "补充资料已有更新。你的填写仍在下方，请先核对最新版本。",
    version_conflict: "这项设置已有更新。请打开最新版本后再决定。",
    source_expired: "这一版本的资料来源已撤回，无法恢复。",
    state_expiry_must_be_aware_future_within_7_days: "结束时间须在接下来 7 天内。清空正文可结束临时状态。",
    select_person_first: "请先在人物资料中将此角色设为当前人物。",
    persona_selection_conflict: "当前人物已变化，请重新打开人物资料。",
    character_selection_conflict: "当前人物已变化，请重新打开人物资料。",
    organization_disabled: "此角色的记忆整理已停用，可在旧版查看整理设置。",
    organization_connection_required: "请先在旧版工作台配置记忆整理的模型连接。",
    organization_model_changed: "记忆整理使用的模型已变化，请在旧版核对授权。",
    invalid_memory: "请填写记忆内容和类别。",
    memory_source_changed: "原消息的使用状态已变化，请重新选择来源。",
    unconfirmed: "本机暂未确认操作结果，请先核对保存状态。",
    submission_conflict: "这次操作的内容已变化，请核对保存状态。",
    local_error: "暂时无法访问本机数据，请稍后再试。"
  };
  const testReasons = {
    provider_http_401: "API Key 未通过验证，请检查后更换密钥。",
    provider_http_402: "DeepSeek 拒绝了计费请求，请检查账户余额。",
    provider_http_403: "DeepSeek 拒绝访问，请检查密钥权限。",
    provider_http_404: "DeepSeek 未找到此模型，请检查已保存的模型选项。",
    provider_http_429: "DeepSeek 限制了请求频率，请稍后再试。",
    provider_timeout: "等待 DeepSeek 响应超时。",
    provider_connection_timeout: "建立 DeepSeek 连接超时。",
    provider_network_error: "连接失败，请检查网络和 Windows 代理。",
    system_proxy_required: "请检查 Windows 系统代理是否已启用。",
    hourly_limit_reached: "已达到本机每小时调用上限。"
  };
  const errorLine = '<p class="error" id="setup-error" role="alert" hidden></p>';
  let view = 0;
  let locked = false;
  let chosenPackage = null;
  let lastTest = null;
  let unresolvedCreation = null;

  async function setup(action, args = {}) {
    const result = await window.pywebview.api.setup(action, args);
    if (!result.ok) {
      const error = new Error(errors[result.error] || "未能完成这项操作，请检查内容或运行状态。");
      error.code = result.error;
      throw error;
    }
    return result.value;
  }

  function requestId() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const chars = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
    return [chars.slice(0, 8), chars.slice(8, 12), chars.slice(12, 16),
      chars.slice(16, 20), chars.slice(20)].join("-");
  }

  function isOpen(revision) {
    return dialog.open && revision === view;
  }

  function panel(title, body, action = "", submit = null) {
    view += 1;
    present(title, errorLine + body, action, submit);
    return view;
  }

  function report(error) {
    const hint = byId("setup-error");
    hint.textContent = error?.code ? error.message : "本机暂未确认结果，请稍后重试。";
    hint.hidden = false;
    hint.tabIndex = -1;
    hint.focus();
  }

  async function run(work, onError = report) {
    if (locked) return;
    const revision = view;
    const controls = [...dialogForm.querySelectorAll("button,input,textarea,select")];
    const disabled = controls.map(control => control.disabled);
    controls.forEach(control => { control.disabled = true; });
    locked = true;
    dialogForm.setAttribute("aria-busy", "true");
    try {
      await work();
    } catch (error) {
      if (isOpen(revision)) onError(error);
    } finally {
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      dialogForm.removeAttribute("aria-busy");
      locked = false;
    }
  }

  async function load(title, read, show) {
    const revision = panel(title, '<p class="hint" role="status">正在读取本机资料…</p>');
    try {
      const value = await read();
      if (isOpen(revision)) show(value);
    } catch (error) {
      if (!isOpen(revision)) return;
      report(error);
      dialogBody.querySelector(".hint").remove();
      dialogBody.append(button("重试", () => load(title, read, show)));
    }
  }

  function button(label, onClick, style = "") {
    const result = document.createElement("button");
    result.type = "button";
    result.className = ("button " + style).trim();
    result.textContent = label;
    result.addEventListener("click", onClick);
    return result;
  }

  function el(tag, text, css = "") {
    const result = document.createElement(tag);
    if (css) result.className = css;
    if (text != null) result.textContent = text;
    return result;
  }

  function when(value) {
    const date = new Date(value);
    return Number.isFinite(date.valueOf()) ? date.toLocaleString("zh-CN", {hour12: false}) : "";
  }

  function field([name, label, type], required = true) {
    const attr = ' name="' + name + '"' + (required ? ' required' : '');
    const control = type === "input" ? '<input' + attr + ' maxlength="250" data-autofocus>'
      : '<textarea' + attr + ' rows="3" maxlength="16000"></textarea>';
    return '<label class="field">' + label + (type === "list" ?
      ' <small class="hint">每行一项</small>' : '') + control + '</label>';
  }

  function putFields(fields, data = {}) {
    for (const [name] of fields) {
      dialogForm.elements[name].value = fieldValue(data[name]);
    }
  }

  function fieldValue(value) {
    return (Array.isArray(value) ? value.join("\n") : value ?? "").replace(/\r\n?/g, "\n");
  }

  function collect(fields, form, original = {}) {
    const result = {};
    for (const [name, label, type] of fields) {
      const text = form.get(name);
      const old = original[name];
      result[name] = old !== undefined && fieldValue(old) === text ? old
        : type === "list" ? lines(text) : text.trim();
      if (type === "list" ? !result[name].length : !result[name].trim()) {
        const error = new Error("请填写" + label + "。");
        error.code = "required";
        throw error;
      }
    }
    return result;
  }

  function lines(text) {
    return text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  }

  function extensionMarkup() {
    return '<div class="fields"><label class="field">固定年龄<input name="age" type="number" min="0" step="1"></label>' +
      '<p class="hint" id="age-from-package" hidden>沿用角色包的年龄设定。</p>' +
      extensionFields.map(item => field(item, false)).join("") + '</div>';
  }

  function showExtensions(value = {}) {
    putFields(extensionFields, value);
    const age = dialogForm.elements.age;
    age.value = value.age ?? "";
    age.disabled = value.age_editable === false;
    byId("age-from-package").hidden = !age.disabled;
    dialogForm.elements.appearance.maxLength = 2000;
    dialogForm.elements.world.maxLength = 2000;
  }

  function collectExtensions(form, base = {}) {
    const changes = {};
    if (form.has("age")) {
      const raw = form.get("age");
      const age = raw === "" ? null : Number(raw);
      if (age !== (base.age ?? null)) changes.age = age;
    }
    for (const [name, , type] of extensionFields) {
      const text = form.get(name);
      const old = base[name];
      const original = fieldValue(old);
      if (text !== original) changes[name] = type === "list" ? lines(text) : text.trim() || null;
    }
    return changes;
  }

  function discardPackage() {
    const token = chosenPackage;
    chosenPackage = null;
    if (token) setup("discard_package", {token}).catch(() => {});
  }

  function configure() {
    load("模型连接", () => setup("connection"), showConnection);
  }

  function showConnection(state) {
    const revision = panel("模型连接",
      '<p>DeepSeek 官方 API</p><p class="hint" id="setup-endpoint"></p>' +
      '<p role="status" id="key-state"></p><p class="hint" id="legacy-state" hidden></p>' +
      '<div class="fields"><label class="field">聊天模型<select name="model" data-autofocus></select></label>' +
      '<label class="field">API Key 操作<select name="key_mode">' +
      '<option value="keep">保留现有设置</option><option value="replace">输入新密钥</option>' +
      '<option value="clear">清除密钥</option></select></label>' +
      '<label class="field" id="key-row" hidden>新 API Key<input name="api_key" type="password" autocomplete="off" ' +
      'maxlength="4096" spellcheck="false"></label>' +
      '<label class="hint" id="key-clear" hidden><input type="checkbox" name="clear_confirm"> ' +
      '我确认暂停使用此密钥的聊天及共用此连接的功能。</label></div>' +
      '<p class="hint">保存只更新本机配置。启用了独立模型配置的角色继续使用其已授权连接。</p>' +
      '<div class="inline-actions" id="connection-actions"></div><p class="hint" id="test-status" role="status" hidden></p>',
      "保存连接", form => run(async () => {
        const updated = await setup("save_connection", {
          name: form.get("model"), key_mode: form.get("key_mode"), key: (form.get("api_key") || "").trim()
        });
        key.value = "";
        lastTest = null;
        history.configurationChanged();
        showConnection(updated);
        byId("key-state").textContent = updated.has_key ? "已保存 · API Key 已设置" : "已保存 · API Key 未设置";
      }, error => {
        report(error);
        if (error.code === "unconfirmed") key.value = "";
      })
    );
    byId("setup-endpoint").textContent = state.endpoint;
    byId("key-state").textContent = state.has_key ? "API Key 已设置" : "API Key 未设置";
    if (state.legacy || state.migration_pending) {
      const row = byId("legacy-state");
      row.hidden = false;
      row.textContent = state.migration_pending ? errors.migration_configuration_required
        : "当前保存的是旧连接。请选择官方模型，核对密钥后保存。";
    }
    const model = dialogForm.elements.model;
    for (const choice of state.choices) {
      model.append(new Option(choice === state.choices[0] ? choice + "（默认）" : choice, choice));
    }
    model.value = state.choices.includes(state.name) ? state.name : state.choices[0];
    const mode = dialogForm.elements.key_mode;
    const key = dialogForm.elements.api_key;
    const clear = dialogForm.elements.clear_confirm;
    mode.options[0].disabled = !state.key_reusable;
    if (!state.has_key || !state.key_reusable) mode.value = "replace";
    function keyMode() {
      const replace = mode.value === "replace";
      const clearing = mode.value === "clear";
      byId("key-row").hidden = !replace;
      key.disabled = !replace;
      key.required = replace;
      if (!replace) key.value = "";
      byId("key-clear").hidden = !clearing;
      clear.disabled = !clearing;
      clear.required = clearing;
      if (!clearing) clear.checked = false;
      if (test) test.disabled = !state.configured || state.migration_pending ||
        mode.value !== "keep" || model.value !== state.name;
    }
    const test = button("测试已保存连接…", () => confirmTest(state));
    byId("connection-actions").append(test);
    if (lastTest) {
      const recheck = button("核对测试结果", () => refreshTest(revision, 0));
      byId("connection-actions").append(recheck);
      test.hidden = ["queued", "running", "unknown"].includes(lastTest.status);
      refreshTest(revision, 0);
    }
    mode.addEventListener("change", keyMode);
    model.addEventListener("change", keyMode);
    keyMode();
  }

  function confirmTest(state) {
    panel("测试连接",
      '<p>将用刚保存的配置向 DeepSeek 发起一次测试，可能产生费用，并计入每小时调用额度。</p>' +
      '<p class="hint">测试使用连接探针，与当前人物和聊天内容分开。</p>', "确认测试",
      () => run(async () => {
        const id = requestId();
        lastTest = {id, status: "unknown"};
        try {
          const result = await setup("test_connection", {request_id: id, confirmed: true});
          lastTest = {id, ...result};
        } catch (error) {
          if (error.code && error.code !== "unconfirmed") {
            lastTest = null;
            throw error;
          }
        }
        showConnection(state);
      })
    );
    dialogBody.append(button("返回模型连接", () => showConnection(state), "ghost"));
  }

  async function refreshTest(revision, attempt) {
    const previous = lastTest;
    if (!previous || !isOpen(revision)) return;
    const label = byId("test-status");
    label.hidden = false;
    try {
      const result = await setup("test_status", {request_id: previous.id});
      if (!isOpen(revision) || lastTest?.id !== previous.id) return;
      lastTest = {id: previous.id, ...result};
      const pending = ["queued", "running"].includes(result.status);
      label.textContent = pending ? "正在等待测试结果…"
        : result.status === "completed" ? "DeepSeek 连接测试成功。"
        : result.status === "unknown" ? "测试受理暂未确认，可再次核对结果。"
        : testReasons[result.reason] || "连接测试未完成，请检查本机运行状态。";
      byId("connection-actions").firstElementChild.hidden = pending || result.status === "unknown";
      if (pending && attempt < 55) setTimeout(() => refreshTest(revision, attempt + 1), 1100);
    } catch {
      if (isOpen(revision)) label.textContent = "暂时无法读取测试结果。";
    }
  }

  function materialDescription(summary) {
    return "扩展资料 " + summary.attributes + " 组 · 聊天可参考设定 " + summary.enabled +
      " 条 · 仅作者可见 " + summary.private + " 条 · 设定共 " + summary.lore +
      " 条 · 表达样例 " + summary.examples + " 条（已启用 " + summary.examples_enabled + " 条）";
  }

  function roleForm() {
    if (unresolvedCreation) {
      checkCreation();
      return;
    }
    discardPackage();
    const fields = [...core, expression];
    let imported = null;
    const group = (label, items) => '<fieldset class="setup-group"><legend>' + label + '</legend>' +
      '<div class="fields">' + items.map(entry => field(entry)).join("") + '</div></fieldset>';
    panel("新建角色",
      '<div class="inline-actions" id="role-import-actions"></div>' +
      '<input id="role-file" type="file" accept=".json,application/json" hidden>' +
      '<p class="hint" id="role-import-status">载入 PCAR 角色 JSON 会预填下方资料。选择空白创建时可直接填写。</p>' +
      group("人物身份", core.slice(0, 3)) + group("性格与边界", core.slice(3)) +
      group("说话方式", [expression]) +
      '<details><summary>相处关系和扩展资料</summary><div class="fields">' +
      field(relationship, false) + extensionMarkup() + '</div></details>' +
      '<p class="hint">保存会建立独立角色。人物设定和关系可用于聊天，角色包的补充资料沿用原可见范围。</p>',
      "创建角色", form => run(async () => {
        if (unresolvedCreation) {
          await checkCreation();
          return;
        }
        const args = {
          spec: {format_version: 1, ...collect(fields, form, imported?.spec)},
          relationship: form.get("description").trim(), package_token: imported?.token ?? null,
          extensions: collectExtensions(form, imported?.extensions),
          request_id: requestId()
        };
        try {
          created(await setup("create_role", args));
        } catch (error) {
          if (!error.code || error.code === "unconfirmed") {
            unresolvedCreation = args;
            byId("role-import-actions").append(button("核对创建结果", checkCreation));
          }
          throw error;
        }
      })
    );
    const fileInput = byId("role-file");
    byId("role-import-actions").append(button("从角色文件预填…", () => fileInput.click()));
    fileInput.addEventListener("change", () => {
      const file = fileInput.files[0];
      fileInput.value = "";
      if (!file) return;
      run(async () => {
        const problem = !file.name.toLowerCase().endsWith(".json") ? "unsupported_package" :
          file.size > 256 * 1024 ? "package_too_large" : null;
        if (problem) throw Object.assign(new Error(errors[problem]), {code: problem});
        let text;
        try {
          text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
        } catch {
          throw Object.assign(new Error(errors.invalid_package), {code: "invalid_package"});
        }
        const result = await setup("preview_package", {text});
        imported = result;
        chosenPackage = result.token;
        putFields(fields, result.spec);
        showExtensions(result.extensions);
        byId("role-import-status").textContent = "文件已载入。可修改以下设定后创建角色。" +
          " " + materialDescription(result.materials) + "。";
        requestAnimationFrame(() => dialogForm.elements.name?.focus());
      });
    });
    showExtensions();
  }

  async function checkCreation() {
    const original = unresolvedCreation;
    if (!original) return;
    if (!dialog.open) panel("核对创建结果", '<p class="hint">正在核对本机创建记录。</p>');
    const revision = view;
    try {
      const result = await setup("creation_status", {request_id: original.request_id});
      if (result) created(result);
      else if (isOpen(revision) && !byId("retry-create")) {
        const retry = button("按原内容重试", () => run(async () => {
          try {
            created(await setup("create_role", original));
          } catch (error) {
            if (error.code && error.code !== "unconfirmed") {
              unresolvedCreation = null;
              retry.remove();
            }
            throw error;
          }
        }));
        retry.id = "retry-create";
        dialogBody.prepend(el("p", "暂未看到创建回执。可用同一份提交重试，避免重复建立角色。", "hint"), retry);
      }
    } catch {
      notify("暂时无法核对创建结果。");
    }
  }

  function created(result) {
    unresolvedCreation = null;
    discardPackage();
    dialog.close();
    history.refresh(result.id);
    notify("角色已创建。可从此人物开始聊天。");
  }

  function showPerson(item = history.selection().person) {
    if (!item) return;
    load("人物资料", () => setup("person", {persona_id: item.id}), person => {
      panel("人物资料",
        '<div class="profile-head"><span class="avatar">' + icon("person") + '</span><strong id="profile-name"></strong></div>' +
        '<p class="profile-text" id="profile-intro"></p><p class="hint" id="profile-selection"></p>' +
        '<div class="inline-actions" id="profile-actions"></div>' +
        '<details><summary>当前设定</summary><div id="profile-summary"></div></details>' +
        '<p class="hint" id="profile-materials"></p>'
      );
      byId("profile-name").textContent = person.name;
      byId("profile-intro").textContent = person.spec.introduction;
      byId("profile-materials").textContent = materialDescription(person.materials);
      const summary = {core_traits: person.spec.core_traits, values_and_boundaries: person.spec.values_and_boundaries,
        initial_background: person.spec.initial_background, expression_style: person.spec.expression_style,
        description: person.versions.relationship.data.description};
      if (person.state_active) summary.text = person.versions.state.data.text;
      for (const [name] of extensionFields) summary[name] = person.extensions[name];
      if (person.extensions.age !== null) summary.age = person.extensions.age;
      byId("profile-summary").append(describe(summary));
      const actions = byId("profile-actions");
      actions.append(button("版本历史", () => showVersions(person)));
      actions.append(button("记忆", () => showMemories(person)));
      if (person.selected) {
        const edit = button("编辑", () => chooseEditor(person), "primary");
        actions.prepend(edit, button("补充资料", () => chooseMaterial(person)));
        byId("profile-selection").textContent = "当前聊天人物";
      } else {
        byId("profile-selection").textContent = "正在查看人物资料。设为当前人物后即可修改。";
        actions.prepend(button("设为当前人物", () => run(async () => {
          await setup("select_person", {persona_id: person.id});
          history.configurationChanged();
          showPerson(person);
        }), "primary"));
      }
    });
  }

  function chooseEditor(person) {
    panel("编辑人物", '<div class="inline-actions" id="editor-sections"></div>');
    const actions = byId("editor-sections");
    for (const kind of Object.keys(person.versions)) {
      actions.append(button(sections[kind], () => editProfile(person, kind)));
    }
    actions.append(button("扩展资料", () => editExtensions(person)));
    dialogBody.append(button("返回人物资料", () => showPerson(person), "ghost"));
  }

  function localDate(value) {
    if (!value) return "";
    const date = new Date(value);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return Number.isFinite(local.valueOf()) ? local.toISOString().slice(0, 16) : "";
  }

  function editProfile(person, kind) {
    const fields = {core, expression: [["style", "表达习惯"]], relationship: [relationship],
      state: [["text", "当前状态"]]}[kind];
    let base = person.versions[kind];
    const required = kind === "core" || kind === "expression";
    const expiry = '<label class="field">结束时间<input type="datetime-local" name="expires_at"></label>' +
      '<p class="hint">保留至未来 7 天内的指定时间。清空状态内容即可结束。</p>';
    panel("编辑" + sections[kind],
      '<div class="fields">' + fields.map(item => field(item, required)).join("") +
      (kind === "state" ? expiry : '') + '</div><div id="version-conflict" hidden></div>',
      "保存修改", form => run(async () => {
        const data = required ? collect(fields, form, base.data) : {[fields[0][0]]: form.get(fields[0][0]).trim()};
        if (kind === "state") {
          const date = new Date(form.get("expires_at"));
          if (data.text && !Number.isFinite(date.valueOf())) {
            const error = new Error("请选择临时状态的结束时间。");
            error.code = "required";
            throw error;
          }
          data.expires_at = data.text ? date.toISOString() : null;
        }
        await setup("save_profile", {persona_id: person.id, kind, data, expected: base.id, request_id: requestId()});
        if (data.name && data.name !== person.name) history.rename(person.id, data.name);
        showPerson(person);
        notify("人物资料已保存。");
      }, error => showConflict(error, person, kind, latest => {
        for (const [name] of fields) {
          const control = dialogForm.elements[name];
          if (control.value === fieldValue(base.data[name])) control.value = fieldValue(latest.data[name]);
        }
        if (kind === "state") {
          const expiryField = dialogForm.elements.expires_at;
          if (expiryField.value === localDate(base.data.expires_at)) expiryField.value = localDate(latest.data.expires_at);
        }
        base = latest;
      }))
    );
    putFields(fields, base.data);
    if (kind === "state") dialogForm.elements.expires_at.value = localDate(base.data.expires_at);
  }

  function showConflict(error, person, kind, accept) {
    report(error);
    if (!["profile_version_conflict", "character_version_conflict"].includes(error.code)) return;
    const conflict = byId("version-conflict");
    conflict.hidden = false;
    conflict.replaceChildren(button("核对最新版本", () => run(async () => {
      const list = await setup("versions", {persona_id: person.id, kind});
      const result = await setup("version_detail", {persona_id: person.id, kind, version_id: list.current});
      const details = el("details");
      details.open = true;
      details.append(el("summary", "当前版本 " + result.version.revision), describe(result.version.data));
      const apply = button("以此版本为基准继续", () => {
        accept(result.version);
        conflict.hidden = true;
        const hint = byId("setup-error");
        hint.hidden = true;
        notify("你的填写已保留。核对后可再次保存。");
      });
      conflict.replaceChildren(details, apply);
    })));
  }

  function editExtensions(person) {
    let expected = person.materials.id;
    panel("编辑扩展资料", extensionMarkup() + '<div id="version-conflict" hidden></div>',
      "保存修改", form => run(async () => {
        await setup("save_extensions", {persona_id: person.id, changes: collectExtensions(form, person.extensions),
          expected, request_id: requestId()});
        showPerson(person);
        notify("扩展资料已保存。");
      }, error => showConflict(error, person, "character", latest => { expected = latest.id; }))
    );
    showExtensions(person.extensions);
  }

  function chooseMaterial(person) {
    panel("补充资料", '<p>添加人物设定，或保存你提供的表达样例。</p><div class="inline-actions" id="material-options"></div>');
    byId("material-options").append(
      button("新增设定", () => materialForm(person, "lorebook")),
      button("新增表达样例", () => materialForm(person, "examples")),
      button("返回人物资料", () => showPerson(person), "ghost")
    );
  }

  function materialForm(person, group) {
    const lore = group === "lorebook";
    const fields = lore ? [["title", "设定标题", "input"], ["category", "设定类别", "input"]]
      : [["scenario", "使用场景", "input"]];
    let expected = person.materials.id;
    panel(lore ? "新增设定" : "新增表达样例",
      '<div class="fields">' + fields.map(item => field(item)).join("") +
      field(["text", "正文"]) + field(["keywords", "关联关键词", "list"], false) +
      (lore ? '<label class="field">可见范围<select name="visibility"><option value="creator_only">仅作者可见</option>' +
        '<option value="conversation">允许聊天参考</option></select></label>' : '') +
      '<label class="hint" id="material-enable"><input type="checkbox" name="enabled"> 启用时允许对话按现有资料规则选用。</label>' +
      '</div><div id="version-conflict" hidden></div>', "保存资料",
      form => run(async () => {
        const entry = collect([...fields, ["text", "正文"]], form);
        entry.keywords = lines(form.get("keywords"));
        entry.enabled = form.get("enabled") === "on";
        if (lore) entry.visibility = form.get("visibility");
        await setup("append_material", {persona_id: person.id, group, fields: entry, expected, request_id: requestId()});
        showPerson(person);
        notify("资料已保存。");
      }, error => showConflict(error, person, "character", latest => { expected = latest.id; }))
    );
    if (lore) {
      const visibility = dialogForm.elements.visibility;
      const enabled = dialogForm.elements.enabled;
      const limitScope = () => {
        enabled.disabled = visibility.value === "creator_only";
        if (enabled.disabled) enabled.checked = false;
        byId("material-enable").hidden = enabled.disabled;
      };
      visibility.addEventListener("change", limitScope);
      limitScope();
    }
    dialogForm.elements.text.maxLength = lore ? 2000 : 500;
    for (const [name] of fields) dialogForm.elements[name].maxLength = name === "category" ? 100 : 200;
  }

  function showVersions(person, kind = "core", before = null) {
    const args = {persona_id: person.id, kind, before};
    load("版本历史", () => setup("versions", args), result => {
      panel("版本历史", '<div class="tablist" id="version-sections" aria-label="资料分类"></div>' +
        '<ul class="summary-list" id="version-rows"></ul><div class="inline-actions" id="version-actions"></div>');
      const tabs = byId("version-sections");
      for (const [key, label] of Object.entries(sections)) {
        const tab = button(label, () => showVersions(person, key));
        tab.setAttribute("aria-pressed", String(key === kind));
        tabs.append(tab);
      }
      for (const version of result.rows) {
        const row = el("li");
        row.append(button("版本 " + version.revision + (version.id === result.current ? " · 当前" : "") +
          " · " + when(version.created_at), () => showVersion(person, kind, version.id), "ghost"));
        byId("version-rows").append(row);
      }
      const actions = byId("version-actions");
      if (result.before) actions.append(button("更早版本", () => showVersions(person, kind, result.before)));
      if (before !== null) actions.append(button("回到最新", () => showVersions(person, kind)));
      actions.append(button("返回人物资料", () => showPerson(person), "ghost"));
    });
  }

  const labels = Object.fromEntries([...core, expression, relationship, ...extensionFields, ["style", "表达习惯"],
    ["text", "正文"], ["expires_at", "结束时间"], ["lorebook", "设定条目"], ["examples", "表达样例"],
    ["identity", "身份"], ["age", "年龄"], ["appearance", "外貌"], ["biography", "背景"],
    ["interests", "兴趣"], ["social", "社交"], ["world", "世界"], ["title", "标题"],
    ["category", "类别"], ["keywords", "关键词"], ["scenario", "场景"], ["enabled", "启用"],
    ["visibility", "可见范围"], ["description", "说明"]].map(([key, label]) => [key, label]));

  function describe(value) {
    if (Array.isArray(value)) {
      const result = el("div", null, "field");
      for (const item of value) result.append(describe(item));
      if (!value.length) result.append(el("span", "空", "hint"));
      return result;
    }
    if (value && typeof value === "object") {
      const list = el("ul", null, "summary-list");
      for (const [key, data] of Object.entries(value)) {
        if (data == null || data === "" || (Array.isArray(data) && !data.length) ||
            ["id", "schema_version", "dependencies", "priority"].includes(key)) continue;
        const row = el("li");
        row.append(el("strong", labels[key] || key), describe(data));
        if (row.lastElementChild.childElementCount || row.lastElementChild.textContent) list.append(row);
      }
      return list;
    }
    const displayed = value === true ? "已启用" : value === false ? "已停用"
      : value === "creator_only" ? "仅作者可见" : value === "conversation" ? "聊天可参考" : value;
    return el("p", displayed ?? "空", "profile-text");
  }

  function showVersion(person, kind, versionId) {
    const args = {persona_id: person.id, kind, version_id: versionId};
    load("查看版本", () => setup("version_detail", args), result => {
      panel(sections[kind] + " · 版本 " + result.version.revision,
        '<div id="version-content"></div><details><summary>与当前版本对比</summary>' +
        '<div id="version-differences"></div></details><div class="inline-actions" id="version-actions"></div>');
      byId("version-content").append(describe(result.version.data));
      const comparison = byId("version-differences");
      for (const [key, change] of Object.entries(result.differences)) {
        comparison.append(el("strong", labels[key] || key), el("p", "当前", "hint"), describe(change.current),
          el("p", "此版本", "hint"), describe(change.historical));
      }
      if (!Object.keys(result.differences).length) comparison.textContent = "设定内容相同。";
      const actions = byId("version-actions");
      if (result.selected && result.can_restore) {
        actions.append(button("恢复为新版本…", () => confirmRestore(person, args, result)));
      }
      actions.append(button("返回版本历史", () => showVersions(person, kind), "ghost"));
    });
  }

  function confirmRestore(person, args, result) {
    panel("恢复版本", "<p>将以历史设定生成一份新版本，适用于这个人物之后的聊天。</p>",
      "确认恢复", () => run(async () => {
        await setup("restore_version", {...args, expected: result.expected, request_id: requestId()});
        if (args.kind === "core") history.rename(person.id, result.version.data.name);
        showVersions(person, args.kind);
        notify("已恢复为新版本。");
      })
    );
  }

  function showMemories(person, query = "") {
    load("记忆", () => setup("memories", {persona_id: person.id, query}), result => {
      panel("记忆", '<label class="field">查找有效记忆<input type="search" name="query" maxlength="300"></label>' +
        '<div class="inline-actions" id="memory-actions"></div><ul class="summary-list" id="memory-items"></ul>',
        "查找", form => showMemories(person, form.get("query").trim()));
      dialogForm.elements.query.value = query;
      const actions = byId("memory-actions");
      actions.append(button("返回人物资料", () => showPerson(person), "ghost"));
      const state = "自动记忆" + (result.policy.enabled ? "已开启" : "已关闭");
      if (result.selected) {
        actions.prepend(button(state + "…", () => changePolicy(person, result.policy)));
        const selected = history.selection();
        if (selected.person?.id === person.id && selected.session) {
          actions.prepend(button("添加记忆", () => addMemory(person, selected.session.id)));
        } else {
          actions.before(el("p", "打开此人物的一段桌面会话后，可选一条消息作为记忆来源。", "hint"));
        }
      } else {
        actions.before(el("p", state + "。设为当前人物后即可修改。", "hint"));
      }
      const list = byId("memory-items");
      if (!result.items.length) list.append(el("li", query ? "没有匹配的有效记忆。" : "还没有有效记忆。", "hint"));
      for (const item of result.items) {
        const row = el("li");
        const details = el("details");
        details.append(el("summary", categories[item.category] || "记忆"),
          el("p", item.text, "profile-text"), el("p", "来源 · " + when(item.source_at), "hint"),
          el("p", item.source_text, "profile-text"));
        if (result.selected) {
          const controls = el("div", null, "inline-actions");
          controls.append(button("修订", () => changeMemory(person, item, false)),
            button("撤回", () => changeMemory(person, item, true)));
          details.append(controls);
        }
        row.append(details);
        list.append(row);
      }
    });
  }

  function addMemory(person, sessionId) {
    const scope = {persona_id: person.id, session_id: sessionId};
    load("添加记忆", () => setup("memory_sources", scope), sources => {
      if (!sources.length) {
        panel("添加记忆", "<p>此会话还没有可用的来源。发出一条消息后即可选择。</p>");
        return;
      }
      panel("添加记忆", '<label class="field">选择来源<select name="source" required></select></label>' +
        '<p class="profile-text" id="memory-source"></p><label class="field">记忆类别<select name="category"></select></label>' +
        field(["text", "记忆内容"]), "保存记忆", form => run(async () => {
          await setup("add_memory", {...scope, event_id: form.get("source"), category: form.get("category"),
            text: form.get("text").trim()});
          showMemories(person);
        }, error => {
          report(error);
          if (!error.code || error.code === "unconfirmed") {
            byId("dialog-actions").querySelector('[type="submit"]').remove();
            dialogBody.append(button("核对记忆列表", () => showMemories(person)));
          }
        }));
      const source = dialogForm.elements.source;
      for (const item of sources) source.append(new Option(when(item.created_at) + " · " + item.text.slice(0, 45), item.id));
      const detail = () => { byId("memory-source").textContent = sources.find(x => x.id === source.value)?.text ?? ""; };
      source.addEventListener("change", detail);
      detail();
      for (const [category, label] of Object.entries(categories)) {
        dialogForm.elements.category.append(new Option(label, category));
      }
    });
  }

  function changeMemory(person, item, retract) {
    panel(retract ? "撤回记忆" : "修订记忆",
      retract ? "<p>撤回后，此人物的后续聊天将停止召回这条记忆。</p>" : field(["text", "记忆内容"]),
      retract ? "确认撤回" : "保存修订", form => run(async () => {
        await setup("change_memory", {persona_id: person.id, memory_id: item.id,
          text: retract ? null : form.get("text").trim()});
        showMemories(person);
      })
    );
    if (!retract) dialogForm.elements.text.value = item.text;
  }

  function changePolicy(person, policy) {
    const enabled = !policy.enabled;
    panel(enabled ? "开启自动记忆" : "关闭自动记忆",
      enabled ? "<p>PCAR 会按当前整理设置处理这个人物的新交流，可能使用已授权的模型并产生用量。</p>"
        : "<p>关闭后停止自动整理新记忆。已有的有效记忆可以继续查看和管理。</p>",
      enabled ? "确认开启" : "确认关闭", () => run(async () => {
        await setup("memory_policy", {persona_id: person.id, enabled, expected: policy.id, request_id: requestId()});
        showMemories(person);
      })
    );
  }

  function diagnostics() {
    load("高级设置", () => setup("diagnostics"), state => {
      panel("高级设置", '<ul id="diagnostic-summary" class="summary-list"></ul><div class="inline-actions" id="diagnostic-actions"></div>' +
        '<p class="hint">备份、迁移和维护可在退出本工作台后，通过「启动PCAR.bat」打开旧版工作台。</p>');
      const summary = byId("diagnostic-summary");
      summary.append(el("li", state.label), el("li", state.connection ? "模型连接已配置" : "模型连接待配置"),
        el("li", "待核对的本机消息 · " + state.unconfirmed),
        el("li", "投递待处理 · " + (state.pending ?? "暂未取得状态")));
      byId("diagnostic-actions").append(button("刷新", diagnostics), button("模型连接", configure));
    });
  }

  function guide(person, people, current) {
    const section = el("section", null, "onboarding");
    section.setAttribute("aria-label", "首次使用");
    section.innerHTML = '<h2>开始第一次聊天</h2><p>准备人物和模型连接，两项可以任选先后。</p>';
    const steps = el("div", null, "setup-steps");
    function step(symbol, title, text, action) {
      const row = el("div", null, "step");
      const stepIcon = el("span", null, "step-icon");
      stepIcon.innerHTML = icon(symbol);
      const copy = el("div", null, "step-copy");
      const detail = el("small", text);
      copy.append(el("strong", title), detail);
      row.append(stepIcon, copy, action);
      steps.append(row);
      return {detail, stepIcon};
    }
    step(person ? "check" : "person", "准备人物", person?.name ?? "从空白创建或导入角色",
      button(person ? "更换人物" : "新建角色", () => {
        if (!people.length) roleForm();
        else {
          panel("选择人物", '<div id="guide-picker" class="fields"></div>');
          const picker = byId("guide-picker");
          for (const item of people) picker.append(button(item.name, () => {
            dialog.close();
            history.select(item);
          }));
          picker.append(button("新建角色", roleForm, "primary"));
        }
      }, person ? "" : "primary"));
    const configureButton = button("配置连接", configure);
    const state = step("link", "连接模型", "正在读取本机配置…", configureButton);
    const start = button("开始聊天", history.start, "primary");
    start.disabled = true;
    const footer = el("div", null, "guide-actions");
    footer.append(start);
    section.append(steps, footer);
    timeline.classList.add("first");
    timeline.replaceChildren(section);
    setup("connection").then(value => {
      if (!current() || !section.isConnected) return;
      state.detail.textContent = value.migration_pending ? "还有迁移项目待确认"
        : value.configured ? "DeepSeek 连接已配置" : "设置基础聊天所需的 API Key";
      state.stepIcon.innerHTML = icon(value.configured ? "check" : "link");
      configureButton.textContent = value.configured ? "查看连接" : "配置连接";
      configureButton.classList.toggle("primary", !!person && !value.configured);
      start.disabled = !(person && value.configured && !value.migration_pending);
    }).catch(() => {
      if (current() && section.isConnected) state.detail.textContent = "暂时无法读取配置，可重新打开模型连接。";
    });
  }

  function intercept(target, handler) {
    target.addEventListener("click", event => {
      if (!history.active()) return;
      event.stopImmediatePropagation();
      handler();
    }, true);
  }

  intercept(byId("new-person"), roleForm);
  intercept(byId("profile-button"), () => showPerson());
  intercept(document.querySelector('[data-menu="connection"]'), configure);
  intercept(document.querySelector('[data-menu="advanced"]'), diagnostics);
  intercept(document.querySelector('[data-menu="help"]'), () => panel("使用帮助",
    "<p>从左侧打开人物或会话，在聊天区继续交流。首次使用可从聊天区配置模型连接和人物。</p>" +
    "<p>改设定、补充资料和管理记忆都在顶部「人物资料」。</p>"));
  dialog.addEventListener("cancel", event => { if (locked) event.preventDefault(); });
  dialog.addEventListener("close", () => { if (!unresolvedCreation) discardPackage(); });
  window.pcarSetup = Object.freeze({guide, connection: configure});
})();
