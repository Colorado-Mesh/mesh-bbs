"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const dialog = byId("signin-dialog");
  const openButton = byId("signin-open");
  const signout = byId("signout");
  const form = byId("compose-form");
  const boardForm = byId("board-form");
  let creatingBoard = false;
  let user = null;
  let token = "";
  let draft = null;
  let submitting = false;
  let savedDraft = null;
  let draftConflict = false;
  const secure = location.protocol === "https:" || ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  const storage = (kind, action, key, value) => {
    try {
      return window[kind][action](key, value);
    } catch {
      return null;
    }
  };
  token = storage("sessionStorage", "getItem", "mesh-bbs-access") || "";
  const draftKey = form ? `mesh-bbs-draft:${form.dataset.board}:${form.dataset.parent}` : "";
  const operation = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, "0")).join("");
  const say = (text, error = false) => {
    if (!form) return;
    byId("compose-message").textContent = text;
    byId("compose-message").classList.toggle("error", error);
  };

  const api = async (path, payload, access = token) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {
        method: payload ? "POST" : "GET",
        credentials: "omit",
        headers: { Authorization: `Bearer ${access}`, ...(payload ? { "Content-Type": "application/json" } : {}) },
        body: payload ? JSON.stringify(payload) : undefined,
        signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(data.error || "The request could not be completed.");
        error.status = response.status;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timeout);
    }
  };

  const updateAccount = () => {
    byId("account-name").textContent = user ? user.actor : "Public reading";
    openButton.hidden = Boolean(user);
    signout.hidden = !user;
    if (boardForm) byId("create-board").disabled = creatingBoard || !secure || !user;
    if (!form) return;
    const wrongActor = draft.actor && user && draft.actor !== user.actor;
    const readOnly = form.dataset.board === "news";
    let message = user ? `Posting as ${user.actor}.` : "Sign in with your contributor key to publish.";
    if (!secure) message = "Use HTTPS, or open this host on localhost, to sign in and post.";
    else if (wrongActor) message = `This draft belongs to ${draft.actor}. Sign in as that contributor, or discard the draft.`;
    else if (readOnly) message = "News is read-only; automatic imports only.";
    byId("compose-access").textContent = message;
    byId("publish").disabled = submitting || draftConflict || !secure || !user || wrongActor || readOnly;
    byId("publish").textContent = submitting ? "Saving…" : draft.pending ? "Retry publication" : "Publish post";
    byId("post-title").readOnly = Boolean(draft.pending);
    byId("post-body").readOnly = Boolean(draft.pending);
    byId("post-format").disabled = Boolean(draft.pending) || draftConflict;
    byId("draft-clear").disabled = submitting || draftConflict || Boolean(draft.pending);
  };

  const saveDraft = () => {
    if (!form) return false;
    const current = storage("localStorage", "getItem", draftKey);
    if (current !== savedDraft) {
      draftConflict = true;
      say("Another tab changed this draft. Copy any text you want to keep, then reload to see the saved version.", true);
      updateAccount();
      return false;
    }
    if (!draft.pending) {
      draft.title = byId("post-title").value;
      draft.body = byId("post-body").value;
      if (user && !draft.actor) draft.actor = user.actor;
    }
    byId("post-format").value = draft.body.startsWith("#!micron\n") ? "micron" : "text";
    const bytes = new TextEncoder().encode(draft.body).length;
    byId("body-count").textContent = `${bytes.toLocaleString()} / 65,536 bytes`;
    byId("body-count").classList.toggle("error", bytes > 65536);
    const value = JSON.stringify(draft);
    const saved = storage("localStorage", "setItem", draftKey, value);
    if (saved !== null) savedDraft = value;
    byId("draft-state").textContent = saved === null ? "Browser storage unavailable; keep this page open" : "Draft saved in this browser";
    return true;
  };

  openButton.hidden = false;
  openButton.addEventListener("click", () => {
    byId("signin-error").textContent = secure ? "" : "Sign-in requires HTTPS or localhost.";
    dialog.showModal();
    byId("access-key").focus();
  });
  byId("signin-close").addEventListener("click", () => dialog.close());
  byId("signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!secure) return;
    const candidate = byId("access-key").value.trim();
    const submit = event.currentTarget.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      user = await api("/api/session", null, candidate);
      token = candidate;
      storage("sessionStorage", "setItem", "mesh-bbs-access", token);
      byId("access-key").value = "";
      dialog.close();
      updateAccount();
    } catch (error) {
      byId("signin-error").textContent = error.status ? error.message : "Could not reach this BBS. Try again when the connection returns.";
    } finally {
      submit.disabled = false;
    }
  });
  signout.addEventListener("click", () => {
    token = "";
    user = null;
    storage("sessionStorage", "removeItem", "mesh-bbs-access");
    updateAccount();
  });

  if (boardForm) boardForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!secure || !user || creatingBoard) return;
    creatingBoard = true;
    updateAccount();
    try {
      const result = await api("/api/boards", { board: byId("board-name").value.trim() });
      location.assign(`/new/${encodeURIComponent(result.board)}`);
    } catch (error) {
      byId("board-message").textContent = error.status ? error.message : "Connection lost. Retry with the same board name; it will not create a duplicate.";
    } finally {
      creatingBoard = false;
      updateAccount();
    }
  });

  if (form) {
    const fresh = () => ({ operation: operation(), title: byId("post-title").defaultValue, body: "", actor: "", pending: false });
    draft = fresh();
    try {
      savedDraft = storage("localStorage", "getItem", draftKey);
      const saved = JSON.parse(savedDraft);
      if (saved && /^[0-9a-f]{32}$/.test(saved.operation) && [saved.title, saved.body, saved.actor].every(v => typeof v === "string") && saved.body.length <= 65536) draft = saved;
    } catch { /* Leave malformed browser drafts untouched until the next edit. */ }
    byId("post-title").value = draft.title;
    byId("post-body").value = draft.body;
    byId("preview-toggle").hidden = false;
    byId("draft-clear").hidden = false;
    byId("post-format").addEventListener("change", () => {
      if (draft.pending || draftConflict) return;
      const body = byId("post-body");
      if (byId("post-format").value === "micron" && !body.value.startsWith("#!micron\n")) body.value = "#!micron\n" + body.value;
      else if (byId("post-format").value === "text" && body.value.startsWith("#!micron\n")) body.value = body.value.slice(9);
      saveDraft();
    });
    form.addEventListener("input", (event) => { if (event.target.id !== "post-format") saveDraft(); });
    let previewRequest = 0;
    byId("preview-toggle").addEventListener("click", async () => {
      const preview = byId("post-preview");
      const request = ++previewRequest;
      preview.hidden = !preview.hidden;
      byId("preview-toggle").textContent = preview.hidden ? "Preview" : "Hide preview";
      if (preview.hidden) return;
      const body = byId("post-body").value;
      const output = preview.querySelector("div");
      output.textContent = body || "Your message preview will appear here.";
      if (!body.startsWith("#!micron\n")) return;
      if (!user) { output.textContent = "Sign in to preview Micron formatting. Your draft is saved."; return; }
      try {
        const result = await api("/api/preview", { body });
        // Only server-rendered, escaped markup enters the DOM. Never insert post source.
        if (request === previewRequest && body === byId("post-body").value) {
          byId("micron-colors").textContent = result.css;
          output.innerHTML = result.html;
        }
      } catch (error) {
        if (request === previewRequest) output.textContent = "Preview unavailable. Your draft is saved. " + error.message;
      }
    });
    byId("draft-clear").addEventListener("click", () => {
      if (!confirm("Discard this browser draft?")) return;
      previewRequest++;
      draft = fresh();
      byId("post-title").value = draft.title;
      byId("post-body").value = "";
      byId("post-preview").hidden = true;
      byId("preview-toggle").textContent = "Preview";
      saveDraft();
      updateAccount();
      say("Draft discarded.");
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submitting || byId("publish").disabled) return;
      if (!saveDraft()) return;
      if (new TextEncoder().encode(draft.title).length > 256 || new TextEncoder().encode(draft.body).length > 65536) {
        say("Use a title up to 256 bytes and a message up to 65,536 bytes.", true);
        return;
      }
      const wasPending = Boolean(draft.pending);
      const submittingToken = token;
      draft.pending = true;
      saveDraft();
      submitting = true;
      updateAccount();
      say("Saving your post to this host…");
      try {
        const result = await api("/api/posts", { board: form.dataset.board, parent_id: form.dataset.parent, title: draft.title, body: draft.body, operation: draft.operation });
        if (storage("localStorage", "getItem", draftKey) === savedDraft) {
          storage("localStorage", "removeItem", draftKey);
        }
        location.assign(`/posts/${result.post_id}`);
      } catch (error) {
        if (error.status && error.status < 500) {
          draft.pending = wasPending;
          say(error.message, true);
          if (error.status === 401 && token === submittingToken) {
            user = null;
            token = "";
            storage("sessionStorage", "removeItem", "mesh-bbs-access");
          }
        } else {
          say("Delivery is not confirmed. Retry this unchanged post; the BBS will not create a duplicate if it already saved it.", true);
        }
        saveDraft();
        submitting = false;
        updateAccount();
      }
    });
    saveDraft();
    if (draft.pending) say("A previous publication was not confirmed. Retry it to recover the saved post.");
  }
  updateAccount();
  if (token && secure) {
    const checkingToken = token;
    api("/api/session").then((account) => {
      if (token !== checkingToken) return;
      user = account;
      updateAccount();
    }).catch((error) => {
      if (token !== checkingToken) return;
      if (error.status === 401) {
        token = "";
        storage("sessionStorage", "removeItem", "mesh-bbs-access");
      }
      say("Sign in again to continue your draft.", true);
    });
  }
})();
