// DocIntel demo UI. Vanilla JS, no build step.
//
// SECURITY: extracted values come from untrusted documents (a PDF can contain "<script>" text and
// the model will faithfully extract it). Everything is rendered through h(), which only ever sets
// textContent, never innerHTML. The page's CSP blocks inline and third-party script as a backstop.

const MAX_BYTES = 25 * 1024 * 1024;
const ALLOWED_TYPES = ["application/pdf", "image/png", "image/jpeg"];
const POLL_MS = 2000;
const POLL_LIMIT = 30;
const TERMINAL = new Set(["COMPLETED", "FAILED"]);

const HINTS = {
  UPSTREAM_UNAVAILABLE: "The AI provider is busy or not configured. Retry in a minute, or use Async mode.",
  EXTRACTION_TIMEOUT: "Gemini did not answer within the 30 s sync deadline. Retry, or use Async mode.",
  SYNC_CAPACITY_EXHAUSTED: "All synchronous slots are busy. Retry shortly or use Async mode.",
  UNSUPPORTED_MEDIA_TYPE: "Only real PDF, PNG and JPEG files are accepted (the type is detected from the file's bytes).",
  PAYLOAD_TOO_LARGE: "The file is larger than 25 MB.",
  PDF_ENCRYPTED: "Password-protected PDFs are not supported.",
  PAGE_LIMIT_EXCEEDED: "The PDF has too many pages for this schema.",
  DEPENDENCY_UNAVAILABLE: "A backing service (database, storage or queue) is down. Check `docker compose ps`.",
};

// ----------------------------------------------------------------------------- tiny DOM helper
/** h("div", {class: "x", text: "y"}, [children]) -> element. Text is always textContent. */
function h(tag, props = {}, children = []) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = String(value);
    else if (key === "on") for (const [evt, fn] of Object.entries(value)) el.addEventListener(evt, fn);
    else el.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) el.append(child);
  return el;
}

const $ = (id) => document.getElementById(id);
const humanize = (key) => key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const pct = (x) => `${Math.round(x * 100)}%`;
const confidenceClass = (c) => (c >= 0.9 ? "bg-success" : c >= 0.7 ? "bg-warning" : "bg-danger");
const isExtracted = (v) => v !== null && typeof v === "object" && "value" in v && "confidence" in v;

// ----------------------------------------------------------------------------- health badge
async function refreshHealth() {
  const badge = $("health");
  try {
    const res = await fetch("/health/ready", { cache: "no-store" });
    const body = await res.json();
    const down = Object.entries(body.checks || {})
      .filter(([, c]) => c.status !== "ok")
      .map(([name]) => name);
    badge.className = `badge ${body.status === "ok" ? "text-bg-success" : "text-bg-warning"}`;
    badge.textContent = body.status === "ok" ? `API healthy · ${body.version}` : `Degraded: ${down.join(", ")} down`;
  } catch {
    badge.className = "badge text-bg-danger";
    badge.textContent = "API unreachable";
  }
}

// ----------------------------------------------------------------------------- schemas
async function loadSchemas() {
  const select = $("schema");
  try {
    const res = await fetch("/api/v1/schemas");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const schemas = await res.json();
    select.replaceChildren(
      ...schemas.map((s) => h("option", { value: s.schema_id, text: `${s.schema_id}  (${s.description.split(":")[0]})` })),
    );
    const describe = () => {
      const s = schemas.find((x) => x.schema_id === select.value);
      $("schema-help").textContent = s ? `${s.description} Critical fields: ${s.critical_fields.join(", ")}.` : "";
    };
    select.addEventListener("change", describe);
    describe();
  } catch (err) {
    select.replaceChildren(h("option", { value: "", text: "Could not load schemas" }));
    showClientError(`Could not load schemas from the API (${err.message}). Is the stack running?`);
  }
  updateSubmitState();
}

// ----------------------------------------------------------------------------- file selection
function selectedFile() {
  return $("file").files[0] || null;
}

function showClientError(message) {
  const box = $("client-error");
  box.textContent = message;
  box.classList.toggle("d-none", !message);
}

function onFileChosen() {
  const file = selectedFile();
  showClientError("");
  $("file-name").textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(0)} KB` : "";
  // Client-side checks are for fast feedback only; the server sniffs bytes and is authoritative.
  if (file && file.size > MAX_BYTES) showClientError("This file is larger than 25 MB and will be rejected.");
  else if (file && file.type && !ALLOWED_TYPES.includes(file.type))
    showClientError(`"${file.type}" doesn't look like a PDF, PNG or JPEG. The server will check the actual bytes.`);
  renderPreview(file);
  updateSubmitState();
}

let previewUrl = null;
function renderPreview(file) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = file ? URL.createObjectURL(file) : null;
  const card = $("preview-card");
  const box = $("preview");
  card.classList.toggle("d-none", !file);
  if (!file) return box.replaceChildren();
  const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
  box.replaceChildren(
    isPdf
      ? h("iframe", { class: "preview-frame", src: previewUrl, title: "PDF preview" })
      : h("img", { class: "preview-image rounded", src: previewUrl, alt: "Selected document" }),
  );
}

function setupDropzone() {
  const zone = $("dropzone");
  ["dragenter", "dragover"].forEach((evt) =>
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.add("dragover");
    }),
  );
  ["dragleave", "drop"].forEach((evt) =>
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.remove("dragover");
    }),
  );
  zone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) {
      $("file").files = e.dataTransfer.files;
      onFileChosen();
    }
  });
}

function updateSubmitState() {
  $("submit").disabled = !(selectedFile() && $("schema").value);
}

function setBusy(busy, label) {
  $("submit").disabled = busy || !(selectedFile() && $("schema").value);
  $("submit-spinner").classList.toggle("d-none", !busy);
  $("submit-label").textContent = label;
}

// ----------------------------------------------------------------------------- submit
let pollTimer = null;

async function onSubmit(event) {
  event.preventDefault();
  const file = selectedFile();
  if (!file || !$("schema").value) return;
  clearTimeout(pollTimer);

  const mode = document.querySelector('input[name="mode"]:checked').value;
  const form = new FormData();
  form.append("file", file);
  form.append("schema_id", $("schema").value);

  const headers = { "X-Request-Id": crypto.randomUUID() };
  const clientId = $("client-id").value.trim();
  if (clientId) headers["X-Client-Id"] = clientId;

  setBusy(true, mode === "sync" ? "Extracting… (Gemini can take a few seconds)" : "Uploading…");
  $("result").replaceChildren(h("div", { class: "text-secondary text-center py-5", text: "Working…" }));
  const started = performance.now();
  try {
    const res = await fetch(`/api/v1/extract?mode=${mode}`, { method: "POST", body: form, headers });
    const body = await res.json().catch(() => ({}));
    const elapsed = Math.round(performance.now() - started);
    if (!res.ok) renderProblem(body, res.status, elapsed);
    else if (mode === "sync") renderResult(body, elapsed);
    else renderJob(body, headers["X-Client-Id"]);
  } catch (err) {
    renderProblem({ title: "Network error", detail: err.message }, 0, null);
  } finally {
    setBusy(false, "Extract");
  }
}

// ----------------------------------------------------------------------------- rendering: errors
function renderProblem(problem, status, elapsedMs) {
  const hint = HINTS[problem.code];
  $("result").replaceChildren(
    h("div", { class: "alert alert-danger mb-3" }, [
      h("div", { class: "d-flex justify-content-between align-items-start gap-2" }, [
        h("h5", { class: "alert-heading mb-1", text: `${status || ""} ${problem.title || "Request failed"}`.trim() }),
        problem.code ? h("span", { class: "badge text-bg-danger", text: problem.code }) : null,
      ]),
      problem.detail ? h("p", { class: "mb-1", text: problem.detail }) : null,
      hint ? h("p", { class: "mb-1 small", text: hint }) : null,
      h("div", {
        class: "small text-secondary",
        text: [problem.request_id && `request_id ${problem.request_id}`, elapsedMs && `${elapsedMs} ms`]
          .filter(Boolean)
          .join(" · "),
      }),
    ]),
    rawJson(problem),
  );
}

// ----------------------------------------------------------------------------- rendering: sync result
function stat(label, value, extra) {
  return h("div", { class: "col-6 col-md-3" }, [
    h("div", { class: "stat" }, [
      h("div", { class: "label", text: label }),
      h("div", { class: "value", text: value }),
      extra ? h("div", { class: "small text-secondary", text: extra }) : null,
    ]),
  ]);
}

function confidenceBar(confidence) {
  const bar = h("div", { class: `progress-bar ${confidenceClass(confidence)}` });
  bar.style.width = pct(confidence); // CSSOM, allowed by the CSP (unlike style attributes)
  return h("div", { class: "d-flex align-items-center gap-2" }, [
    h("div", { class: "progress confidence flex-grow-1", role: "progressbar", "aria-label": "confidence" }, [bar]),
    h("span", { class: "small text-secondary", text: pct(confidence) }),
  ]);
}

function valueCell(extracted) {
  return extracted.value === null || extracted.value === undefined
    ? h("span", { class: "null-value", text: "not found" })
    : h("span", { class: "field-value", text: String(extracted.value) });
}

function fieldsTable(data, lowConfidence) {
  const rows = Object.entries(data)
    .filter(([, v]) => isExtracted(v))
    .map(([name, v]) =>
      h("tr", { class: lowConfidence.has(name) ? "table-warning" : undefined }, [
        h("th", { scope: "row", class: "fw-normal text-secondary", text: humanize(name) }),
        h("td", {}, [valueCell(v)]),
        h("td", {}, [confidenceBar(v.confidence)]),
        h("td", { class: "text-secondary small", text: v.page ?? "–" }),
      ]),
    );
  return h("table", { class: "table table-sm align-middle mb-0" }, [
    h("thead", {}, [
      h("tr", {}, ["Field", "Value", "Confidence", "Page"].map((t) => h("th", { scope: "col", text: t }))),
    ]),
    h("tbody", {}, rows),
  ]);
}

function listSection(name, items) {
  const title = h("h6", { class: "mt-4", text: `${humanize(name)} (${items.length})` });
  if (!items.length) return h("div", {}, [title, h("p", { class: "small text-secondary", text: "None found." })]);
  // A list of bare extracted values (e.g. generic_document.parties) or of objects of them.
  const rows = items.map((item) => (isExtracted(item) ? { value: item } : item));
  const columns = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  return h("div", {}, [
    title,
    h("div", { class: "table-responsive" }, [
      h("table", { class: "table table-sm table-striped align-middle" }, [
        h("thead", {}, [h("tr", {}, columns.map((c) => h("th", { scope: "col", text: humanize(c) })))]),
        h(
          "tbody",
          {},
          rows.map((r) =>
            h(
              "tr",
              {},
              columns.map((c) => {
                const cell = r[c];
                if (!isExtracted(cell)) return h("td", { text: cell ?? "" });
                return h("td", {}, [
                  valueCell(cell),
                  h("span", { class: `badge ms-2 ${confidenceClass(cell.confidence)}`, text: pct(cell.confidence) }),
                ]);
              }),
            ),
          ),
        ),
      ]),
    ]),
  ]);
}

function rawJson(body) {
  const text = JSON.stringify(body, null, 2);
  const copy = h("button", { class: "btn btn-sm btn-outline-secondary", type: "button", text: "Copy JSON" });
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(text);
    copy.textContent = "Copied";
  });
  const download = h("button", { class: "btn btn-sm btn-outline-secondary", type: "button", text: "Download" });
  download.addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
    h("a", { href: url, download: `${body.job_id || "docintel-response"}.json` }).click();
    URL.revokeObjectURL(url);
  });
  return h("details", { class: "mt-4" }, [
    h("summary", { class: "small text-secondary", text: "Raw JSON response" }),
    h("div", { class: "d-flex gap-2 my-2" }, [copy, download]),
    h("pre", { class: "raw-json rounded p-3 mb-0", text }),
  ]);
}

function renderResult(body, elapsedMs) {
  const score = body.document_validity_score;
  const lowConfidence = new Set(body.low_confidence_fields || []);
  const scoreBar = h("div", { class: `progress-bar ${confidenceClass(score)}` });
  scoreBar.style.width = pct(score);
  const lists = Object.entries(body.data).filter(([, v]) => Array.isArray(v));

  $("result").replaceChildren(
    h("div", { class: "d-flex justify-content-between align-items-center mb-2" }, [
      h("h5", { class: "mb-0", text: `${body.schema_id} · ${body.status}` }),
      h("span", { class: "badge text-bg-light border", text: body.model }),
    ]),
    h("div", { class: "mb-3" }, [
      h("div", { class: "d-flex justify-content-between small" }, [
        h("span", { text: `Document validity ${pct(score)}` }),
        h("span", { class: "text-secondary", text: `model self-reported ${pct(body.model_reported_validity)}` }),
      ]),
      h("div", { class: "progress", role: "progressbar", "aria-label": "validity" }, [scoreBar]),
    ]),
    h("div", { class: "row g-2 mb-3" }, [
      stat("Total time", `${(elapsedMs / 1000).toFixed(1)} s`, `Gemini ${body.timings_ms.gemini_ms} ms`),
      stat("Cost", `$${Number(body.usage.estimated_cost_usd).toFixed(4)}`, "estimated"),
      stat("Input tokens", body.usage.input_tokens.toLocaleString()),
      stat("Output + thinking", (body.usage.output_tokens + body.usage.thinking_tokens).toLocaleString(),
        `${body.usage.thinking_tokens} thinking`),
    ]),
    body.warnings.length
      ? h("div", { class: "alert alert-warning py-2" }, [
          h("div", { class: "fw-semibold mb-1", text: `${body.warnings.length} validation warning(s)` }),
          h(
            "ul",
            { class: "mb-0 small" },
            body.warnings.map((w) =>
              h("li", {}, [
                h("span", { class: "badge text-bg-warning me-2", text: w.code }),
                h("span", { text: `${w.field ? `${w.field}: ` : ""}${w.detail}` }),
              ]),
            ),
          ),
        ])
      : h("div", { class: "alert alert-success py-2 small", text: "All business-rule checks passed." }),
    lowConfidence.size
      ? h("p", { class: "small text-secondary", text: `Highlighted rows are below the confidence threshold: ${[...lowConfidence].join(", ")}.` })
      : null,
    h("div", { class: "table-responsive" }, [fieldsTable(body.data, lowConfidence)]),
    ...lists.map(([name, items]) => listSection(name, items)),
    rawJson(body),
  );
}

// ----------------------------------------------------------------------------- rendering: async job
function renderJob(submit, clientId) {
  const status = h("span", { class: "badge text-bg-info", text: submit.status });
  const log = h("ul", { class: "small text-secondary mb-0" }, [h("li", { text: `${new Date().toLocaleTimeString()} · ${submit.status}` })]);
  $("result").replaceChildren(
    h("div", { class: "d-flex justify-content-between align-items-center mb-3" }, [
      h("h5", { class: "mb-0", text: "Job queued" }),
      status,
    ]),
    h("dl", { class: "row small mb-3" }, [
      h("dt", { class: "col-sm-3", text: "Job id" }),
      h("dd", { class: "col-sm-9 font-monospace", text: submit.job_id }),
      h("dt", { class: "col-sm-3", text: "Status URL" }),
      h("dd", { class: "col-sm-9 font-monospace", text: submit.status_url }),
    ]),
    h("div", {
      class: "alert alert-info small",
      text: "The document is validated, stored encrypted and queued. The worker that processes async jobs " +
        "arrives in Phase 4, so the status stays STAGED for now. Use Sync mode to see an extraction.",
    }),
    h("h6", { text: "Status history" }),
    log,
    rawJson(submit),
  );

  let polls = 0;
  let last = submit.status;
  const poll = async () => {
    polls += 1;
    try {
      const res = await fetch(submit.status_url, { headers: clientId ? { "X-Client-Id": clientId } : {} });
      const job = await res.json();
      if (res.ok && job.status !== last) {
        last = job.status;
        status.textContent = job.status;
        log.append(h("li", { text: `${new Date().toLocaleTimeString()} · ${job.status}` }));
      }
      if (res.ok && TERMINAL.has(job.status)) return;
    } catch {
      /* transient; keep polling */
    }
    if (polls < POLL_LIMIT) pollTimer = setTimeout(poll, POLL_MS);
  };
  pollTimer = setTimeout(poll, POLL_MS);
}

// ----------------------------------------------------------------------------- boot
$("file").addEventListener("change", onFileChosen);
$("schema").addEventListener("change", updateSubmitState);
$("upload-form").addEventListener("submit", onSubmit);
setupDropzone();
loadSchemas();
refreshHealth();
setInterval(refreshHealth, 15000);
