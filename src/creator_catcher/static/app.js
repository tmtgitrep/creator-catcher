"use strict";
let config = null;
let lastStatusState = null;
const byId = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Request failed");
  return body;
}
function requestOptions(body) {
  return {method:"POST", headers:{"Content-Type":"application/json","X-Creator-Catcher":"1"}, body:JSON.stringify(body)};
}
function showMessage(text, error) {
  const message = byId("message");
  message.textContent = text;
  message.classList.toggle("error", Boolean(error));
}
function showHistoryMessage(text, error) {
  const message = byId("history-message");
  message.textContent = text;
  message.classList.toggle("error", Boolean(error));
}
function showVideoMessage(text, error) {
  const message = byId("video-message");
  message.textContent = text;
  message.classList.toggle("error", Boolean(error));
}
function creatorElement(creator, index) {
  const row = document.createElement("div");
  row.className = "creator";
  const enabled = document.createElement("input");
  enabled.type = "checkbox"; enabled.checked = creator.enabled; enabled.title = "Enable creator";
  enabled.addEventListener("change", () => { config.creators[index].enabled = enabled.checked; saveConfig(); });
  const name = document.createElement("input");
  name.value = creator.name; name.maxLength = 120; name.ariaLabel = "Creator name";
  name.addEventListener("change", () => { config.creators[index].name = name.value; saveConfig(); });
  const url = document.createElement("input");
  url.className = "url"; url.value = creator.url; url.ariaLabel = "Creator URL";
  url.addEventListener("change", () => { config.creators[index].url = url.value; saveConfig(); });
  const count = document.createElement("span");
  count.className = "download-count";
  const total = Number(creator.download_count || 0);
  const recent = Number(creator.last_scan_download_count || 0);
  count.textContent = `${total} total · ${recent} last scan`;
  count.title = "Videos downloaded since Creator Catcher 0.3.0";
  const remove = document.createElement("button");
  remove.type = "button"; remove.className = "danger"; remove.textContent = "Remove";
  remove.addEventListener("click", () => { config.creators.splice(index, 1); saveConfig(); });
  row.append(enabled, name, url, count, remove);
  return row;
}
function renderErrors(errors) {
  const details = byId("scan-errors");
  const list = byId("error-list");
  const entries = Array.isArray(errors) ? errors : [];
  list.replaceChildren();
  details.hidden = entries.length === 0;
  byId("error-summary").textContent = `${entries.length} ${entries.length === 1 ? "error" : "errors"} from the last scan`;
  entries.forEach((error) => {
    const item = document.createElement("li");
    const context = [error.creator, error.stage].filter(Boolean).join(" · ");
    item.textContent = `${context ? context + ": " : ""}${error.message || String(error)}`;
    list.append(item);
  });
  if (!entries.length) details.open = false;
}
function renderHistoryCreators() {
  const select = byId("history-creator");
  const previous = select.value;
  select.replaceChildren();
  config.creators.forEach((creator) => {
    const option = document.createElement("option");
    option.value = creator.url;
    option.textContent = creator.name;
    select.append(option);
  });
  if (config.creators.some((creator) => creator.url === previous)) select.value = previous;
  select.disabled = config.creators.length === 0;
  byId("history-download").disabled = config.creators.length === 0;
}
function renderConfig() {
  byId("download-dir").value = config.download_dir;
  byId("move-to-dir").value = config.move_to_dir || "";
  byId("lookback").value = String(config.lookback_days);
  byId("maximum").value = String(config.max_per_creator);
  byId("height").value = String(config.max_height);
  byId("automatic-enabled").checked = Boolean(config.automatic_scans_enabled);
  byId("automatic-interval").value = String(config.automatic_scan_interval_days);
  byId("automatic-time").value = config.automatic_scan_time;
  renderAutomaticScheduleState();
  const list = byId("creators");
  list.replaceChildren();
  if (!config.creators.length) {
    const empty = document.createElement("div");
    empty.className = "empty"; empty.textContent = "No creators saved yet."; list.append(empty);
  } else {
    config.creators.forEach((creator, index) => list.append(creatorElement(creator, index)));
  }
  renderHistoryCreators();
}
function renderAutomaticScheduleState() {
  const enabled = byId("automatic-enabled").checked;
  byId("automatic-interval").disabled = !enabled;
  byId("automatic-time").disabled = !enabled;
}
async function saveConfig() {
  try {
    config = await api("/api/config", requestOptions(config));
    renderConfig(); showMessage("Saved.", false);
  } catch (error) {
    showMessage(error.message, true);
    await loadConfig();
  }
}
async function loadConfig() { config = await api("/api/config"); renderConfig(); }
async function loadVersion() {
  const health = await api("/health");
  byId("app-version").textContent = `v${health.version}`;
}
async function pollStatus() {
  try {
    const status = await api("/api/status");
    byId("status-headline").textContent = status.headline || "Ready";
    byId("status-detail").textContent = status.detail || "";
    const progress = byId("progress");
    const runningStates = ["starting","checking","downloading","downloaded","moving"];
    const finishedStates = ["complete","error","idle"];
    if (["complete","error"].includes(status.state)) progress.value = Number(status.percent ?? 100);
    else if (status.state === "idle") progress.value = 0;
    else if (status.percent === null || status.percent === undefined) progress.removeAttribute("value");
    else progress.value = Number(status.percent);
    const wasRunning = runningStates.includes(lastStatusState);
    byId("scan").disabled = runningStates.includes(status.state);
    byId("history-download").disabled = runningStates.includes(status.state) || !config?.creators?.length;
    byId("video-download").disabled = runningStates.includes(status.state);
    renderErrors(status.errors);
    if (finishedStates.includes(status.state) && wasRunning) await loadConfig();
    lastStatusState = status.state;
  } catch (error) {
    byId("status-headline").textContent = "Connection lost";
    byId("status-detail").textContent = error.message;
  }
}
byId("add-creator").addEventListener("submit", async (event) => {
  event.preventDefault();
  config.creators.push({name:byId("new-name").value,url:byId("new-url").value,enabled:true});
  await saveConfig(); event.target.reset();
});
byId("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  config.download_dir = byId("download-dir").value;
  config.move_to_dir = byId("move-to-dir").value;
  config.lookback_days = Number(byId("lookback").value);
  config.max_per_creator = Number(byId("maximum").value);
  config.max_height = Number(byId("height").value);
  config.automatic_scans_enabled = byId("automatic-enabled").checked;
  config.automatic_scan_interval_days = Number(byId("automatic-interval").value);
  config.automatic_scan_time = byId("automatic-time").value;
  await saveConfig();
});
byId("automatic-enabled").addEventListener("change", renderAutomaticScheduleState);
byId("scan").addEventListener("click", async () => {
  byId("scan").disabled = true;
  byId("history-download").disabled = true;
  byId("video-download").disabled = true;
  try { await api("/api/scan", requestOptions({})); await pollStatus(); }
  catch (error) { showMessage(error.message, true); byId("scan").disabled = false; byId("history-download").disabled = false; byId("video-download").disabled = false; }
});
byId("video").addEventListener("submit", async (event) => {
  event.preventDefault();
  byId("scan").disabled = true;
  byId("history-download").disabled = true;
  byId("video-download").disabled = true;
  const videoUrl = byId("video-url").value;
  try {
    await api("/api/video", requestOptions({video_url:videoUrl}));
    showVideoMessage("Video download started.", false);
    event.target.reset();
    await pollStatus();
  } catch (error) {
    showVideoMessage(error.message, true);
    byId("scan").disabled = false;
    byId("history-download").disabled = !config?.creators?.length;
    byId("video-download").disabled = false;
  }
});
byId("history").addEventListener("submit", async (event) => {
  event.preventDefault();
  byId("scan").disabled = true;
  byId("history-download").disabled = true;
  byId("video-download").disabled = true;
  const creatorUrl = byId("history-creator").value;
  const years = Number(byId("history-years").value);
  try {
    await api("/api/history", requestOptions({creator_url:creatorUrl, years}));
    showHistoryMessage(`Started ${years}-year history download.`, false);
    await pollStatus();
  } catch (error) {
    showHistoryMessage(error.message, true);
    byId("scan").disabled = false;
    byId("history-download").disabled = !config?.creators?.length;
    byId("video-download").disabled = false;
  }
});
loadConfig().catch((error) => showMessage(error.message, true));
loadVersion().catch(() => { byId("app-version").textContent = ""; });
pollStatus();
setInterval(pollStatus, 1000);
