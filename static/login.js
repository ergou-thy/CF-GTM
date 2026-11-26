"use strict";

(() => {
  const form = document.getElementById("loginForm");
  const password = document.getElementById("pwd");
  const button = document.getElementById("loginButton");
  const error = document.getElementById("err");
  const toggle = document.getElementById("togglePassword");
  let busy = false;
  let redirecting = false;
  const idleNodes = Array.from(button.childNodes);

  toggle.addEventListener("click", () => {
    const showing = password.type === "password";
    password.type = showing ? "text" : "password";
    toggle.textContent = showing ? "隐藏" : "显示";
    toggle.setAttribute("aria-label", showing ? "隐藏密码" : "显示密码");
    toggle.setAttribute("aria-pressed", String(showing));
    password.focus();
  });
  password.addEventListener("input", () => { password.removeAttribute("aria-invalid"); error.textContent = ""; });
  window.addEventListener("offline", () => { if (!busy) error.textContent = "网络已离线，请恢复连接后登录。"; });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    if (!password.value) { error.textContent = "请输入面板密码。"; password.focus(); return; }
    if (!navigator.onLine) { error.textContent = "网络已离线，请恢复连接后登录。"; return; }
    busy = true;
    button.disabled = true; password.readOnly = true; toggle.disabled = true;
    button.setAttribute("aria-busy", "true"); button.textContent = "正在验证身份…";
    error.textContent = "";
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/login", {
        method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || "" },
        body: JSON.stringify({ password: password.value }),
      });
      let result;
      try { result = await response.json(); } catch { throw new Error("服务器响应异常，请刷新页面后重试。"); }
      if (!response.ok || result?.ok !== true) {
        const fallback = response.status === 403 ? "安全校验未通过，请刷新页面后重试。" : response.status === 429 ? "尝试次数较多，请稍后再试。" : response.status === 401 ? "密码不正确，请重新输入。" : "登录未成功，请稍后重试。";
        throw new Error(typeof result?.msg === "string" && result.msg ? result.msg : fallback);
      }
      password.value = "";
      redirecting = true;
      button.textContent = "登录成功，正在进入…";
      location.assign("/");
    } catch (failure) {
      error.textContent = failure.name === "AbortError" ? "连接超时，登录结果未确认，请重试。" : failure instanceof TypeError ? "无法连接服务，请检查网络后重试。" : failure.message;
      password.setAttribute("aria-invalid", "true");
      password.readOnly = false; password.focus(); password.select();
    } finally {
      clearTimeout(timeout);
      if (!redirecting) {
        busy = false;
        button.disabled = false; password.readOnly = false; toggle.disabled = false;
        button.removeAttribute("aria-busy"); button.replaceChildren(...idleNodes);
      }
    }
  });
})();
