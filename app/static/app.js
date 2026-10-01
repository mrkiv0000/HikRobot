"use strict";

const form = document.getElementById("settings-form");
const cameraCount = document.getElementById("camera-count");
const expectedCodes = document.getElementById("expected-codes");
const collectionWindow = document.getElementById("collection-window");
const settingsContainer = document.getElementById("camera-settings");
const cameraList = document.getElementById("camera-list");
const dirtyMarker = document.getElementById("dirty-marker");
const saveMessage = document.getElementById("save-message");
const connectMessage = document.getElementById("connect-message");
const connectButton = document.getElementById("connect-button");
const disconnectButton = document.getElementById("disconnect-button");
const globalError = document.getElementById("global-error");
const logList = document.getElementById("log-list");

let savedSettings = null;
let latestState = null;
let connectionRequest = false;
let disconnectionRequest = false;

const statusLabels = {unused: "Не используется", disconnected: "Отключено", connecting: "Подключается", connected: "Подключено"};
const kindLabels = {
  data: "Входящие данные", connection: "Подключение", closed: "Закрытие", error: "Ошибка связи",
  assembly_error: "Ошибка сборки", parse_error: "Ошибка разбора", parsed: "Разбор", no_read: "NoRead", cycle: "Цикл",
};

function buildSettingsFields() {
  for (let index = 0; index < 4; index += 1) {
    const section = document.createElement("div");
    section.className = "config-camera";
    const heading = document.createElement("h3");
    heading.textContent = `Камера ${index + 1}`;
    const row = document.createElement("div");
    row.className = "config-row";
    const ipLabel = document.createElement("label");
    ipLabel.className = "field";
    ipLabel.textContent = "IP-адрес";
    const ip = document.createElement("input");
    ip.className = "camera-ip";
    ip.name = `camera_${index + 1}_ip`;
    ip.required = true;
    ip.setAttribute("aria-label", `IP-адрес камеры ${index + 1}`);
    ipLabel.appendChild(ip);
    const portLabel = document.createElement("label");
    portLabel.className = "field";
    portLabel.textContent = "Порт";
    const port = document.createElement("input");
    port.className = "camera-port";
    port.name = `camera_${index + 1}_port`;
    port.type = "number";
    port.min = "1";
    port.max = "65535";
    port.step = "1";
    port.required = true;
    port.setAttribute("aria-label", `Порт камеры ${index + 1}`);
    portLabel.appendChild(port);
    row.append(ipLabel, portLabel);
    section.append(heading, row);
    settingsContainer.appendChild(section);
  }
}

function buildCameraCards() {
  for (let index = 0; index < 4; index += 1) {
    const card = document.createElement("section");
    card.className = "camera";
    const title = document.createElement("div");
    title.className = "camera-title";
    title.append(document.createTextNode(`Камера ${index + 1}`));
    const protocol = document.createElement("span");
    protocol.className = "camera-num";
    protocol.textContent = "TCP";
    title.appendChild(protocol);
    const status = document.createElement("div");
    status.className = "status unused";
    const dot = document.createElement("span");
    dot.className = "dot";
    status.append(dot, document.createTextNode("Не используется"));
    const endpoint = document.createElement("div");
    endpoint.className = "endpoint";
    endpoint.textContent = "—";
    const error = document.createElement("div");
    error.className = "camera-error";
    error.hidden = true;
    card.append(title, status, endpoint, error);
    cameraList.appendChild(card);
  }
}

function settingsFromForm() {
  const ips = Array.from(document.querySelectorAll(".camera-ip"));
  const ports = Array.from(document.querySelectorAll(".camera-port"));
  return {
    camera_count: Number(cameraCount.value), expected_codes: Number(expectedCodes.value),
    collection_window: Number(collectionWindow.value),
    cameras: ips.map((input, index) => ({ip: input.value.trim(), port: Number(ports[index].value)})),
  };
}

function putSettingsInForm(settings) {
  cameraCount.value = String(settings.camera_count);
  expectedCodes.value = String(settings.expected_codes);
  collectionWindow.value = String(settings.collection_window);
  const ips = document.querySelectorAll(".camera-ip");
  const ports = document.querySelectorAll(".camera-port");
  settings.cameras.forEach((camera, index) => {
    ips[index].value = camera.ip;
    ports[index].value = String(camera.port);
  });
}

function updateDirtyMarker() {
  if (savedSettings) dirtyMarker.hidden = JSON.stringify(settingsFromForm()) === JSON.stringify(savedSettings);
}

function validateSettings(settings) {
  if (!Number.isInteger(settings.camera_count) || settings.camera_count < 1 || settings.camera_count > 4) throw new Error("Количество камер должно быть от 1 до 4.");
  if (!Number.isInteger(settings.expected_codes) || settings.expected_codes < 1 || settings.expected_codes > 4) throw new Error("Ожидаемое количество кодов должно быть от 1 до 4.");
  if (!Number.isFinite(settings.collection_window) || settings.collection_window <= 0) throw new Error("Окно сбора должно быть конечным положительным числом.");
  const ipv4 = /^(?:0|[1-9]\d{0,2})(?:\.(?:0|[1-9]\d{0,2})){3}$/;
  settings.cameras.forEach((camera, index) => {
    const parts = camera.ip.split(".");
    if (!ipv4.test(camera.ip) || parts.some((part) => Number(part) > 255)) throw new Error(`Камера ${index + 1}: требуется корректный IPv4-адрес.`);
    if (!Number.isInteger(camera.port) || camera.port < 1 || camera.port > 65535) throw new Error(`Камера ${index + 1}: порт должен быть целым числом 1–65535.`);
  });
}

async function api(url, options = {}) {
  const response = await fetch(url, {cache: "no-store", ...options});
  if (!response.ok) {
    let message = `Ошибка HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg).join("; ");
    } catch (_) { /* use HTTP status */ }
    throw new Error(message);
  }
  return response.json();
}

function formatTime(timestamp) {
  if (!timestamp) return "—";
  return new Date(timestamp).toLocaleTimeString("ru-RU", {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3});
}

function addCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = String(value);
  if (className) cell.className = className;
  row.appendChild(cell);
}

function renderState(state) {
  latestState = state;
  const current = settingsFromForm();
  const selected = state.cameras.filter((camera) => camera.selected).length;
  document.getElementById("selected-count").textContent = `Выбрано камер: ${selected}`;
  state.cameras.forEach((camera, index) => {
    const card = cameraList.children[index];
    const status = card.querySelector(".status");
    const endpoint = card.querySelector(".endpoint");
    const error = card.querySelector(".camera-error");
    status.className = `status ${camera.error ? "error" : camera.status}`;
    status.replaceChildren();
    const dot = document.createElement("span");
    dot.className = "dot";
    status.append(dot, document.createTextNode(statusLabels[camera.status] || camera.status));
    const shownEndpoint = camera.endpoint || current.cameras[index];
    endpoint.textContent = camera.status === "unused" ? "—" : `${shownEndpoint.ip} : ${shownEndpoint.port}`;
    error.textContent = camera.error || "";
    error.hidden = !camera.error;
  });
  connectButton.disabled = connectionRequest || state.connection_in_progress || state.disconnection_in_progress;
  disconnectButton.disabled = disconnectionRequest || state.disconnection_in_progress || !state.has_active_connections;
  disconnectButton.textContent = state.disconnection_in_progress || disconnectionRequest ? "Отключение…" : "Отключить все камеры";
  renderCycle(state.cycle);
  renderHistory(state.history);
  renderJournal(state.journal);
}

function renderCycle(cycle) {
  const kind = document.getElementById("last-kind");
  const source = document.getElementById("last-source");
  const summary = document.getElementById("last-summary");
  const detail = document.getElementById("last-detail");
  const time = document.getElementById("last-time");
  const count = document.getElementById("last-count");
  const empty = document.getElementById("last-empty");
  const table = document.getElementById("last-table");
  const body = document.getElementById("last-table-body");
  body.replaceChildren();
  table.hidden = true;
  empty.hidden = false;
  const windowNote = document.getElementById("cycle-window-note");
  if (!cycle || cycle.state === "waiting") {
    kind.className = "tag pending";
    kind.textContent = "Ожидание";
    source.textContent = "Цикл ещё не начат";
    summary.textContent = "Ожидание первого кода";
    detail.textContent = "Таймер запустится после первого корректного кода.";
    time.textContent = "—";
    windowNote.textContent = "Окно не запущено";
    count.textContent = `Получено 0 из ${cycle ? cycle.expected : "—"}`;
    empty.textContent = "Таблица заполнится после первого корректного кода.";
    return;
  }
  if (cycle.state === "waiting_no_read") {
    kind.className = "tag pending";
    kind.textContent = "Ожидание";
    source.textContent = "Цикл с кодами не открыт";
    summary.textContent = cycle.summary;
    detail.textContent = `NoRead: камеры ${cycle.no_read_cameras.join(", ")}. Таймаут ожидания не используется.`;
    time.textContent = "—";
    windowNote.textContent = "Ожидание остальных камер";
    count.textContent = `Получено 0 из ${cycle.expected}`;
    empty.textContent = "NoRead не является QR-кодом и не открывает окно сбора.";
    return;
  }

  const active = !cycle.completed;
  source.textContent = cycle.cycle_id ? `Цикл №${cycle.cycle_id} · начало ${formatTime(cycle.started_at)}` : "Завершённый результат";
  summary.textContent = cycle.summary;
  count.textContent = `Получено ${cycle.received} из ${cycle.expected}`;
  if (active) {
    const remaining = Math.max(0, Number(cycle.remaining));
    time.textContent = `${remaining.toLocaleString("ru-RU", {minimumFractionDigits: 1, maximumFractionDigits: 1})} с`;
    windowNote.textContent = `Окно ${Number(cycle.duration).toLocaleString("ru-RU")} с`;
    if (cycle.state === "excess" || cycle.blockers.length) {
      kind.className = "tag error-tag";
      kind.textContent = "Ошибка · сбор";
    } else {
      kind.className = "tag pending";
      kind.textContent = "Сбор";
    }
    const notes = [];
    if (cycle.blockers.length) notes.push(`Блокирующие причины: ${cycle.blockers.join("; ")}`);
    if (cycle.no_read_cameras.length) notes.push(`NoRead: камеры ${cycle.no_read_cameras.join(", ")}`);
    detail.textContent = notes.length ? notes.join(". ") : "Успех будет определён только после окончания полного окна.";
  } else {
    time.textContent = "Завершён";
    windowNote.textContent = cycle.completed_at ? formatTime(cycle.completed_at) : "Окно завершено";
    if (cycle.state === "success" && cycle.is_current) {
      kind.className = "tag success-tag";
      kind.textContent = "Успех";
      detail.textContent = "Количество соответствует, блокирующих ошибок нет.";
    } else if (cycle.state === "cancelled") {
      kind.className = "tag pending";
      kind.textContent = "Отменён";
      detail.textContent = cycle.reasons.join("; ");
    } else if (cycle.state === "success") {
      kind.className = "tag pending";
      kind.textContent = "Ранее завершён";
      detail.textContent = "Результат более не актуален после изменения готовности или нового события.";
    } else {
      kind.className = "tag error-tag";
      kind.textContent = "Ошибка";
      detail.textContent = cycle.reasons.join("; ");
    }
  }

  if (cycle.records.length) {
    cycle.records.forEach((record) => {
      const row = document.createElement("tr");
      addCell(row, record.id, "id-cell");
      addCell(row, `${record.sn} · ${record.side}`);
      addCell(row, record.a);
      addCell(row, `Камера ${record.camera}`);
      body.appendChild(row);
    });
    table.hidden = false;
    empty.hidden = true;
  } else {
    empty.textContent = cycle.state === "cancelled" ? "Набор отменённого цикла очищен." : "В завершённом результате нет QR-кодов.";
  }
}

function renderHistory(history) {
  const empty = document.getElementById("history-empty");
  const table = document.getElementById("history-table");
  const body = document.getElementById("history-table-body");
  body.replaceChildren();
  if (!history.length) {
    empty.hidden = false;
    table.hidden = true;
    return;
  }
  const groups = new Map();
  history.forEach((record) => {
    if (!groups.has(record.message_id)) groups.set(record.message_id, []);
    groups.get(record.message_id).push(record);
  });
  Array.from(groups.values()).reverse().flat().forEach((record) => {
    const row = document.createElement("tr");
    addCell(row, formatTime(record.timestamp));
    addCell(row, record.camera);
    addCell(row, record.id, "id-cell");
    addCell(row, `${record.sn} · ${record.side}`);
    addCell(row, record.a);
    addCell(row, record.x);
    addCell(row, record.y);
    body.appendChild(row);
  });
  empty.hidden = true;
  table.hidden = false;
}

function renderJournal(events) {
  logList.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Журнал пуст.";
    logList.appendChild(empty);
    return;
  }
  events.forEach((event) => {
    const row = document.createElement("div");
    const isError = ["error", "assembly_error", "parse_error"].includes(event.kind);
    row.className = `log-row ${isError ? "error" : event.kind === "data" ? "" : "event"}`;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatTime(event.timestamp);
    const source = document.createElement("span");
    source.textContent = event.source;
    const kind = document.createElement("span");
    kind.className = "log-kind";
    kind.textContent = kindLabels[event.kind] || event.kind;
    const message = document.createElement("span");
    message.textContent = event.kind === "data" ? `${event.byte_count} байт · ${event.message}` : event.message;
    row.append(time, source, kind, message);
    logList.appendChild(row);
  });
}

async function pollState() {
  try { renderState(await api("/api/state")); }
  catch (error) { connectMessage.textContent = `Сервер недоступен: ${error.message}`; }
}

form.addEventListener("input", updateDirtyMarker);
form.addEventListener("change", updateDirtyMarker);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const settings = settingsFromForm();
    validateSettings(settings);
    const result = await api("/api/settings", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(settings)});
    savedSettings = result.settings;
    putSettingsInForm(savedSettings);
    updateDirtyMarker();
    saveMessage.textContent = "Настройки сохранены. Ожидаемое количество и окно применятся к следующему циклу; подключения не изменены.";
    globalError.hidden = true;
  } catch (error) { saveMessage.textContent = `Ошибка: ${error.message}`; }
});

connectButton.addEventListener("click", async () => {
  try {
    const settings = settingsFromForm();
    validateSettings(settings);
    connectionRequest = true;
    connectButton.disabled = true;
    disconnectButton.disabled = false;
    connectMessage.textContent = "Выполняется подключение…";
    renderState(await api("/api/cameras/connect", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(settings)}));
    connectMessage.textContent = "Попытки подключения завершены. Проверьте статусы камер.";
  } catch (error) { connectMessage.textContent = `Ошибка: ${error.message}`; }
  finally {
    connectionRequest = false;
    if (latestState) renderState(latestState);
  }
});

disconnectButton.addEventListener("click", async () => {
  try {
    disconnectionRequest = true;
    disconnectButton.disabled = true;
    disconnectButton.textContent = "Отключение…";
    connectMessage.textContent = "Отключение камер…";
    renderState(await api("/api/cameras/disconnect", {method: "POST"}));
    connectMessage.textContent = "Все камеры отключены пользователем.";
  } catch (error) { connectMessage.textContent = `Ошибка: ${error.message}`; }
  finally {
    disconnectionRequest = false;
    if (latestState) renderState(latestState);
  }
});

const tabs = Array.from(document.querySelectorAll(".tab"));
function selectTab(tab) {
  tabs.forEach((item) => {
    const active = item === tab;
    item.setAttribute("aria-selected", String(active));
    document.getElementById(item.getAttribute("aria-controls")).hidden = !active;
  });
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    selectTab(tabs[next]);
    tabs[next].focus();
  });
});

async function initialize() {
  buildSettingsFields();
  buildCameraCards();
  try {
    const result = await api("/api/settings");
    savedSettings = result.settings;
    putSettingsInForm(savedSettings);
    updateDirtyMarker();
    if (result.load_error) {
      globalError.textContent = result.load_error;
      globalError.hidden = false;
    }
    await pollState();
    window.setInterval(pollState, 500);
  } catch (error) {
    globalError.textContent = `Не удалось загрузить приложение: ${error.message}`;
    globalError.hidden = false;
  }
}

initialize();
