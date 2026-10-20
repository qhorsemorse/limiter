#!/usr/bin/env bash
# Generate the local CA + DoH server certificate (10 years, SAN 127.0.0.1/localhost).
# Idempotent: leaves existing certs untouched.
set -eu
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE/certs"
if [ -s ca.pem ] && [ -s server.pem ]; then
  if openssl verify -CAfile ca.pem server.pem >/dev/null 2>&1; then
    echo "Certs already exist and the chain verifies -- skipping."
  else
    echo "Certs exist but the chain does NOT verify. Remove certs/ and re-run."
    exit 1
  fi
  exit 0
fi
mkdir -p "$BASE/certs"
openssl genrsa -out ca.key 3072
openssl req -x509 -new -key ca.key -sha256 -days 3650 \
  -subj "/CN=ControlLimits Local CA" -out ca.pem
openssl genrsa -out server.key 2048
openssl req -new -key server.key -subj "/CN=localhost" -out server.csr
cat > server.ext <<'EOF'
subjectAltName = IP:127.0.0.1,DNS:localhost
extendedKeyUsage = serverAuth
basicConstraints = CA:FALSE
EOF
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -days 3650 -sha256 -extfile server.ext -out server.pem
chmod 600 ca.key server.key
openssl verify -CAfile ca.pem server.pem
echo "Certs ready in $BASE/certs. Next: tools/setup_firefox.sh imports the CA into Firefox."
