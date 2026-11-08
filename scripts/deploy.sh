#!/usr/bin/env bash
# Builda a imagem dos MCPs, envia por SSH para um host com Docker e recria os containers.
# Requer, no host, um compose.yml que use a imagem `mcp:latest` (e os .env dos servidores).
#   DEPLOY_HOST      destino SSH (ex.: usuario@meu-servidor)
#   DEPLOY_DIR       pasta do compose.yml no host, relativa ao HOME
#   DEPLOY_PLATFORM  plataforma da imagem (padrão linux/arm64)
# Defina no mise.local.toml (fora do git).
set -euo pipefail

HOST="${DEPLOY_HOST:?defina DEPLOY_HOST (ex.: no mise.local.toml)}"
REMOTE_DIR="${DEPLOY_DIR:?defina DEPLOY_DIR (ex.: no mise.local.toml)}"
PLATFORM="${DEPLOY_PLATFORM:-linux/arm64}"
IMAGE="mcp"

cd "$(dirname "$0")/.."

TAG="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
[[ -n "$(git status --porcelain 2>/dev/null || echo dirty)" ]] && TAG="${TAG}-dirty-$(date +%Y%m%d%H%M%S)"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "Verificando o host ($HOST)"
ssh "$HOST" "test -f $REMOTE_DIR/compose.yml" || {
  echo "~/$REMOTE_DIR/compose.yml não existe em $HOST. Crie o compose do host antes do deploy."
  exit 1
}

step "Testes, tipos e lint"
poetry run poe test
poetry run poe type
poetry run ruff check src
poetry run ruff format --check src

step "Build $IMAGE:$TAG ($PLATFORM)"
docker buildx build \
  --platform "$PLATFORM" \
  --tag "$IMAGE:$TAG" \
  --tag "$IMAGE:latest" \
  --load \
  .

step "Enviando imagem para $HOST"
docker save "$IMAGE:$TAG" "$IMAGE:latest" | gzip -1 | ssh "$HOST" 'gunzip | docker load'

step "Subindo os containers"
ssh "$HOST" bash -s -- "$REMOTE_DIR" "$IMAGE" "$TAG" <<'REMOTE'
set -euo pipefail
cd "$1"
docker compose up -d --wait --wait-timeout 120
# Mantém só a tag atual e a latest
docker images "$2" --format '{{.Tag}}' | grep -vx -e latest -e "$3" | xargs -r -I{} docker rmi "$2:{}" >/dev/null || true
docker image prune -f >/dev/null
docker compose ps
REMOTE

step "Deploy de $IMAGE:$TAG concluído"
