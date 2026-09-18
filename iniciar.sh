#!/usr/bin/env bash
# Inicia o sistema de juntar PDFs. Uso: ./iniciar.sh
#   HOST=0.0.0.0 ./iniciar.sh   -> acessível por outros computadores da rede
#   PORT=9000 ./iniciar.sh      -> outra porta
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/uvicorn ]; then
  echo "Instalando dependências (só na primeira vez)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "Abra no navegador: http://localhost:${PORT}"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --timeout-keep-alive 75
