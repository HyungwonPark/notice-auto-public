// 작업 공지 안내 — small page helpers, no libraries.
(function () {
  "use strict";
  const ACTOR_KEY = "csp.actor";

  function csrf() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }
  function storedActor() {
    try { return localStorage.getItem(ACTOR_KEY) || ""; } catch (e) { return ""; }
  }
  function actor() { return storedActor(); }

  // The site has one shared login, so each person types their name where it is asked; the last name typed
  // is kept in this browser and filled into the next name field.
  document.querySelectorAll("input[data-actor-field]").forEach(function (f) {
    if (!f.value) f.value = storedActor();
    if (f.type === "hidden") return;
    f.addEventListener("input", function () {
      try { localStorage.setItem(ACTOR_KEY, f.value.trim()); } catch (e) { /* storage blocked */ }
    });
  });
  // A name is required: an empty name field is marked and the browser says what is missing.
  function needName(input) {
    input.setAttribute("aria-invalid", "true");
    input.setCustomValidity("확인한 사람 이름을 입력하세요");
    input.reportValidity();
    input.focus();
    input.addEventListener("input", function clear() {
      input.removeAttribute("aria-invalid");
      input.setCustomValidity("");
      input.removeEventListener("input", clear);
    });
  }
  document.querySelectorAll("form[data-needs-actor]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      const field = form.querySelector("input[name=actor]");
      if (field && !field.value.trim()) { field.value = actor(); }
      if (field && !field.value.trim()) {
        ev.preventDefault();
        const visible = field.type === "hidden" ? form.querySelector("input[data-actor-field]:not([type=hidden])") : field;
        if (visible) { needName(visible); } else { ev.preventDefault(); }
      }
    });
  });

  // Forms marked data-warn-unsaved ask before the page is left with unsaved edits.
  document.querySelectorAll("form[data-warn-unsaved]").forEach(function (form) {
    let dirty = false;
    form.addEventListener("input", function () { dirty = true; });
    form.addEventListener("submit", function () { dirty = false; });
    window.addEventListener("beforeunload", function (ev) {
      if (dirty) { ev.preventDefault(); ev.returnValue = ""; }
    });
  });

  // Copy buttons. Plain HTTP is not a secure context, so the Clipboard API is often unavailable; there a copy
  // event fills the clipboard (works even for a field inside a closed <details>), and failing that the text is
  // selected for Ctrl+C.
  async function copyText(el) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(el.value);
      return true;
    }
    const fill = function (ev) { ev.clipboardData.setData("text/plain", el.value); ev.preventDefault(); };
    document.addEventListener("copy", fill);
    try {
      if (document.execCommand("copy")) return true;
    } finally {
      document.removeEventListener("copy", fill);
    }
    el.focus();
    el.select();
    return false;
  }
  // Formatted copy (the 별도 발송 draft with its box): HTML for the mail editor, plain text for anything else.
  // Without the Clipboard API (plain HTTP) a copy event fills both formats.
  async function copyRich(html, text) {
    if (navigator.clipboard && window.isSecureContext && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([text], { type: "text/plain" }),
      })]);
      return true;
    }
    const fill = function (ev) {
      ev.clipboardData.setData("text/html", html);
      ev.clipboardData.setData("text/plain", text);
      ev.preventDefault();
    };
    document.addEventListener("copy", fill);
    try { return document.execCommand("copy"); } finally { document.removeEventListener("copy", fill); }
  }
  document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      const target = document.getElementById(btn.dataset.copyTarget);
      const note = document.getElementById(btn.dataset.copyTarget + "-note");
      const rich = btn.dataset.copyHtml && document.getElementById(btn.dataset.copyHtml);
      let ok = false;
      try { ok = rich ? await copyRich(rich.innerHTML, target.value) : await copyText(target); } catch (e) { ok = false; }
      note.textContent = ok ? (rich ? "표 포함으로 복사했습니다" : "복사했습니다") : "선택했습니다. Ctrl+C로 복사하세요";
      if (!btn.dataset.recordUrl) return;
      const body = new URLSearchParams({ what: btn.dataset.what || "", actor: actor() });
      const r = await fetch(btn.dataset.recordUrl, {
        method: "POST", body: body, headers: { "X-CSRFToken": csrf() }, credentials: "same-origin",
      });
      if (r.ok) {
        const data = await r.json();
        const tag = document.getElementById("status-tag");
        if (tag) { tag.textContent = data.label; tag.className = "tag tag-" + data.status; }
      }
    });
  });

  // Select-all for 담당자 확인 checkboxes, one per table (사전 공지, 완료 공지).
  document.querySelectorAll("[data-select-all]").forEach(function (all) {
    all.addEventListener("change", function () {
      all.closest("table").querySelectorAll("input[name=ids]:not(:disabled)").forEach(function (c) { c.checked = all.checked; });
    });
  });

  // Light/Dark. Until one is chosen the page follows the system setting; the choice is kept in this browser.
  const root = document.documentElement;
  const systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function showTheme() {
    const current = root.getAttribute("data-theme") || (systemDark && systemDark.matches ? "dark" : "light");
    document.querySelectorAll("[data-theme-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.themeSet === current));
    });
  }
  document.querySelectorAll("[data-theme-set]").forEach(function (b) {
    b.addEventListener("click", function () {
      root.setAttribute("data-theme", b.dataset.themeSet);
      try { localStorage.setItem("csp.theme", b.dataset.themeSet); } catch (e) { /* storage blocked */ }
      showTheme();
    });
  });
  if (systemDark && systemDark.addEventListener) systemDark.addEventListener("change", showTheme);
  showTheme();

  // "새 메일 확인": blocked during the cooldown, then the page reloads once to show the result.
  const scan = document.getElementById("scan-button");
  if (scan) {
    const wait = parseInt(scan.dataset.cooldown || "10", 10);
    const label = scan.querySelector("span");
    scan.form.addEventListener("submit", function () { scan.disabled = true; label.textContent = "요청 중…"; });
    if (new URLSearchParams(window.location.search).get("requested")) {
      let left = wait;
      scan.disabled = true;
      label.textContent = "확인 중… " + left + "초";
      const tick = setInterval(function () {
        left -= 1;
        label.textContent = "확인 중… " + left + "초";
        if (left <= 0) { clearInterval(tick); window.location.replace(window.location.pathname); }
      }, 1000);
    }
  }

  // A row that stands for one 안내 or one 작업 opens from any cell (계정, 대상 서버, …), not only from its name.
  // Links, checkboxes and buttons in the row keep their own job, and selecting text to copy does not navigate.
  document.querySelectorAll("tr[data-href]").forEach(function (row) {
    const open = function (ev, newTab) {
      if (ev.target.closest("a, button, input, label, select, textarea, td.check")) return;
      if (String(window.getSelection())) return;
      if (newTab) { window.open(row.dataset.href, "_blank", "noopener"); } else { window.location.href = row.dataset.href; }
    };
    row.addEventListener("click", function (ev) { open(ev, ev.ctrlKey || ev.metaKey || ev.shiftKey); });
    row.addEventListener("auxclick", function (ev) { if (ev.button === 1) open(ev, true); });
  });

  // Sign-in day line: put the 지금 marker at this PC's time, and keep it moving while the page is open.
  const dayline = document.querySelector("[data-dayline]");
  if (dayline) {
    const marker = dayline.querySelector("[data-dayline-now]");
    const clock = dayline.querySelector("[data-dayline-time]");
    const label = marker.firstElementChild;
    const two = function (n) { return (n < 10 ? "0" : "") + n; };
    const place = function () {
      const now = new Date();
      const share = (now.getHours() * 60 + now.getMinutes()) / 1440;
      clock.textContent = two(now.getHours()) + ":" + two(now.getMinutes());
      marker.style.left = share * 100 + "%";
      marker.hidden = false;
      // Near either end the label hangs inward instead of past the line.
      const x = share * marker.parentElement.clientWidth, half = label.offsetWidth / 2;
      marker.classList.toggle("near-start", x < half);
      marker.classList.toggle("near-end", marker.parentElement.clientWidth - x < half);
    };
    place();
    setInterval(place, 30000);
    window.addEventListener("resize", place);
  }

  // Sorting: every table header sorts its column; a second click reverses. A cell's data-sort-value (an ISO
  // date, a status order) wins over its text; numbers ("3명", "12건") compare as numbers; empty cells go last.
  const collator = new Intl.Collator("ko", { numeric: true, sensitivity: "base" });
  function sortKey(cell) {
    if (!cell) return "";
    const v = cell.dataset.sortValue !== undefined ? cell.dataset.sortValue : cell.textContent;
    return v.replace(/\s+/g, " ").trim();
  }
  function compare(a, b) {
    if (a === "" || b === "") return a === b ? 0 : (a === "" ? 1 : -1);
    const na = /^-?\d[\d,]*(\.\d+)?(\D{0,2})$/.test(a) ? parseFloat(a.replace(/,/g, "")) : NaN;
    const nb = /^-?\d[\d,]*(\.\d+)?(\D{0,2})$/.test(b) ? parseFloat(b.replace(/,/g, "")) : NaN;
    if (!isNaN(na) && !isNaN(nb)) return na - nb;
    return collator.compare(a, b);
  }
  document.querySelectorAll("table").forEach(function (table) {
    if (table.hasAttribute("data-no-sort") || !table.tHead || !table.tHead.rows.length) return;
    const heads = Array.prototype.slice.call(table.tHead.rows[0].cells);
    heads.forEach(function (th, col) {
      if (th.hasAttribute("data-no-sort") || th.querySelector("input, button") || !th.textContent.trim()) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sort";
      while (th.firstChild) button.appendChild(th.firstChild);
      th.appendChild(button);
      th.setAttribute("aria-sort", "none");
      button.addEventListener("click", function () {
        const asc = th.getAttribute("aria-sort") !== "ascending";
        heads.forEach(function (h) { if (h.hasAttribute("aria-sort")) h.setAttribute("aria-sort", "none"); });
        th.setAttribute("aria-sort", asc ? "ascending" : "descending");
        Array.prototype.forEach.call(table.tBodies, function (body) {
          const rows = Array.prototype.filter.call(body.rows, function (r) { return !r.querySelector("td.empty"); });
          rows.sort(function (a, b) {
            const ka = sortKey(a.cells[col]), kb = sortKey(b.cells[col]);
            if (ka === "" || kb === "") return compare(ka, kb);  // empty last both ways
            return asc ? compare(ka, kb) : compare(kb, ka);
          });
          rows.forEach(function (r) { body.appendChild(r); });
        });
      });
    });
  });

  // Quiet entrance for blocks marked .reveal (CSS only animates when motion is allowed).
  const blocks = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { rootMargin: "0px 0px -40px 0px" });
    blocks.forEach(function (b, i) { b.style.setProperty("--i", String(i % 6)); io.observe(b); });
  } else {
    blocks.forEach(function (b) { b.classList.add("in"); });
  }
})();
