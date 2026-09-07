"use strict";
let config = null;
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
  const remove = document.createElement("button");
  remove.type = "button"; remove.className = "danger"; remove.textContent = "Remove";
  remove.addEventListener("click", () => { config.creators.splice(index, 1); saveConfig(); });
  row.append(enabled, name, url, remove);
  return row;
}
function renderConfig() {
  byId("download-dir").value = config.download_dir;
  byId("lookback").value = String(config.lookback_days);
  byId("maximum").value = String(config.max_per_creator);
  byId("height").value = String(config.max_height);
  const list = byId("creators");
  list.replaceChildren();
  if (!config.creators.length) {
    const empty = document.createElement("div");
    empty.className = "empty"; empty.textContent = "No creators saved yet."; list.append(empty);
  } else {
    config.creators.forEach((creator, index) => list.append(creatorElement(creator, index)));
  }
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
async function pollStatus() {
  try {
    const status = await api("/api/status");
    byId("status-headline").textContent = status.headline || "Ready";
    byId("status-detail").textContent = status.detail || "";
    const progress = byId("progress");
    if (status.percent === null || status.percent === undefined) progress.removeAttribute("value");
    else progress.value = Number(status.percent);
    byId("scan").disabled = ["checking","downloading"].includes(status.state);
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
  config.lookback_days = Number(byId("lookback").value);
  config.max_per_creator = Number(byId("maximum").value);
  config.max_height = Number(byId("height").value);
  await saveConfig();
});
byId("scan").addEventListener("click", async () => {
  byId("scan").disabled = true;
  try { await api("/api/scan", requestOptions({})); await pollStatus(); }
  catch (error) { showMessage(error.message, true); byId("scan").disabled = false; }
});
loadConfig().catch((error) => showMessage(error.message, true));
pollStatus();
setInterval(pollStatus, 1000);
