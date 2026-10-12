const state = { schema: null, activeJob: null, poll: null, cloud: false, download: null };
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(element.timer);
  element.timer = setTimeout(() => element.classList.remove("show"), 2400);
}

function humanBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function selectedColumns() {
  return $$("[data-column]:checked").map(input => input.value);
}

function updatePlan() {
  const format = $("input[name=format]:checked").value.toUpperCase();
  const years = $("#years").value.trim() || "No years";
  const limit = Number($("#limit").value || 0);
  const count = selectedColumns().length;
  $("#planSummary").textContent = `${years} · ${limit ? `up to ${limit.toLocaleString()} rows` : "all matching rows"} · ${format} · ${count} columns`;
  $("#columnCount").textContent = `${count} of ${state.schema?.all_columns.length || 0} columns selected`;
}

function renderSchema(schema) {
  state.schema = schema;
  const defaults = new Set(schema.default_columns);
  $("#schemaGroups").innerHTML = schema.groups.map((group, index) => `
    <section class="schema-group">
      <button class="schema-summary" type="button" data-toggle-group="${group.id}">
        <span>${String(index + 1).padStart(2, "0")}</span>
        <div><strong>${group.label}</strong><small>${group.description}</small></div>
        <b data-group-count="${group.id}">0/${group.columns.length}</b>
      </button>
      <div class="schema-columns" data-group="${group.id}">
        ${group.columns.map(column => `<label class="column-option" title="${column}"><input data-column data-column-group="${group.id}" type="checkbox" value="${column}" ${defaults.has(column) ? "checked" : ""} /><span>${column}</span></label>`).join("")}
      </div>
    </section>`).join("");
  $$("[data-column]").forEach(input => input.addEventListener("change", updateGroupCounts));
  $$("[data-toggle-group]").forEach(button => button.addEventListener("click", () => {
    const group = $(`[data-group="${button.dataset.toggleGroup}"]`);
    group.hidden = !group.hidden;
  }));
  updateGroupCounts();
}

function updateGroupCounts() {
  state.schema.groups.forEach(group => {
    const count = $$(`[data-column-group="${group.id}"]:checked`).length;
    $(`[data-group-count="${group.id}"]`).textContent = `${count}/${group.columns.length}`;
  });
  updatePlan();
}

function selectColumnSet(mode) {
  const selected = new Set(mode === "recommended" ? state.schema.default_columns : mode === "all" ? state.schema.all_columns : []);
  $$("[data-column]").forEach(input => { input.checked = selected.has(input.value); });
  updateGroupCounts();
}

async function inspectYear() {
  const years = $("#years").value.trim();
  const first = years.match(/\d{4}/)?.[0];
  if (!first) return toast("Enter a valid year first");
  const panel = $("#yearInspector");
  panel.querySelector("strong").textContent = "Checking public index…";
  try {
    const info = await api(`/api/inspect?year=${first}`);
    panel.querySelector("span").textContent = `${info.year} metadata`;
    panel.querySelector("strong").textContent = `${info.file_count.toLocaleString()} records · ${info.total_size_human}`;
    panel.querySelector("button").textContent = "Available ✓";
  } catch (error) {
    panel.querySelector("strong").textContent = error.message;
    panel.querySelector("button").textContent = "Retry";
  }
}

function readConfig() {
  return {
    years: $("#years").value.trim(),
    query: $("#query").value.trim(),
    judge: $("#judge").value.trim(),
    citation: $("#citation").value.trim(),
    disposal: $("#disposal").value.trim(),
    language: $("#language").value.trim(),
    limit: Number($("#limit").value || 0),
    format: $("input[name=format]:checked").value,
    include_pdf_text: $("#includePdf").checked,
    columns: selectedColumns()
  };
}

async function startExtraction() {
  const config = readConfig();
  if (!config.years) return toast("Choose at least one year");
  if (!config.columns.length) return toast("Choose at least one output column");
  $("#runExtraction").disabled = true;
  try {
    if (state.cloud) return await startCloudExtraction(config);
    const job = await api("/api/extract", { method: "POST", body: JSON.stringify(config) });
    state.activeJob = job.id;
    showJob(job);
    clearInterval(state.poll);
    state.poll = setInterval(pollJob, 900);
    await pollJob();
  } catch (error) {
    toast(error.message);
    $("#runExtraction").disabled = false;
  }
}

async function startCloudExtraction(config) {
  showJob({ status: "running", phase: "download", message: "Fetching source metadata and preparing your download", rows: 0 });
  try {
    const response = await fetch("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config)
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `Extraction failed (${response.status})`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/)?.[1] || `supreme-court-export.${config.format}`;
    const rows = Number(response.headers.get("X-NyayaDesk-Rows") || 0);
    if (state.download?.url) URL.revokeObjectURL(state.download.url);
    state.download = { url: URL.createObjectURL(blob), filename };
    showJob({ status: "complete", phase: "complete", message: `${filename} · ${humanBytes(blob.size)}`, rows });
    $("#downloadResult").onclick = downloadCloudResult;
    downloadCloudResult();
    toast("Export ready—your download has started");
  } catch (error) {
    showJob({ status: "failed", phase: "failed", message: error.message, rows: 0 });
    toast(error.message);
  } finally {
    $("#runExtraction").disabled = false;
  }
}

function downloadCloudResult() {
  if (!state.download) return;
  const link = document.createElement("a");
  link.href = state.download.url;
  link.download = state.download.filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function showJob(job) {
  $("#emptyJob").hidden = true;
  $("#jobPanel").hidden = false;
  $("#jobStatus").textContent = job.status === "complete" ? "Export ready" : job.status === "failed" ? "Extraction failed" : "Extracting records";
  $("#jobMessage").textContent = job.message || job.status;
  $("#jobRows").textContent = Number(job.rows || 0).toLocaleString();
  $("#jobPhase").textContent = (job.phase || "queued").replaceAll("_", " ");
  const phaseWidth = { queued: 5, download: 28, extract: 68, complete: 100, failed: 100, cancelled: 100 }[job.phase] || 12;
  $("#jobProgress").style.width = `${phaseWidth}%`;
  const spinner = $("#jobSpinner");
  spinner.className = `spinner${job.status === "complete" ? " done" : job.status === "failed" || job.status === "cancelled" ? " failed" : ""}`;
  $("#downloadResult").hidden = job.status !== "complete";
  $("#cancelJob").hidden = !["queued", "running"].includes(job.status);
  if (job.download_url) $("#downloadResult").onclick = () => { window.location.href = job.download_url; };
}

async function pollJob() {
  if (!state.activeJob) return;
  try {
    const job = await api(`/api/jobs/${state.activeJob}`);
    showJob(job);
    if (["complete", "failed", "cancelled"].includes(job.status)) {
      clearInterval(state.poll);
      $("#runExtraction").disabled = false;
      if (job.status === "complete") { toast("Export ready to download"); loadFiles(); }
      if (job.status === "failed") toast(job.message);
    }
  } catch (error) {
    clearInterval(state.poll);
    $("#runExtraction").disabled = false;
    toast(error.message);
  }
}

async function cancelJob() {
  if (state.cloud) return toast("A live export finishes in the same request and cannot be cancelled");
  if (!state.activeJob) return;
  await api(`/api/jobs/${state.activeJob}/cancel`, { method: "POST", body: "{}" });
  toast("Cancelling after the current file operation");
}

async function loadFiles() {
  if (state.cloud) {
    $("#fileList").innerHTML = `<p class="muted">Live exports download directly to your device and are not retained on the server.</p>`;
    return;
  }
  try {
    const data = await api("/api/files");
    $("#fileList").innerHTML = data.files.length ? data.files.map(file => `<article class="file-item"><span>${file.name.split(".").pop().toUpperCase()}</span><div><strong>${file.name}</strong><small>${humanBytes(file.size)} · ${file.modified.replace("T", " ")}</small></div><a href="${file.download_url}">Download</a></article>`).join("") : `<p class="muted">No exports yet.</p>`;
  } catch (error) { toast(error.message); }
}

function resetForm() {
  $("#years").value = "2023";
  ["#query", "#judge", "#citation", "#disposal", "#language"].forEach(id => $(id).value = "");
  $("#limit").value = "100";
  $("#includePdf").checked = false;
  $("input[name=format][value=csv]").checked = true;
  $$(".format-card").forEach(card => card.classList.toggle("active", card.querySelector("input").checked));
  selectColumnSet("recommended");
}

async function initialize() {
  try {
    const schema = await api("/api/schema");
    state.cloud = Boolean(schema.cloud?.enabled);
    renderSchema(schema);
    if (state.cloud) configureCloudMode(schema.cloud);
  } catch (error) { toast(error.message); }
  $("#inspectYear").addEventListener("click", inspectYear);
  $("#runExtraction").addEventListener("click", startExtraction);
  $("#cancelJob").addEventListener("click", cancelJob);
  $("#refreshFiles").addEventListener("click", loadFiles);
  $("#recommendedColumns").addEventListener("click", () => selectColumnSet("recommended"));
  $("#allColumns").addEventListener("click", () => selectColumnSet("all"));
  $("#noColumns").addEventListener("click", () => selectColumnSet("none"));
  $("#resetForm").addEventListener("click", resetForm);
  $$("input, select").forEach(input => input.addEventListener("input", updatePlan));
  $$(".format-card input").forEach(input => input.addEventListener("change", () => {
    $$(".format-card").forEach(card => card.classList.toggle("active", card.querySelector("input").checked));
    updatePlan();
  }));
  updatePlan();
  loadFiles();
}

function configureCloudMode(cloud) {
  document.querySelector(".eyebrow").textContent = "LIVE SUPREME COURT EXTRACTION";
  document.querySelector(".hero > div > p").textContent = `Create a source-traceable CSV or JSONL directly from the public Supreme Court dataset. Live exports support ${cloud.max_years} years and ${cloud.max_rows.toLocaleString()} rows per run.`;
  const limit = $("#limit");
  limit.max = String(cloud.max_rows);
  limit.min = "1";
  limit.parentElement.querySelector("small").textContent = `Live limit: 1–${cloud.max_rows.toLocaleString()} rows per export.`;
  const sqlite = $('input[name="format"][value="sqlite"]');
  sqlite.disabled = true;
  sqlite.closest(".format-card").classList.add("disabled");
  sqlite.closest(".format-card").title = "SQLite is available in the local bulk extractor";
  const pdf = $("#includePdf");
  pdf.disabled = true;
  pdf.closest(".pdf-option").classList.add("disabled");
  pdf.closest(".pdf-option").title = "Full PDF extraction is available in the local bulk extractor";
  $("#refreshFiles").hidden = true;
  document.querySelector(".explainer ol").innerHTML = `<li><span>1</span><div><strong>Public yearly metadata</strong><small>Fetched from the source dataset for your chosen years.</small></div></li><li><span>2</span><div><strong>Only matching rows</strong><small>Filtered by the criteria you set above.</small></div></li><li><span>3</span><div><strong>A direct private download</strong><small>Generated for you and not retained on this server.</small></div></li>`;
}

document.addEventListener("DOMContentLoaded", initialize);
