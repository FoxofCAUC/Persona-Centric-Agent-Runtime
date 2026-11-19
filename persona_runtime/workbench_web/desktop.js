(() => {
  "use strict";

  const tasks = [
    ["运行状态", showStatus],
    ["收起到托盘", () => window.pywebview.api.hide_to_tray()],
    ["退出工作台", () => window.pywebview.api.exit_workbench()]
  ];

  async function showStatus() {
    present(
      "运行状态",
      '<p id="native-status" role="status">正在读取本机服务状态。</p>' +
      '<p class="hint">桌面聊天使用所选实例的本机服务。体验演示可从右上角进入。</p>'
    );
    const label = document.getElementById("native-status");
    try {
      const status = await window.pywebview.api.runtime_status();
      if (label.isConnected) label.textContent = status.label;
    } catch {
      if (label.isConnected) label.textContent = "暂时无法读取本机服务状态。";
    }
  }

  window.pcarDesktop = Object.freeze({ status: showStatus, notice: notify });

  function connect() {
    const menu = document.getElementById("app-menu");
    if (menu.dataset.desktopReady) return;
    menu.dataset.desktopReady = "true";
    for (const [label, action] of tasks) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "rail-row";
      button.textContent = label;
      button.addEventListener("click", async () => {
        showMenu(false);
        try {
          await action();
        } catch {
          notify("当前操作暂未完成，请重试。");
        }
      });
      menu.append(button);
    }
    window.pywebview.api.ready().catch(() => notify("窗口尚未就绪，请重新打开工作台。"));
  }

  window.addEventListener("pywebviewready", connect, { once: true });
  if (window.pywebview?.api) connect();
})();
