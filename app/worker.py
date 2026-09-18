"""Tarefas pesadas com PDF, executadas em um processo separado do servidor.

Rodar fora do servidor evita travar os envios em andamento e devolve toda a
memória ao sistema quando a tarefa termina.

    python -m app.worker inspect ARQUIVO.pdf
    python -m app.worker convert ORIGEM DESTINO.pdf "nome-original.docx"
    python -m app.worker merge   < {"output": "...", "inputs": [{"path": "...", "name": "..."}]}

Cada linha escrita em stdout é um objeto JSON (andamento, resultado ou erro).
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zlib
from array import array
from collections import deque
from decimal import Decimal
from pathlib import Path

import pikepdf
from PIL import Image, ImageOps
from pikepdf import ObjectType

from app import formats

try:  # fotos de iPhone (.heic)
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - segue sem HEIC
    pass

MIB = 1024 * 1024
XREF_ENTRY_SIZE = 11  # campos /W [1 8 2]

# Compactação sem perda: streams gravados sem filtro nenhum passam por zlib.
MIN_PACK_BYTES = 512
# Redução de imagens (opcional): ~150 pontos por polegada em uma folha A4.
MAX_IMAGE_SIDE = 1800
MAX_IMAGE_PIXELS = 80_000_000
JPEG_QUALITY = 72

# Conversão de imagens e documentos para PDF.
A4_SIZE = (595.28, 841.89)
PAGE_MARGIN = 24
DEFAULT_DPI = 96
LIBREOFFICE_TIMEOUT = 600


def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def open_pdf(path):
    # "stream" lê o arquivo sob demanda, sem mapear gigabytes na memória.
    return pikepdf.open(path, access_mode=pikepdf.AccessMode.stream)


def describe_error(exc):
    if isinstance(exc, pikepdf.PasswordError):
        return "está protegido por senha"
    return f"não pôde ser lido como PDF ({exc})"


def inspect(path):
    try:
        with open_pdf(path) as pdf:
            emit(pages=len(pdf.pages))
    except (pikepdf.PdfError, pikepdf.PasswordError) as exc:
        emit(error=f"O arquivo {describe_error(exc)}.")
        return 1
    return 0


class StreamingPdfWriter:
    """Grava um PDF objeto por objeto, direto no disco.

    Juntar com pikepdf.pages.extend() faz o qpdf copiar para a memória todo o
    conteúdo das páginas antes de salvar, o que esgota a RAM com arquivos de
    vários gigabytes. Aqui cada objeto é gravado assim que é lido, então a
    memória usada fica limitada ao maior objeto individual.
    """

    def __init__(self, path):
        self.file = open(path, "wb", buffering=8 * MIB)
        self.offsets = array("Q", [0])  # posição de cada objeto; o número 0 é reservado
        self.file.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")

    def allocate(self):
        self.offsets.append(0)
        return len(self.offsets) - 1

    def write_object(self, number, body, stream_data=None):
        self.offsets[number] = self.file.tell()
        self.file.write(b"%d 0 obj\n" % number)
        self.file.write(body)
        if stream_data is not None:
            self.file.write(b"\nstream\n")
            self.file.write(stream_data)
            self.file.write(b"\nendstream")
        self.file.write(b"\nendobj\n")

    def close(self, root_number):
        # Tabela de referências em formato de stream (PDF 1.5+): posições com 8 bytes,
        # sem o limite de 10 dígitos (~9,3 GB) da tabela clássica "xref".
        xref_number = self.allocate()
        xref_offset = self.file.tell()
        self.offsets[xref_number] = xref_offset

        compressor = zlib.compressobj()
        compressed = []
        batch = bytearray(b"\x00" + bytes(8) + b"\xff\xff")
        for offset in self.offsets[1:]:
            batch += (b"\x01" + offset.to_bytes(8, "big") + b"\x00\x00") if offset else bytes(XREF_ENTRY_SIZE)
            if len(batch) >= MIB:
                compressed.append(compressor.compress(batch))
                batch.clear()
        compressed.append(compressor.compress(batch))
        compressed.append(compressor.flush())
        data = b"".join(compressed)

        file_id = os.urandom(16).hex().encode()
        self.write_object(
            xref_number,
            b"<</Type/XRef/Size %d/W[1 8 2]/Root %d 0 R/ID[<%s><%s>]/Filter/FlateDecode/Length %d>>"
            % (len(self.offsets), root_number, file_id, file_id, len(data)),
            data,
        )
        self.file.write(b"startxref\n%d\n%%%%EOF\n" % xref_offset)
        self.file.close()


class PageCopier:
    """Copia as páginas de um PDF de origem, e tudo o que elas usam, para o writer."""

    def __init__(self, writer, pages_number, shrink_images=False):
        self.writer = writer
        self.parent_ref = b"/Parent %d 0 R" % pages_number
        self.shrink_images = shrink_images
        self.name_cache = {}
        self.damaged_streams = 0
        self.saved_bytes = 0

    def copy_pages(self, pdf, on_bytes):
        numbers = {}  # (número, geração) na origem -> número no PDF final
        pending = deque()

        def ref(obj):
            key = obj.objgen
            number = numbers.get(key)
            if number is None:
                number = numbers[key] = self.writer.allocate()
                pending.append(obj)
            return number

        page_numbers = [ref(page.obj) for page in pdf.pages]
        page_keys = {page.obj.objgen for page in pdf.pages}
        while pending:
            obj = pending.popleft()
            key = obj.objgen
            self.write(numbers[key], obj, ref, is_page=key in page_keys, on_bytes=on_bytes)
        return page_numbers

    def write(self, number, obj, ref, is_page, on_bytes):
        out = []
        type_code = obj._type_code
        if type_code == ObjectType.stream:
            try:
                data = obj.read_raw_bytes()
            except pikepdf.PdfError:
                # Conteúdo ilegível no arquivo de origem: segue com um stream vazio.
                data = b""
                self.damaged_streams += 1
            original_size = len(data)

            smaller = self.shrink_image(obj, data) if self.shrink_images else None
            if smaller is not None:
                data = self.write_image(number, obj, ref, smaller)
            else:
                data = self.pack_stream(obj, data, ref, out)
                self.writer.write_object(number, b"".join(out), data)
            self.saved_bytes += original_size - len(data)
            on_bytes(original_size)
            return
        if type_code == ObjectType.dictionary:
            if is_page:
                # A página passa a pertencer à árvore de páginas do PDF final.
                self.dictionary(obj, ref, out, skip=("/Parent",), extra=self.parent_ref)
            else:
                self.dictionary(obj, ref, out)
        elif type_code == ObjectType.array:
            self.array(obj, ref, out)
        else:
            out.append(obj.unparse(resolved=True))
        self.writer.write_object(number, b"".join(out))

    def pack_stream(self, obj, data, ref, out):
        """Comprime, sem perder nada, os streams gravados sem filtro na origem."""
        extra = b""
        if len(data) >= MIN_PACK_BYTES and "/Filter" not in obj:
            packed = zlib.compress(data, 6)
            if len(packed) < len(data) * 0.95:
                data = packed
                extra = b"/Filter/FlateDecode"
        self.dictionary(obj, ref, out, skip=("/Length",), extra=extra + b"/Length %d" % len(data))
        return data

    def shrink_image(self, obj, data):
        """Recomprime uma imagem como JPEG, diminuindo também os pixels se for grande.

        Devolve None quando não é imagem, quando o formato não é suportado ou
        quando o resultado não ficaria menor. Nesses casos o original é mantido.
        """
        try:
            if obj.get("/Subtype") != "/Image" or not data:
                return None
            # Máscaras, imagens de 1 bit (escaneados em preto e branco) e faixas
            # de cor invertidas ficam maiores ou mudam de aparência em JPEG.
            if any(key in obj for key in ("/ImageMask", "/Mask", "/Decode")):
                return None
            if int(obj.get("/BitsPerComponent", 8)) < 8:
                return None
            if int(obj.Width) * int(obj.Height) > MAX_IMAGE_PIXELS:
                return None
            image = pikepdf.PdfImage(obj).as_pil_image()
            scale = MAX_IMAGE_SIDE / max(image.size)
            if scale < 1:
                size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
                image = image.resize(size, Image.LANCZOS)
            mode = "L" if image.mode in ("1", "L", "LA", "I", "I;16") else "RGB"
            if image.mode != mode:
                image = image.convert(mode)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
        except Exception:  # noqa: BLE001 - formato exótico: mantém a imagem original
            return None
        packed = buffer.getvalue()
        if len(packed) >= len(data):
            return None
        return packed, b"DeviceGray" if mode == "L" else b"DeviceRGB", image.width, image.height

    def write_image(self, number, obj, ref, smaller):
        data, colorspace, width, height = smaller
        parts = [
            b"<</Type/XObject/Subtype/Image/Width %d/Height %d/BitsPerComponent 8"
            b"/ColorSpace/%s/Filter/DCTDecode/Length %d" % (width, height, colorspace, len(data))
        ]
        if "/SMask" in obj:  # a transparência continua em um objeto à parte
            parts.append(b"/SMask ")
            self.value(obj.get("/SMask"), ref, parts)
        if obj.get("/Interpolate", False):
            parts.append(b"/Interpolate true")
        parts.append(b">>")
        self.writer.write_object(number, b"".join(parts), data)
        return data

    def name(self, key):
        encoded = self.name_cache.get(key)
        if encoded is None:
            encoded = self.name_cache[key] = pikepdf.Name(key).unparse()
        return encoded

    def dictionary(self, obj, ref, out, skip=(), extra=b""):
        out.append(b"<<")
        for key, value in obj.items():
            if key in skip:
                continue
            out.append(self.name(key))
            out.append(b" ")
            self.value(value, ref, out)
            out.append(b" ")
        out.append(extra)
        out.append(b">>")

    def array(self, obj, ref, out):
        out.append(b"[")
        for item in obj:
            self.value(item, ref, out)
            out.append(b" ")
        out.append(b"]")

    def value(self, value, ref, out):
        # O pikepdf entrega números, booleanos e null como tipos do Python.
        if isinstance(value, pikepdf.Object):
            if value.is_indirect:
                out.append(b"%d 0 R" % ref(value))
            elif value._type_code == ObjectType.dictionary:
                self.dictionary(value, ref, out)
            elif value._type_code == ObjectType.array:
                self.array(value, ref, out)
            else:
                out.append(value.unparse())
        elif value is True:
            out.append(b"true")
        elif value is False:
            out.append(b"false")
        elif value is None:
            out.append(b"null")
        elif isinstance(value, int):
            out.append(b"%d" % value)
        elif isinstance(value, Decimal):
            out.append(format(value, "f").encode())
        else:
            out.append(f"{value:f}".encode())


def write_outline(writer, outline_number, bookmarks):
    """Um marcador por arquivo, apontando para a primeira página dele."""
    numbers = [writer.allocate() for _ in bookmarks]
    for index, ((title, page_number), number) in enumerate(zip(bookmarks, numbers)):
        body = [b"<</Title ", pikepdf.String(title).unparse(), b"/Parent %d 0 R" % outline_number]
        if index > 0:
            body.append(b"/Prev %d 0 R" % numbers[index - 1])
        if index < len(numbers) - 1:
            body.append(b"/Next %d 0 R" % numbers[index + 1])
        body.append(b"/Dest[%d 0 R/Fit]>>" % page_number)
        writer.write_object(number, b"".join(body))
    if numbers:
        root = b"<</Type/Outlines/First %d 0 R/Last %d 0 R/Count %d>>" % (numbers[0], numbers[-1], len(numbers))
    else:
        root = b"<</Type/Outlines/Count 0>>"
    writer.write_object(outline_number, root)


def merge(output, inputs, compression="original"):
    total_bytes = sum(os.path.getsize(item["path"]) for item in inputs) or 1
    writer = StreamingPdfWriter(output)
    catalog_number = writer.allocate()
    pages_number = writer.allocate()
    outline_number = writer.allocate()
    copier = PageCopier(writer, pages_number, shrink_images=(compression == "imagens"))

    page_numbers = []
    bookmarks = []
    damaged_files = []
    progress = {"bytes": 0, "percent": -1, "current": 0}

    def report():
        percent = min(99, progress["bytes"] * 100 // total_bytes)
        if percent != progress["percent"]:
            progress["percent"] = percent
            emit(stage="writing", current=progress["current"], total=len(inputs), percent=percent)

    def on_bytes(count):
        progress["bytes"] += count
        report()

    for index, item in enumerate(inputs):
        progress["current"] = index + 1
        emit(stage="writing", current=index + 1, total=len(inputs), percent=max(progress["percent"], 0))
        damaged_before = copier.damaged_streams
        try:
            with open_pdf(item["path"]) as source:
                first_page = len(page_numbers)
                page_numbers.extend(copier.copy_pages(source, on_bytes))
        except (pikepdf.PdfError, pikepdf.PasswordError) as exc:
            emit(error=f"O arquivo “{item['name']}” {describe_error(exc)}.")
            return 1
        if len(page_numbers) > first_page:
            bookmarks.append((Path(item["name"]).stem, page_numbers[first_page]))
        if copier.damaged_streams > damaged_before:
            damaged_files.append(item["name"])

    kids = b" ".join(b"%d 0 R" % number for number in page_numbers)
    writer.write_object(pages_number, b"<</Type/Pages/Count %d/Kids[%s]>>" % (len(page_numbers), kids))
    write_outline(writer, outline_number, bookmarks)
    writer.write_object(
        catalog_number,
        b"<</Type/Catalog/Pages %d 0 R/Outlines %d 0 R/PageMode/UseOutlines>>" % (pages_number, outline_number),
    )
    writer.close(catalog_number)

    # Confere se o arquivo gravado abre e tem todas as páginas.
    emit(stage="verifying")
    with open_pdf(output) as check:
        if len(check.pages) != len(page_numbers):
            emit(error=f"O PDF final ficou com {len(check.pages)} páginas em vez de {len(page_numbers)}.")
            return 1

    notice = None
    if damaged_files:
        notice = (
            f"Partes danificadas de {', '.join(damaged_files)} não puderam ser lidas "
            "e ficaram em branco no PDF final."
        )
    emit(stage="done", pages=len(page_numbers), notice=notice, saved=copier.saved_bytes)
    return 0


def add_image_page(pdf, frame, data, image_filter, colorspace, bits=8):
    """Cria uma página A4 (em pé ou deitada) com a imagem centralizada."""
    width, height = frame.size
    page_width, page_height = A4_SIZE if height >= width else A4_SIZE[::-1]
    # Tamanho natural da imagem em pontos, a partir da resolução gravada nela.
    dpi = frame.info.get("dpi", (DEFAULT_DPI, DEFAULT_DPI))[0] or DEFAULT_DPI
    dpi = min(max(float(dpi), 50.0), 1200.0)
    scale = min(
        (page_width - 2 * PAGE_MARGIN) / width,
        (page_height - 2 * PAGE_MARGIN) / height,
        72.0 / dpi,  # nunca amplia além do tamanho natural
    )
    draw_width, draw_height = width * scale, height * scale

    image = pdf.make_stream(
        data, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image,
        Width=width, Height=height, ColorSpace=colorspace, BitsPerComponent=bits, Filter=image_filter,
    )
    page = pdf.add_blank_page(page_size=(page_width, page_height))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im=image))
    page.Contents = pdf.make_stream(
        b"q %.2f 0 0 %.2f %.2f %.2f cm /Im Do Q"
        % (draw_width, draw_height, (page_width - draw_width) / 2, (page_height - draw_height) / 2)
    )


def encode_frame(frame, source_bytes, source_format):
    """Escolhe como gravar a imagem: sem perda quando vale a pena, JPEG nas fotos."""
    if frame.mode in ("RGBA", "LA", "PA") or (frame.mode == "P" and "transparency" in frame.info):
        # O PDF não tem fundo: o transparente vira branco, como no papel.
        background = Image.new("RGB", frame.size, "white")
        background.paste(frame.convert("RGBA"), mask=frame.convert("RGBA").split()[-1])
        frame = background
    if source_format == "JPEG" and source_bytes is not None and frame.mode in ("RGB", "L", "CMYK"):
        colorspace = {"RGB": "/DeviceRGB", "L": "/DeviceGray", "CMYK": "/DeviceCMYK"}[frame.mode]
        return source_bytes, pikepdf.Name.DCTDecode, pikepdf.Name(colorspace), 8
    if frame.mode == "1":  # traço em preto e branco: JPEG borraria
        return zlib.compress(frame.tobytes(), 9), pikepdf.Name.FlateDecode, pikepdf.Name.DeviceGray, 1
    if frame.mode not in ("RGB", "L"):
        frame = frame.convert("RGB")
    # Poucas cores (telas, desenhos): comprime bem sem perder nada.
    if frame.getcolors(256) is not None:
        colorspace = pikepdf.Name.DeviceGray if frame.mode == "L" else pikepdf.Name.DeviceRGB
        return zlib.compress(frame.tobytes(), 6), pikepdf.Name.FlateDecode, colorspace, 8
    buffer = io.BytesIO()
    frame.save(buffer, "JPEG", quality=92, optimize=True, progressive=False)
    colorspace = pikepdf.Name.DeviceGray if frame.mode == "L" else pikepdf.Name.DeviceRGB
    return buffer.getvalue(), pikepdf.Name.DCTDecode, colorspace, 8


def convert_image(source, output):
    """Transforma uma imagem (ou cada quadro de um TIFF/GIF) em páginas de PDF."""
    pdf = pikepdf.Pdf.new()
    with Image.open(source) as handle:
        source_format = handle.format
        frames = getattr(handle, "n_frames", 1)
        for index in range(frames):
            handle.seek(index)
            frame = ImageOps.exif_transpose(handle) or handle
            rotated = frame is not handle and handle.getexif().get(0x0112, 1) != 1
            raw = None
            if frames == 1 and not rotated:
                with open(source, "rb") as original:
                    raw = original.read()
            data, image_filter, colorspace, bits = encode_frame(frame, raw, source_format)
            add_image_page(pdf, frame, data, image_filter, colorspace, bits)
    pdf.save(output)
    return len(pdf.pages)


def convert_document(source, output, suffix):
    """Converte com o LibreOffice (Word, Excel, PowerPoint, texto, HTML...)."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("o LibreOffice não está instalado neste servidor")
    with tempfile.TemporaryDirectory(dir=os.path.dirname(output)) as folder:
        # O LibreOffice escolhe o filtro pela extensão, e o envio é gravado sempre
        # com o mesmo nome; por isso o arquivo ganha aqui a extensão de origem.
        staged = Path(folder) / f"entrada{suffix}"
        os.link(source, staged)
        result = subprocess.run(
            [
                soffice, "--headless", "--norestore", "--invisible", "--nolockcheck",
                f"-env:UserInstallation=file://{folder}/perfil",
                "--convert-to", "pdf", "--outdir", folder, str(staged),
            ],
            capture_output=True, timeout=LIBREOFFICE_TIMEOUT,
        )
        converted = Path(folder) / "entrada.pdf"
        if not converted.exists():
            details = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
            raise RuntimeError(details[-1] if details else "o LibreOffice não gerou o PDF")
        os.replace(converted, output)
    with open_pdf(output) as pdf:
        return len(pdf.pages)


def convert(source, output, name):
    suffix = formats.extension(name)
    try:
        if suffix in formats.IMAGE_EXTENSIONS:
            pages = convert_image(source, output)
        elif suffix in formats.DOCUMENT_EXTENSIONS:
            pages = convert_document(source, output, suffix)
        elif suffix in formats.TEXT_EXTENSIONS:
            pages = convert_document(source, output, ".txt")
        else:
            emit(error=f"Arquivos {suffix or 'sem extensão'} não podem ser juntados.")
            return 1
    except subprocess.TimeoutExpired:
        emit(error="A conversão demorou demais e foi cancelada.")
        return 1
    except Exception as exc:  # noqa: BLE001 - a mensagem precisa chegar ao navegador
        emit(error=f"Não foi possível converter para PDF ({exc}).")
        return 1
    emit(pages=pages)
    return 0


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "inspect" and len(sys.argv) == 3:
        return inspect(sys.argv[2])
    if command == "convert" and len(sys.argv) == 5:
        return convert(sys.argv[2], sys.argv[3], sys.argv[4])
    if command == "merge":
        job = json.load(sys.stdin)
        return merge(job["output"], job["inputs"], job.get("compression", "original"))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
