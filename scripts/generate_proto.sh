#!/usr/bin/env bash
# Genera il codice Python dai file .proto
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROTO_DIR="$PROJECT_DIR/proto"
OUT_DIR="$PROJECT_DIR/meshcoverage/proto"

echo "==> Generazione codice Protobuf..."
mkdir -p "$OUT_DIR"
touch "$OUT_DIR/__init__.py"

python -m grpc_tools.protoc \
    --proto_path="$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    "$PROTO_DIR"/*.proto

# Fix import paths nei file generati (per compatibilità con il package)
for f in "$OUT_DIR"/*_pb2*.py; do
    sed -i 's/^import \(.*_pb2\)/from meshcoverage.proto import \1/' "$f" 2>/dev/null || true
done

echo "==> File generati in: $OUT_DIR"
ls "$OUT_DIR"
