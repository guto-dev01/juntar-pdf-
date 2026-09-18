"use strict";

const PARALLEL_UPLOADS = 2;
const MAX_RETRIES = 10;
const STALL_TIMEOUT_MS = 120_000;
const JOB_STORAGE_KEY = "juntar-pdfs:ultima-juncao";
const UPLOAD_STORAGE_PREFIX = "juntar-pdfs:envio:";

const $ = (id) => document.getElementById(id);
const els = {
  dropzone: $("dropzone"),
  fileInput: $("file-input"),
  filesSection: $("files-section"),
  summary: $("summary"),
  overallBar: $("overall-bar"),
  overallText: $("overall-text"),
  fileList: $("file-list"),
  sortBtn: $("sort-btn"),
  clearBtn: $("clear-btn"),
  mergeSection: $("merge-section"),
  outputName: $("output-name"),
  compression: $("compression"),
  compressionHint: $("compression-hint"),
  mergeBtn: $("merge-btn"),
  job: $("job"),
  jobBar: $("job-bar"),
  jobText: $("job-text"),
  downloadLink: $("download-link"),
  toast: $("toast"),
  formats: $("formats"),
  template: $("file-template"),
};

const state = {
  chunkSize: 32 * 1024 * 1024,
  extensions: null, // o servidor informa quais tipos aceita
  items: [],
  job: null,
  jobOutdated: false,
  jobTimer: null,
};

// ------------------------------------------------------------------ utilidades

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  const digits = unit === 0 ? 0 : value < 10 ? 2 : 1;
  return `${value.toLocaleString("pt-BR", { maximumFractionDigits: digits })} ${units[unit]}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  seconds = Math.round(seconds);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${s} s`;
  return `${s} s`;
}

const plural = (n, one, many) => `${n.toLocaleString("pt-BR")} ${n === 1 ? one : many}`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// O localStorage pode estar bloqueado (janela anônima, políticas do navegador).
const storage = {
  get(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* segue sem retomar */ }
  },
  remove(key) {
    try { localStorage.removeItem(key); } catch { /* nada a fazer */ }
  },
};

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function errorMessage(status, text) {
  try {
    const data = JSON.parse(text);
    if (typeof data.detail === "string") return data.detail;
  } catch { /* resposta sem JSON */ }
  return status ? `Erro ${status} no servidor.` : "Sem conexão com o servidor.";
}

async function api(method, url, body) {
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new HttpError(0, "Sem conexão com o servidor.");
  }
  const text = await response.text();
  if (!response.ok) throw new HttpError(response.status, errorMessage(response.status, text));
  return text ? JSON.parse(text) : null;
}

// Erros em que repetir a mesma parte não resolve.
const isPermanent = (err) => [400, 401, 413, 422, 507].includes(err.status);

let toastTimer;
function toast(message) {
  els.toast.textContent = message;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 6000);
}

// ------------------------------------------------------------------ lista de arquivos

const fileKey = (file) => `${UPLOAD_STORAGE_PREFIX}${file.name}|${file.size}|${file.lastModified}`;
const extensionOf = (name) => (name.match(/\.[^.\/\\]+$/) || [""])[0].toLowerCase();
const isSupported = (file) => !state.extensions || state.extensions.includes(extensionOf(file.name));
const isPdf = (name) => extensionOf(name) === ".pdf";
const isLocked = () => state.job?.status === "queued" || state.job?.status === "running";
const isPending = (item) => ["waiting", "uploading", "verifying"].includes(item.status);

function addFiles(fileList) {
  if (isLocked()) {
    toast("Aguarde a junção terminar para adicionar arquivos.");
    return;
  }
  const files = [...fileList];
  const accepted = files.filter(isSupported);
  let duplicates = 0;
  for (const file of accepted) {
    const key = fileKey(file);
    if (state.items.some((item) => item.key === key)) {
      duplicates++;
      continue;
    }
    const item = {
      key, file, id: null, status: "waiting", received: 0, inflight: 0, pages: null,
      error: null, note: null, permanent: false, cancelled: false, xhr: null, rate: null, rateMark: null,
    };
    item.el = createItemElement(item);
    state.items.push(item);
  }

  const notes = [];
  if (files.length > accepted.length) {
    notes.push(`${plural(files.length - accepted.length, "arquivo ignorado", "arquivos ignorados")}: tipo não aceito`);
  }
  if (duplicates) notes.push(`${plural(duplicates, "arquivo já estava", "arquivos já estavam")} na lista`);
  if (notes.length) toast(`${notes.join(" · ")}.`);
  if (accepted.length > duplicates) listChanged();
  pump();
}

function createItemElement(item) {
  const el = els.template.content.firstElementChild.cloneNode(true);
  const name = el.querySelector(".file-name");
  name.textContent = item.file.name;
  name.title = item.file.name;
  el.querySelector(".up").addEventListener("click", () => move(item, -1));
  el.querySelector(".down").addEventListener("click", () => move(item, 1));
  el.querySelector(".retry").addEventListener("click", () => retryItem(item));
  el.querySelector(".remove").addEventListener("click", () => removeItem(item));
  el.addEventListener("dragstart", (event) => {
    if (isLocked()) return event.preventDefault();
    draggedItem = item;
    el.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", item.file.name);
  });
  el.addEventListener("dragend", () => {
    el.classList.remove("dragging");
    draggedItem = null;
    const order = [...els.fileList.children];
    const before = state.items.map((i) => i.key).join("\n");
    state.items.sort((a, b) => order.indexOf(a.el) - order.indexOf(b.el));
    if (state.items.map((i) => i.key).join("\n") !== before) listChanged();
    renderAll();
  });
  return el;
}

let draggedItem = null;

function move(item, delta) {
  const from = state.items.indexOf(item);
  const to = from + delta;
  if (isLocked() || to < 0 || to >= state.items.length) return;
  [state.items[from], state.items[to]] = [state.items[to], state.items[from]];
  listChanged();
  renderAll();
}

function sortByName() {
  if (isLocked()) return;
  const collator = new Intl.Collator("pt-BR", { numeric: true, sensitivity: "base" });
  state.items.sort((a, b) => collator.compare(a.file.name, b.file.name));
  listChanged();
  renderAll();
}

function removeItem(item) {
  if (isLocked()) return;
  item.cancelled = true;
  item.xhr?.abort();
  state.items.splice(state.items.indexOf(item), 1);
  item.el.remove();
  storage.remove(item.key);
  if (item.id) api("DELETE", `/api/uploads/${item.id}`).catch(() => {});
  listChanged();
  pump();
}

function retryItem(item) {
  item.status = "waiting";
  item.error = null;
  pump();
}

function clearAll() {
  if (isLocked()) return;
  if (!confirm("Remover todos os arquivos da lista e apagá-los do servidor?")) return;
  for (const item of state.items.splice(0)) {
    item.cancelled = true;
    item.xhr?.abort();
    item.el.remove();
    storage.remove(item.key);
    if (item.id) api("DELETE", `/api/uploads/${item.id}`).catch(() => {});
  }
  discardJob();
  renderAll();
}

// ------------------------------------------------------------------ envio em partes

function pump() {
  let active = state.items.filter((i) => i.status === "uploading" || i.status === "verifying").length;
  for (const item of state.items) {
    if (active >= PARALLEL_UPLOADS) break;
    if (item.status === "waiting") {
      active++;
      uploadItem(item);
    }
  }
  renderAll();
}

async function uploadItem(item) {
  item.status = "uploading";
  item.error = null;
  item.permanent = false;
  item.rate = null;
  item.rateMark = null;
  try {
    await openSession(item);
    let failures = 0;
    while (item.received < item.file.size) {
      if (item.cancelled) return;
      const end = Math.min(item.received + state.chunkSize, item.file.size);
      try {
        item.received = await sendChunk(item, item.received, end);
        failures = 0;
        item.note = null;
      } catch (err) {
        if (item.cancelled) return;
        if (isPermanent(err) || ++failures > MAX_RETRIES) throw err;
        item.note = `${err.message} Tentando de novo (${failures}/${MAX_RETRIES})…`;
        scheduleRender();
        await sleep(Math.min(30_000, 1000 * 2 ** (failures - 1)));
        if (item.cancelled) return;
        await resyncOffset(item);
      }
    }

    item.status = "verifying";
    item.note = null;
    scheduleRender();
    const info = await api("POST", `/api/uploads/${item.id}/complete`);
    if (item.cancelled) return;
    item.pages = info.pages;
    item.status = "done";
  } catch (err) {
    if (item.cancelled) return;
    item.status = "error";
    item.error = err.message;
    // 422: o servidor recusou o arquivo (não é PDF, tem senha…) e já o apagou.
    item.permanent = err.status === 422;
    if (err.status === 422 || err.status === 404) {
      storage.remove(item.key);
      item.id = null;
      item.received = 0;
    }
  } finally {
    item.inflight = 0;
    item.note = null;
    if (!item.cancelled) pump();
  }
}

// Reaproveita um envio anterior do mesmo arquivo (por exemplo, depois de recarregar a página).
async function openSession(item) {
  const savedId = item.id || storage.get(item.key);
  if (savedId) {
    try {
      const info = await api("GET", `/api/uploads/${savedId}`);
      if (info.size === item.file.size) {
        item.id = savedId;
        item.received = info.received;
        return;
      }
    } catch (err) {
      if (err.status !== 404) throw err;
    }
  }
  const info = await api("POST", "/api/uploads", { name: item.file.name, size: item.file.size });
  if (item.cancelled) {
    api("DELETE", `/api/uploads/${info.id}`).catch(() => {});
    return;
  }
  item.id = info.id;
  item.received = 0;
  storage.set(item.key, info.id);
}

// Depois de uma falha, pergunta ao servidor quantos bytes realmente chegaram.
async function resyncOffset(item) {
  try {
    const info = await api("GET", `/api/uploads/${item.id}`);
    item.received = info.received;
  } catch (err) {
    if (err.status === 404) {
      storage.remove(item.key);
      item.id = null;
      await openSession(item);
    }
    // Outros erros (servidor fora do ar): a próxima tentativa decide.
  }
}

function sendChunk(item, start, end) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let stalled = false;
    let watchdog;
    const arm = () => {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => { stalled = true; xhr.abort(); }, STALL_TIMEOUT_MS);
    };
    const finish = () => {
      clearTimeout(watchdog);
      item.xhr = null;
      item.inflight = 0;
    };

    item.xhr = xhr;
    xhr.open("PUT", `/api/uploads/${item.id}?offset=${start}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      arm();
      item.inflight = Math.min(event.loaded, end - start);
      scheduleRender();
    };
    xhr.onload = () => {
      finish();
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText).received);
      } else {
        reject(new HttpError(xhr.status, errorMessage(xhr.status, xhr.responseText)));
      }
    };
    xhr.onerror = () => {
      finish();
      reject(new HttpError(0, "A conexão foi interrompida."));
    };
    xhr.onabort = () => {
      finish();
      reject(new HttpError(0, stalled ? "A conexão parou de responder." : "Envio cancelado."));
    };
    arm();
    xhr.send(item.file.slice(start, end));
  });
}

// ------------------------------------------------------------------ junção

async function startMerge() {
  if (isLocked()) return;
  els.mergeBtn.disabled = true;
  const previous = state.job;
  try {
    const job = await api("POST", "/api/merge", {
      upload_ids: state.items.map((item) => item.id),
      output_name: els.outputName.value,
      compression: els.compression.value,
    });
    // O resultado anterior foi substituído: libera o espaço dele no servidor.
    if (previous) api("DELETE", `/api/jobs/${previous.id}`).catch(() => {});
    state.job = job;
    state.jobOutdated = false;
    storage.set(JOB_STORAGE_KEY, job.id);
    pollJob();
  } catch (err) {
    toast(err.message);
  }
  renderAll();
}

async function pollJob() {
  clearTimeout(state.jobTimer);
  const current = state.job;
  if (!isLocked()) return;
  try {
    const fresh = await api("GET", `/api/jobs/${current.id}`);
    if (state.job?.id !== current.id) return;
    state.job = fresh;
  } catch (err) {
    if (err.status === 404) {
      state.job = { ...current, status: "error", error: "A junção foi perdida (o servidor pode ter sido reiniciado). Clique em Juntar PDFs de novo." };
      storage.remove(JOB_STORAGE_KEY);
    }
    // Falha de rede passageira: tenta de novo no próximo ciclo.
  }
  renderAll();
  if (isLocked()) state.jobTimer = setTimeout(pollJob, 1000);
}

function discardJob() {
  if (state.job && !isLocked()) api("DELETE", `/api/jobs/${state.job.id}`).catch(() => {});
  clearTimeout(state.jobTimer);
  state.job = null;
  state.jobOutdated = false;
  storage.remove(JOB_STORAGE_KEY);
}

function listChanged() {
  if (state.job?.status === "done") state.jobOutdated = true;
}

// ------------------------------------------------------------------ desenho da tela

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderAll();
  });
}

function updateRate(item, now) {
  const bytes = item.received + item.inflight;
  if (!item.rateMark) {
    item.rateMark = { time: now, bytes };
    return;
  }
  const seconds = (now - item.rateMark.time) / 1000;
  if (seconds < 1) return;
  const instant = Math.max(0, (bytes - item.rateMark.bytes) / seconds);
  item.rate = item.rate == null ? instant : item.rate * 0.7 + instant * 0.3;
  item.rateMark = { time: now, bytes };
}

function renderItem(item, index, now, locked) {
  const el = item.el;
  const size = item.file.size;
  const bytes = Math.min(size, item.received + item.inflight);
  const percent = item.status === "done" ? 100 : (bytes / size) * 100;
  const count = state.items.length;

  el.querySelector(".position").textContent = index + 1;
  el.querySelector(".file-meta").textContent =
    formatBytes(size) + (item.pages != null ? ` · ${plural(item.pages, "página", "páginas")}` : "");

  const fill = el.querySelector(".fill");
  fill.style.width = `${percent}%`;
  fill.classList.toggle("done", item.status === "done");
  fill.classList.toggle("error", item.status === "error");

  let status;
  if (item.status === "uploading") {
    updateRate(item, now);
    status = item.note || `${Math.floor(percent)}% · ${formatBytes(bytes)} de ${formatBytes(size)}`;
    if (!item.note && item.rate > 0) {
      status += ` · ${formatBytes(item.rate)}/s · falta ${formatDuration((size - bytes) / item.rate)}`;
    }
  } else {
    status = {
      waiting: "Na fila para envio",
      verifying: isPdf(item.file.name) ? "Conferindo o PDF…" : "Convertendo para PDF…",
      done: "Enviado",
      error: item.error,
    }[item.status];
  }
  el.querySelector(".file-status").textContent = status;
  el.classList.toggle("error", item.status === "error");
  el.draggable = !locked;

  el.querySelector(".up").disabled = locked || index === 0;
  el.querySelector(".down").disabled = locked || index === count - 1;
  el.querySelector(".remove").disabled = locked;
  el.querySelector(".retry").hidden = !(item.status === "error" && !item.permanent);
}

function renderJob() {
  const job = state.job;
  els.job.hidden = !job;
  if (!job) return;

  let percent = 0;
  let text;
  const elapsed = formatDuration(Date.now() / 1000 - job.created);
  if (job.status === "queued") {
    text = "Aguardando outra junção terminar…";
  } else if (job.status === "running") {
    if (job.stage === "writing") {
      percent = job.percent;
      text = `Juntando arquivo ${job.current} de ${job.total}… ${job.percent}%`;
    } else if (job.stage === "verifying" || job.stage === "done") {
      percent = 100;
      text = "Conferindo o PDF final…";
    } else {
      text = "Preparando…";
    }
    text += ` (${elapsed})`;
  } else if (job.status === "done") {
    percent = 100;
    text = `Pronto! ${plural(job.pages, "página", "páginas")} · ${formatBytes(job.size)}`;
    const saved = job.input_bytes - job.size;
    if (saved > job.input_bytes * 0.02) {
      text += ` · ${Math.round((saved / job.input_bytes) * 100)}% menor que a soma dos originais`;
    }
    if (job.notice) text += ` · ${job.notice}`;
    if (state.jobOutdated) text += " · A lista mudou depois desta junção; junte de novo para atualizar.";
  } else {
    text = job.error;
  }

  els.jobBar.style.width = `${percent}%`;
  els.jobBar.classList.toggle("done", job.status === "done");
  els.jobBar.classList.toggle("error", job.status === "error");
  els.jobText.textContent = text;
  els.jobText.classList.toggle("error", job.status === "error");
  els.downloadLink.hidden = job.status !== "done";
  if (job.status === "done") {
    els.downloadLink.href = `/api/jobs/${job.id}/download`;
    els.downloadLink.textContent = `Baixar ${job.name}`;
  }
}

function renderAll() {
  const now = performance.now();
  const locked = isLocked();
  const items = state.items;

  items.forEach((item, index) => {
    if (els.fileList.children[index] !== item.el) {
      els.fileList.insertBefore(item.el, els.fileList.children[index] || null);
    }
    renderItem(item, index, now, locked);
  });

  els.filesSection.hidden = items.length === 0;
  els.mergeSection.hidden = items.length === 0 && !state.job;

  const totalBytes = items.reduce((sum, i) => sum + i.file.size, 0);
  const sentBytes = items.reduce(
    (sum, i) => sum + (i.status === "done" ? i.file.size : Math.min(i.file.size, i.received + i.inflight)), 0);
  const allDone = items.length > 0 && items.every((i) => i.status === "done");
  const pagesKnown = allDone && items.every((i) => i.pages != null);

  let summary = `${plural(items.length, "arquivo", "arquivos")} · ${formatBytes(totalBytes)}`;
  if (pagesKnown) summary += ` · ${plural(items.reduce((s, i) => s + i.pages, 0), "página", "páginas")}`;
  els.summary.textContent = summary;

  els.overallBar.style.width = `${totalBytes ? (sentBytes / totalBytes) * 100 : 0}%`;
  els.overallBar.classList.toggle("done", allDone);
  const errors = items.filter((i) => i.status === "error").length;
  if (allDone) {
    els.overallText.textContent = "Todos os arquivos foram enviados";
  } else {
    const rate = items.reduce((sum, i) => sum + (i.status === "uploading" && i.rate ? i.rate : 0), 0);
    let text = `${formatBytes(sentBytes)} de ${formatBytes(totalBytes)}`;
    if (rate > 0) text += ` · falta ${formatDuration((totalBytes - sentBytes) / rate)}`;
    if (errors) text += ` · ${plural(errors, "erro", "erros")}`;
    els.overallText.textContent = text;
  }

  els.sortBtn.disabled = locked;
  els.clearBtn.disabled = locked;
  els.compression.disabled = locked;
  els.compressionHint.textContent = COMPRESSION_HINTS[els.compression.value];
  els.mergeBtn.disabled = locked || items.length < 2 || !allDone;
  els.mergeBtn.textContent = locked ? "Juntando…" : items.length >= 2 ? `Juntar ${items.length} PDFs` : "Juntar PDFs";
  els.mergeBtn.title =
    items.length < 2 ? "Adicione pelo menos 2 PDFs"
    : errors ? "Remova os arquivos com erro (ou tente enviá-los de novo)"
    : !allDone ? "Aguarde o envio de todos os arquivos"
    : "";

  renderJob();
}

// ------------------------------------------------------------------ eventos

els.dropzone.addEventListener("click", () => els.fileInput.click());
els.dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    els.fileInput.click();
  }
});
els.fileInput.addEventListener("change", () => {
  addFiles(els.fileInput.files);
  els.fileInput.value = "";
});

// Arquivos soltos em qualquer lugar da página (e não abertos pelo navegador).
const carriesFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragover", (event) => {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  els.dropzone.classList.add("over");
});
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) els.dropzone.classList.remove("over");
});
document.addEventListener("drop", (event) => {
  els.dropzone.classList.remove("over");
  if (!carriesFiles(event)) return;
  event.preventDefault();
  addFiles(event.dataTransfer.files);
});

// Reordenação arrastando os itens da lista.
els.fileList.addEventListener("dragover", (event) => {
  if (!draggedItem) return;
  event.preventDefault();
  const target = event.target.closest(".file");
  if (!target || target === draggedItem.el) return;
  const rect = target.getBoundingClientRect();
  const after = event.clientY > rect.top + rect.height / 2;
  els.fileList.insertBefore(draggedItem.el, after ? target.nextSibling : target);
});
els.fileList.addEventListener("drop", (event) => {
  if (draggedItem) event.preventDefault();
});

const COMPRESSION_HINTS = {
  original: "As páginas são copiadas como estão. O arquivo final fica mais ou menos do tamanho da soma dos PDFs.",
  imagens: "As imagens são recomprimidas e as muito grandes são reduzidas; o texto e os desenhos não mudam. Documentos escaneados costumam ficar cerca de 5 vezes menores. Demora cerca de 1 minuto a cada 700 MB.",
};

els.compression.addEventListener("change", renderAll);
els.sortBtn.addEventListener("click", sortByName);
els.clearBtn.addEventListener("click", clearAll);
els.mergeBtn.addEventListener("click", startMerge);
els.outputName.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !els.mergeBtn.disabled) startMerge();
});

window.addEventListener("beforeunload", (event) => {
  if (state.items.some(isPending) || isLocked()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

async function init() {
  try {
    const config = await api("GET", "/api/config");
    state.chunkSize = config.chunk_size;
    state.extensions = config.extensions;
    els.fileInput.accept = config.extensions.join(",");
    els.formats.textContent = "PDF, fotos (JPG, PNG, HEIC…), Word, Excel, PowerPoint, texto e HTML";
  } catch { /* usa o tamanho padrão */ }

  const lastJob = storage.get(JOB_STORAGE_KEY);
  if (lastJob) {
    try {
      state.job = await api("GET", `/api/jobs/${lastJob}`);
      pollJob();
    } catch {
      storage.remove(JOB_STORAGE_KEY);
    }
  }
  renderAll();
}

init();
