"""Servidor para enviar PDFs grandes em partes e juntá-los em um único arquivo.

Fluxo:
  1. POST /api/uploads                 cria o envio de um arquivo
  2. PUT  /api/uploads/{id}?offset=N    recebe uma parte (pode ser retomado)
  3. POST /api/uploads/{id}/complete    confere o PDF e conta as páginas
  4. POST /api/merge                    junta os arquivos na ordem enviada
  5. GET  /api/jobs/{id}                acompanha o andamento
  6. GET  /api/jobs/{id}/download       baixa o PDF final
"""

import asyncio
import hmac
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from app import formats

MIB = 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = Path(os.environ.get("PDF_DATA_DIR", BASE_DIR / "dados")).resolve()
UPLOADS_DIR = DATA_DIR / "envios"
OUTPUTS_DIR = DATA_DIR / "resultados"

CHUNK_SIZE = int(os.environ.get("PDF_CHUNK_MB", "32")) * MIB
MAX_REQUEST_BYTES = CHUNK_SIZE * 2
WRITE_BUFFER_BYTES = 4 * MIB
DISK_RESERVE_BYTES = 512 * MIB
RETENTION_SECONDS = float(os.environ.get("PDF_RETENCAO_HORAS", "24")) * 3600
INSPECT_TIMEOUT_SECONDS = 180
CONVERT_TIMEOUT_SECONDS = 900

ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# Com a chave definida, só entra quem abriu o link com ?chave=... (ou digitou a chave).
ACCESS_KEY = os.environ.get("PDF_CHAVE_ACESSO", "")
ACCESS_COOKIE = "juntar_pdfs_acesso"
ACCESS_COOKIE_SECONDS = 30 * 24 * 3600
ACCESS_PAGE = (Path(__file__).resolve().parent / "acesso.html").read_text()

jobs: dict[str, dict] = {}
busy_uploads: set[str] = set()
background_tasks: set[asyncio.Task] = set()
merge_slot = asyncio.Semaphore(1)


# ---------------------------------------------------------------- utilidades


def format_bytes(value):
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def free_disk_bytes():
    return shutil.disk_usage(DATA_DIR).free


def check_id(value):
    if not ID_PATTERN.match(value):
        raise HTTPException(404, "Não encontrado.")
    return value


def write_json(path, data):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def safe_pdf_name(name):
    name = re.sub(r'[\x00-\x1f"<>:|?*/\\]', "-", name)[:200].strip(". ")
    if not name.lower().endswith(".pdf"):
        name = (name or "documento-unido") + ".pdf"
    return name


def upload_dir(upload_id):
    return UPLOADS_DIR / check_id(upload_id)


def upload_data_path(upload_id):
    return upload_dir(upload_id) / "arquivo.pdf"


def read_upload(upload_id):
    meta_path = upload_dir(upload_id) / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except FileNotFoundError:
        raise HTTPException(404, "Envio não encontrado. Ele pode ter expirado.")
    data = upload_data_path(upload_id)
    meta["received"] = data.stat().st_size if data.exists() else 0
    return meta


def active_jobs():
    return [job for job in jobs.values() if job["status"] in ("queued", "running")]


def uploads_in_use():
    return {upload_id for job in active_jobs() for upload_id in job["upload_ids"]}


async def run_worker(*args, stdin=None, timeout=None):
    """Executa app.worker e devolve (código de saída, mensagens JSON, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.worker", *args,
        cwd=BASE_DIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, parse_messages(out.decode().splitlines()), err.decode(errors="replace")


def parse_messages(lines):
    messages = []
    for line in lines:
        try:
            messages.append(json.loads(line))
        except ValueError:
            pass
    return messages


# ---------------------------------------------------------------- limpeza


def newest_mtime(path):
    if path.is_dir():
        return max((p.stat().st_mtime for p in path.iterdir()), default=path.stat().st_mtime)
    return path.stat().st_mtime


def cleanup_expired(protected_uploads, protected_jobs):
    """Apaga envios e resultados parados há mais tempo que a retenção."""
    cutoff = time.time() - RETENTION_SECONDS
    removed_jobs = set()
    for folder, protected in ((UPLOADS_DIR, protected_uploads), (OUTPUTS_DIR, protected_jobs)):
        for path in folder.iterdir():
            item_id = path.name.split(".")[0]
            try:
                if item_id in protected or newest_mtime(path) >= cutoff:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                if folder == OUTPUTS_DIR:
                    removed_jobs.add(item_id)
            except FileNotFoundError:
                pass
    return removed_jobs


async def cleanup_loop():
    while True:
        # Os conjuntos são copiados aqui, no loop de eventos, e não dentro da thread.
        protected_uploads = uploads_in_use() | set(busy_uploads)
        protected_jobs = {job["id"] for job in active_jobs()}
        try:
            removed = await asyncio.to_thread(cleanup_expired, protected_uploads, protected_jobs)
            for job_id in removed:
                jobs.pop(job_id, None)
        except OSError as exc:
            print(f"Falha na limpeza automática: {exc}", file=sys.stderr)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    # Junções interrompidas por um reinício do servidor.
    for leftover in OUTPUTS_DIR.glob("*.tmp"):
        leftover.unlink(missing_ok=True)
    cleaner = asyncio.create_task(cleanup_loop())
    yield
    cleaner.cancel()


app = FastAPI(title="Juntar PDFs", lifespan=lifespan)


def same_secret(candidate, secret):
    return hmac.compare_digest(candidate.encode(), secret.encode())


class AccessKeyMiddleware:
    """Exige a chave de acesso quando PDF_CHAVE_ACESSO está definida.

    Middleware ASGI puro (e não @app.middleware) para não atrapalhar o envio e o
    download de arquivos grandes em fluxo contínuo.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not ACCESS_KEY:
            return await self.app(scope, receive, send)
        request = Request(scope)
        if same_secret(request.cookies.get(ACCESS_COOKIE, ""), ACCESS_KEY):
            return await self.app(scope, receive, send)

        key = request.query_params.get("chave", "")
        if key and same_secret(key, ACCESS_KEY):
            # Guarda a chave em um cookie e tira ela do endereço.
            response = RedirectResponse(request.url.path, status_code=303)
            response.set_cookie(
                ACCESS_COOKIE, ACCESS_KEY, max_age=ACCESS_COOKIE_SECONDS,
                httponly=True, samesite="lax", secure=request.url.scheme == "https",
            )
        elif request.url.path.startswith("/api/"):
            response = JSONResponse({"detail": "Acesso negado: abra o link completo de acesso."}, 401)
        else:
            response = HTMLResponse(ACCESS_PAGE, 401)
        await response(scope, receive, send)


app.add_middleware(AccessKeyMiddleware)


# ---------------------------------------------------------------- envios


class NewUpload(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    size: int = Field(gt=0)


def public_upload(meta):
    return {key: meta.get(key) for key in ("id", "name", "size", "received", "completed", "pages")}


@app.get("/api/config")
async def config():
    return {
        "chunk_size": CHUNK_SIZE,
        "free_bytes": free_disk_bytes(),
        "extensions": sorted(formats.SUPPORTED_EXTENSIONS),
    }


@app.post("/api/uploads")
async def create_upload(body: NewUpload):
    free = free_disk_bytes()
    if body.size + DISK_RESERVE_BYTES > free:
        raise HTTPException(
            507,
            f"Espaço em disco insuficiente para “{body.name}”: "
            f"precisa de {format_bytes(body.size)}, há {format_bytes(free)} livres.",
        )
    upload_id = uuid.uuid4().hex
    folder = UPLOADS_DIR / upload_id
    folder.mkdir(parents=True)
    upload_data_path(upload_id).touch()
    meta = {
        "id": upload_id,
        "name": body.name,
        "size": body.size,
        "completed": False,
        "pages": None,
        "created": time.time(),
    }
    write_json(folder / "meta.json", meta)
    return public_upload({**meta, "received": 0})


@app.get("/api/uploads/{upload_id}")
async def get_upload(upload_id: str):
    return public_upload(read_upload(upload_id))


@app.put("/api/uploads/{upload_id}")
async def upload_chunk(upload_id: str, offset: int, request: Request):
    meta = read_upload(upload_id)
    if meta["completed"]:
        raise HTTPException(409, "Este arquivo já foi enviado por completo.")
    if upload_id in busy_uploads:
        raise HTTPException(409, "Já há uma parte deste arquivo sendo recebida.")
    if offset != meta["received"]:
        raise HTTPException(409, f"Posição incorreta: o servidor já tem {meta['received']} bytes.")

    limit = min(meta["size"] - offset, MAX_REQUEST_BYTES)
    declared = request.headers.get("content-length")
    if declared is not None and int(declared) > limit:
        raise HTTPException(413, "A parte enviada é maior do que o permitido.")

    path = upload_data_path(upload_id)
    busy_uploads.add(upload_id)
    written = 0
    try:
        with path.open("ab") as file:
            buffer = bytearray()
            try:
                async for part in request.stream():
                    if written + len(buffer) + len(part) > limit:
                        file.truncate(offset)
                        raise HTTPException(413, "A parte enviada é maior do que o permitido.")
                    buffer += part
                    if len(buffer) >= WRITE_BUFFER_BYTES:
                        await asyncio.to_thread(file.write, buffer)
                        written += len(buffer)
                        buffer = bytearray()
            except ClientDisconnect:
                # Guarda o que chegou; o navegador pergunta a posição e continua dali.
                pass
            if buffer:
                await asyncio.to_thread(file.write, buffer)
                written += len(buffer)
    except OSError as exc:
        with suppress(OSError):
            os.truncate(path, offset)
        raise HTTPException(507, f"Não foi possível gravar no disco do servidor: {exc.strerror}.")
    finally:
        busy_uploads.discard(upload_id)
    return {"received": offset + written}


@app.post("/api/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str):
    meta = read_upload(upload_id)
    if meta["completed"]:
        return public_upload(meta)
    if upload_id in busy_uploads:
        raise HTTPException(409, "O arquivo ainda está sendo recebido.")
    if meta["received"] != meta["size"]:
        raise HTTPException(
            409, f"Envio incompleto: {meta['received']} de {meta['size']} bytes recebidos."
        )

    path = upload_data_path(upload_id)
    suffix = formats.extension(meta["name"])
    with path.open("rb") as file:
        is_pdf = b"%PDF-" in file.read(1024)
    if not is_pdf and suffix not in formats.CONVERTIBLE_EXTENSIONS:
        shutil.rmtree(upload_dir(upload_id), ignore_errors=True)
        raise HTTPException(
            422,
            f"“{meta['name']}” não é um PDF e não é de um tipo que dá para converter.",
        )

    busy_uploads.add(upload_id)
    try:
        if is_pdf:
            code, messages, _stderr = await run_worker(
                "inspect", str(path), timeout=INSPECT_TIMEOUT_SECONDS
            )
        else:
            # Imagens e documentos viram PDF aqui; daí em diante tudo é PDF.
            converted = path.with_name("convertido.pdf")
            code, messages, _stderr = await run_worker(
                "convert", str(path), str(converted), meta["name"], timeout=CONVERT_TIMEOUT_SECONDS
            )
            if code == 0 and converted.exists():
                os.replace(converted, path)
            else:
                converted.unlink(missing_ok=True)
        result = messages[-1] if messages else {}
        if code != 0 or "error" in result:
            shutil.rmtree(upload_dir(upload_id), ignore_errors=True)
            detail = result.get("error", "O arquivo não pôde ser lido.")
            raise HTTPException(422, f"“{meta['name']}”: {detail}")
        meta["pages"] = result.get("pages")
    except asyncio.TimeoutError:
        if not is_pdf:
            shutil.rmtree(upload_dir(upload_id), ignore_errors=True)
            raise HTTPException(422, f"“{meta['name']}”: a conversão demorou demais.")
        # PDFs muito danificados demoram para abrir; a junção ainda vai tentar.
        meta["pages"] = None
    finally:
        busy_uploads.discard(upload_id)

    meta["completed"] = True
    write_json(upload_dir(upload_id) / "meta.json", {k: v for k, v in meta.items() if k != "received"})
    return public_upload(meta)


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(upload_id: str):
    folder = upload_dir(upload_id)
    if upload_id in uploads_in_use():
        raise HTTPException(409, "O arquivo está sendo usado em uma junção em andamento.")
    shutil.rmtree(folder, ignore_errors=True)
    return {"deleted": True}


# ---------------------------------------------------------------- junção


class MergeRequest(BaseModel):
    upload_ids: list[str] = Field(min_length=1)
    output_name: str = Field(default="documento-unido.pdf", max_length=300)
    # "original": só compacta o que dá sem perder nada.
    # "imagens": recomprime as imagens, deixando o arquivo bem menor.
    compression: Literal["original", "imagens"] = "original"


def public_job(job):
    return {k: v for k, v in job.items() if k != "upload_ids"}


def find_job(job_id):
    check_id(job_id)
    job = jobs.get(job_id)
    if job is None:
        saved = OUTPUTS_DIR / f"{job_id}.json"
        if not saved.exists():
            raise HTTPException(404, "Junção não encontrada. Ela pode ter expirado.")
        job = jobs[job_id] = json.loads(saved.read_text())
    return job


@app.post("/api/merge")
async def start_merge(body: MergeRequest):
    uploads = [read_upload(upload_id) for upload_id in body.upload_ids]
    for meta in uploads:
        if not meta["completed"]:
            raise HTTPException(409, f"“{meta['name']}” ainda não terminou de ser enviado.")

    # Depois de converter, o arquivo guardado tem um tamanho diferente do enviado.
    input_bytes = sum(upload_data_path(meta["id"]).stat().st_size for meta in uploads)
    # O PDF final ocupa mais ou menos a soma dos arquivos; junções na fila também contam.
    needed = input_bytes + sum(job["input_bytes"] for job in active_jobs()) + DISK_RESERVE_BYTES
    free = free_disk_bytes()
    if needed > free:
        raise HTTPException(
            507,
            f"Espaço em disco insuficiente para o PDF final: "
            f"precisa de cerca de {format_bytes(needed)}, há {format_bytes(free)} livres.",
        )

    job_id = uuid.uuid4().hex
    job = jobs[job_id] = {
        "id": job_id,
        "name": safe_pdf_name(body.output_name),
        "status": "queued",
        "stage": None,
        "current": 0,
        "total": len(uploads),
        "percent": 0,
        "pages": None,
        "size": None,
        "notice": None,
        "compression": body.compression,
        "input_bytes": input_bytes,
        "error": None,
        "created": time.time(),
        "upload_ids": [meta["id"] for meta in uploads],
    }
    task = asyncio.create_task(run_merge(job, uploads))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return public_job(job)


async def run_merge(job, uploads):
    final_path = OUTPUTS_DIR / f"{job['id']}.pdf"
    tmp_path = OUTPUTS_DIR / f"{job['id']}.tmp"
    async with merge_slot:
        job["status"] = "running"
        try:
            payload = {
                "output": str(tmp_path),
                "compression": job["compression"],
                "inputs": [
                    {"path": str(upload_data_path(meta["id"])), "name": meta["name"]}
                    for meta in uploads
                ],
            }
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.worker", "merge",
                cwd=BASE_DIR,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_tail = deque(maxlen=20)

            async def drain_stderr():
                async for line in proc.stderr:
                    stderr_tail.append(line.decode(errors="replace").rstrip())

            stderr_task = asyncio.create_task(drain_stderr())
            proc.stdin.write(json.dumps(payload).encode())
            await proc.stdin.drain()
            proc.stdin.close()

            error = None
            async for line in proc.stdout:
                messages = parse_messages([line.decode(errors="replace")])
                if not messages:
                    continue
                message = messages[0]
                if "error" in message:
                    error = message["error"]
                    continue
                job["stage"] = message.get("stage", job["stage"])
                for key in ("current", "total", "percent", "pages", "notice"):
                    if key in message:
                        job[key] = message[key]

            code = await proc.wait()
            await stderr_task
            if code == 0 and error is None:
                os.replace(tmp_path, final_path)
                job.update(status="done", percent=100, size=final_path.stat().st_size)
            elif code < 0 and error is None:
                job.update(
                    status="error",
                    error="O processo de junção foi encerrado pelo sistema "
                    "(possivelmente falta de memória). Tente novamente com menos programas abertos.",
                )
            else:
                details = error or " ".join(stderr_tail) or f"código de saída {code}"
                job.update(status="error", error=f"Falha ao juntar os PDFs: {details}")
        except Exception as exc:  # noqa: BLE001 - o erro precisa chegar ao navegador
            job.update(status="error", error=f"Falha ao juntar os PDFs: {exc}")
        finally:
            tmp_path.unlink(missing_ok=True)
            if job["status"] == "done":
                write_json(OUTPUTS_DIR / f"{job['id']}.json", job)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    return public_job(find_job(job_id))


@app.api_route("/api/jobs/{job_id}/download", methods=["GET", "HEAD"])
async def download_job(job_id: str):
    job = find_job(job_id)
    path = OUTPUTS_DIR / f"{job_id}.pdf"
    if job["status"] != "done" or not path.exists():
        raise HTTPException(409, "O PDF final ainda não está pronto.")
    return FileResponse(path, media_type="application/pdf", filename=job["name"])


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = find_job(job_id)
    if job["status"] in ("queued", "running"):
        raise HTTPException(409, "A junção ainda está em andamento.")
    for suffix in (".pdf", ".json"):
        (OUTPUTS_DIR / f"{job_id}{suffix}").unlink(missing_ok=True)
    jobs.pop(job_id, None)
    return {"deleted": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
