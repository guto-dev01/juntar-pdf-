#!/usr/bin/env bash
# Abre o sistema para uso fora deste computador, por um túnel da Cloudflare,
# protegido por uma chave de acesso que vai embutida no link.
#
#   ./compartilhar.sh              Ctrl+C encerra o servidor e o túnel
#   PDF_CHAVE_ACESSO=xyz ./compartilhar.sh   usa uma chave fixa
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p dados

# cloudflared: usa o instalado ou baixa a versão oficial para .bin/
CLOUDFLARED="$(command -v cloudflared || true)"
[ -z "$CLOUDFLARED" ] && [ -x "$HOME/.local/bin/cloudflared" ] && CLOUDFLARED="$HOME/.local/bin/cloudflared"
if [ -z "$CLOUDFLARED" ]; then
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *) echo "Arquitetura não suportada: $(uname -m)"; exit 1 ;;
  esac
  echo "Baixando cloudflared..."
  mkdir -p .bin
  curl -fsSL -o .bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
  chmod +x .bin/cloudflared
  CLOUDFLARED=.bin/cloudflared
fi

# Primeira porta livre a partir da 8001 (a 8000 fica para o ./iniciar.sh local).
PORT="${PORT:-8001}"
while (: > "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; do PORT=$((PORT + 1)); done

export PDF_CHAVE_ACESSO="${PDF_CHAVE_ACESSO:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')}"

SERVER_LOOP_PID=""
TUNNEL_PID=""
cleanup() {
  kill $SERVER_LOOP_PID $TUNNEL_PID 2>/dev/null || true
  [ -f dados/servidor.pid ] && kill "$(cat dados/servidor.pid)" 2>/dev/null || true
  rm -f dados/compartilhar.pid dados/compartilhar-link.txt dados/servidor.pid
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# O servidor sobe de novo sozinho se cair. Assim dá para carregar uma atualização
# (kill $(cat dados/servidor.pid)) sem derrubar o túnel nem trocar o link.
(
  while true; do
    HOST=127.0.0.1 PORT="$PORT" ./iniciar.sh >> dados/servidor-compartilhado.log 2>&1 &
    echo $! > dados/servidor.pid
    wait $! || true
    sleep 1
  done
) &
SERVER_LOOP_PID=$!
for _ in $(seq 120); do
  (: > "/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  kill -0 "$SERVER_LOOP_PID" 2>/dev/null || { echo "O servidor não iniciou. Veja dados/servidor-compartilhado.log"; exit 1; }
  sleep 0.5
done

"$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > dados/tunel.log 2>&1 &
TUNNEL_PID=$!
URL=""
for _ in $(seq 60); do
  URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' dados/tunel.log | head -1 || true)"
  [ -n "$URL" ] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "Não foi possível abrir o túnel. Veja dados/tunel.log"
  exit 1
fi

LINK="$URL/?chave=$PDF_CHAVE_ACESSO"
echo $$ > dados/compartilhar.pid
echo "$LINK" > dados/compartilhar-link.txt
echo
echo "Link para compartilhar (já inclui a chave de acesso):"
echo "  $LINK"
echo
echo "O link muda a cada vez que este script é iniciado. Ctrl+C encerra."
echo "Para carregar uma atualização do sistema sem trocar o link:"
echo "  kill \$(cat dados/servidor.pid)"

# Fica no ar até o túnel cair (ou até o Ctrl+C).
wait "$TUNNEL_PID"
