"""Quais formatos o sistema aceita, além de PDF.

Tudo o que não é PDF vira PDF no momento do envio: imagens pelo Pillow e
documentos pelo LibreOffice.
"""

from pathlib import Path

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif", ".ico", ".ppm", ".pgm",
}

# Abertos pelo LibreOffice (texto, planilha, apresentação, desenho, web).
DOCUMENT_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".ott", ".rtf", ".wps", ".abw",
    ".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".ods", ".ots", ".csv", ".tsv", ".dif",
    ".ppt", ".pptx", ".pps", ".ppsx", ".pot", ".potx", ".odp", ".otp",
    ".odg", ".otg", ".svg", ".vsd", ".vsdx", ".pub", ".epub", ".html", ".htm", ".xhtml",
}

# Convertidos como texto simples (o LibreOffice escolhe o filtro pela extensão).
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".json", ".xml", ".yml", ".yaml", ".ini", ".srt"}

CONVERTIBLE_EXTENSIONS = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS | TEXT_EXTENSIONS
SUPPORTED_EXTENSIONS = CONVERTIBLE_EXTENSIONS | {".pdf"}


def extension(name):
    return Path(name.replace("\\", "/")).suffix.lower()


def is_supported(name):
    return extension(name) in SUPPORTED_EXTENSIONS
