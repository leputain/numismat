#!/usr/bin/env bash
set -euo pipefail

web_edge_image=${WEB_EDGE_IMAGE:-finbot-web:check}
public_host=web-edge.smoke.invalid
resource_prefix="numismat-web-edge-smoke-$$-$(date +%s)"
network_name="${resource_prefix}-net"
secret_volume="${resource_prefix}-secrets"
api_container="${resource_prefix}-api"
web_container="${resource_prefix}-web"
secret_seed_container="${resource_prefix}-secret-seed"

tmp_dir=""
network_created=0
secret_volume_created=0
api_container_created=0
web_container_created=0
secret_seed_container_created=0

fail() {
    printf 'web-edge smoke: ERROR: %s\n' "$1" >&2
    exit 1
}

cleanup() {
    local tmp_basename

    set +e
    if [[ $web_container_created -eq 1 ]]; then
        docker rm --force "$web_container" >/dev/null 2>&1
    fi
    if [[ $api_container_created -eq 1 ]]; then
        docker rm --force "$api_container" >/dev/null 2>&1
    fi
    if [[ $secret_seed_container_created -eq 1 ]]; then
        docker rm --force "$secret_seed_container" >/dev/null 2>&1
    fi
    if [[ $secret_volume_created -eq 1 ]]; then
        docker volume rm "$secret_volume" >/dev/null 2>&1
    fi
    if [[ $network_created -eq 1 ]]; then
        docker network rm "$network_name" >/dev/null 2>&1
    fi
    if [[ -n $tmp_dir && -d $tmp_dir ]]; then
        tmp_basename=$(basename -- "$tmp_dir")
        case "$tmp_basename" in
            numismat-web-edge-smoke.*) rm -rf -- "$tmp_dir" ;;
            *) printf 'web-edge smoke: refusing unsafe temp cleanup\n' >&2 ;;
        esac
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for required_command in awk basename cmp curl docker grep mktemp openssl sed; do
    command -v "$required_command" >/dev/null 2>&1 \
        || fail "required command is unavailable: $required_command"
done

docker image inspect "$web_edge_image" >/dev/null 2>&1 \
    || fail "web image is unavailable: $web_edge_image"

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/numismat-web-edge-smoke.XXXXXXXX")
case "$(basename -- "$tmp_dir")" in
    numismat-web-edge-smoke.*) ;;
    *) fail "mktemp returned an unexpected path" ;;
esac

ca_key="$tmp_dir/ca.key"
ca_cert="$tmp_dir/ca.crt"
leaf_key="$tmp_dir/leaf.key"
leaf_request="$tmp_dir/leaf.csr"
leaf_cert="$tmp_dir/leaf.crt"
leaf_extensions="$tmp_dir/leaf.ext"

openssl genrsa -out "$ca_key" 2048 >/dev/null 2>&1
openssl req -x509 -new -sha256 -days 1 \
    -key "$ca_key" \
    -subj '/CN=Numismat Web Edge Smoke CA' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -out "$ca_cert" >/dev/null 2>&1
openssl req -new -newkey rsa:2048 -nodes \
    -keyout "$leaf_key" \
    -subj "/CN=$public_host" \
    -out "$leaf_request" >/dev/null 2>&1
printf '%s\n' \
    'basicConstraints=critical,CA:FALSE' \
    'keyUsage=critical,digitalSignature,keyEncipherment' \
    'extendedKeyUsage=serverAuth' \
    "subjectAltName=DNS:$public_host" \
    > "$leaf_extensions"
openssl x509 -req -sha256 -days 1 \
    -in "$leaf_request" \
    -CA "$ca_cert" \
    -CAkey "$ca_key" \
    -CAcreateserial \
    -extfile "$leaf_extensions" \
    -out "$leaf_cert" >/dev/null 2>&1
chmod 0600 "$ca_key" "$leaf_key"

docker network create --driver bridge "$network_name" >/dev/null
network_created=1
docker volume create "$secret_volume" >/dev/null
secret_volume_created=1

docker create \
    --name "$secret_seed_container" \
    --user 0:0 \
    --mount "type=volume,source=$secret_volume,target=/run/secrets" \
    --entrypoint /bin/sh \
    "$web_edge_image" \
    -c 'set -eu
        cp /tmp/miniapp_tls_fullchain /run/secrets/miniapp_tls_fullchain
        cp /tmp/miniapp_tls_private_key /run/secrets/miniapp_tls_private_key
        chown 101:101 /run/secrets/miniapp_tls_fullchain /run/secrets/miniapp_tls_private_key
        chmod 0444 /run/secrets/miniapp_tls_fullchain
        chmod 0400 /run/secrets/miniapp_tls_private_key' \
    >/dev/null
secret_seed_container_created=1
docker cp "$leaf_cert" "$secret_seed_container:/tmp/miniapp_tls_fullchain"
docker cp "$leaf_key" "$secret_seed_container:/tmp/miniapp_tls_private_key"
docker start --attach "$secret_seed_container" >/dev/null
docker rm "$secret_seed_container" >/dev/null
secret_seed_container_created=0

stub_config="$tmp_dir/stub-nginx.conf"
printf '%s\n' \
    'pid /tmp/nginx-stub.pid;' \
    'worker_processes 1;' \
    'error_log /dev/stderr crit;' \
    'events { worker_connections 128; }' \
    'http {' \
    '    access_log off;' \
    '    client_body_temp_path /tmp/nginx-stub-client-body;' \
    '    server {' \
    '        listen 8080;' \
    '        server_name _;' \
    '        default_type text/plain;' \
    '        add_header Cache-Control "no-store, max-age=0" always;' \
    '        add_header Pragma "no-cache" always;' \
    '        add_header X-Smoke-Upstream "stub" always;' \
    '        add_header X-Smoke-Host "$http_host" always;' \
    '        add_header X-Smoke-Preserved "$http_origin|$http_cookie" always;' \
    '        add_header X-Smoke-Stripped "$http_forwarded|$http_proxy|$http_x_forwarded_for|$http_x_forwarded_host|$http_x_forwarded_port|$http_x_forwarded_proto|$http_x_real_ip" always;' \
    '        location / { return 200 "upstream=stub"; }' \
    '    }' \
    '}' \
    > "$stub_config"

docker create \
    --name "$api_container" \
    --network "$network_name" \
    --network-alias api \
    --user 101:101 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 20 \
    --entrypoint nginx \
    "$web_edge_image" \
    -c /etc/nginx/stub-nginx.conf \
    -g 'daemon off;' \
    >/dev/null
api_container_created=1
docker cp "$stub_config" "$api_container:/etc/nginx/stub-nginx.conf"
docker start "$api_container" >/dev/null

docker run --detach \
    --name "$web_container" \
    --network "$network_name" \
    --user 101:101 \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 50 \
    --init \
    --stop-timeout 15 \
    --env "MINIAPP_PUBLIC_URL=https://$public_host" \
    --mount "type=volume,source=$secret_volume,target=/run/secrets,readonly" \
    --publish 127.0.0.1::8443 \
    "$web_edge_image" \
    >/dev/null
web_container_created=1

published_port=$(docker port "$web_container" 8443/tcp | awk -F: 'NR == 1 { print $NF }')
[[ $published_port =~ ^[0-9]+$ ]] || fail "Docker did not publish the HTTPS port"

curl_common=(
    --disable
    --silent
    --show-error
    --noproxy '*'
    --connect-timeout 2
    --max-time 10
    --cacert "$ca_cert"
    --resolve "$public_host:$published_port:127.0.0.1"
)

edge_ready=0
for ((attempt = 1; attempt <= 80; attempt += 1)); do
    if curl "${curl_common[@]}" \
        --connect-timeout 1 \
        --max-time 1 \
        --output /dev/null \
        "https://$public_host:$published_port/" \
        >/dev/null 2>&1; then
        edge_ready=1
        break
    fi
    if [[ $(docker inspect --format '{{.State.Running}}' "$web_container") != true ]]; then
        docker logs "$web_container" >&2 || true
        fail "web edge exited before readiness"
    fi
    sleep 0.25
done
[[ $edge_ready -eq 1 ]] || fail "web edge did not become ready"

request() {
    local request_name=$1
    shift
    local status

    status=$(curl "${curl_common[@]}" \
        --dump-header "$tmp_dir/$request_name.headers" \
        --output "$tmp_dir/$request_name.body" \
        --write-out '%{http_code}' \
        "$@")
    printf '%s' "$status" > "$tmp_dir/$request_name.status"
}

assert_status() {
    local request_name=$1
    local expected_status=$2
    local actual_status

    actual_status=$(<"$tmp_dir/$request_name.status")
    [[ $actual_status == "$expected_status" ]] \
        || fail "$request_name returned HTTP $actual_status, expected $expected_status"
}

assert_header_contains() {
    local request_name=$1
    local header_name=$2
    local expected_fragment=$3

    awk \
        -v header_name="${header_name,,}:" \
        -v expected_fragment="${expected_fragment,,}" \
        '{
            line = tolower($0)
            sub(/\r$/, "", line)
            if (index(line, header_name) == 1 && index(line, expected_fragment) > 0) {
                found = 1
            }
        }
        END { exit(found ? 0 : 1) }
        ' "$tmp_dir/$request_name.headers" \
        || fail "$request_name is missing required $header_name header content"
}

assert_header_absent() {
    local request_name=$1
    local header_name=$2

    if grep -Eiq "^${header_name}:" "$tmp_dir/$request_name.headers"; then
        fail "$request_name unexpectedly returned $header_name"
    fi
}

assert_security_headers() {
    local request_name=$1

    assert_header_contains "$request_name" Content-Security-Policy "default-src 'none'"
    assert_header_contains "$request_name" Strict-Transport-Security 'max-age=31536000'
    assert_header_contains "$request_name" X-Content-Type-Options 'nosniff'
    assert_header_contains "$request_name" Referrer-Policy 'no-referrer'
}

finance_marker="finance-marker-$$"
cookie_marker="cookie-marker-$$"
origin_value=https://origin.smoke.invalid

request index "https://$public_host:$published_port/index.html"
assert_status index 200
assert_security_headers index
assert_header_contains index Cache-Control 'no-store'
assert_header_contains index Pragma 'no-cache'
assert_header_absent index ETag

request spa "https://$public_host:$published_port/ledger/$finance_marker?account=$finance_marker"
assert_status spa 200
assert_security_headers spa
assert_header_contains spa Cache-Control 'no-store'
assert_header_absent spa ETag
cmp -s "$tmp_dir/index.body" "$tmp_dir/spa.body" \
    || fail "SPA route did not fall back to index.html"

request missing_asset \
    "https://$public_host:$published_port/assets/$finance_marker-missing.js?query=$finance_marker"
assert_status missing_asset 404
assert_header_contains missing_asset Cache-Control 'no-store'
if cmp -s "$tmp_dir/index.body" "$tmp_dir/missing_asset.body"; then
    fail "missing asset fell back to the SPA"
fi

asset_file=$(docker exec "$web_container" \
    sh -c "find /usr/share/nginx/html/assets -type f -print 2>/dev/null | sed -n '1p'")
[[ $asset_file == /usr/share/nginx/html/assets/* ]] \
    || fail "built image does not contain a Vite asset"
asset_url=${asset_file#/usr/share/nginx/html}
request asset "https://$public_host:$published_port$asset_url"
assert_status asset 200
assert_security_headers asset
assert_header_contains asset Cache-Control 'public'
assert_header_contains asset Cache-Control 'max-age=31536000'
assert_header_contains asset Cache-Control 'immutable'

request api_root "https://$public_host:$published_port/api?query=$finance_marker"
assert_status api_root 404
if cmp -s "$tmp_dir/index.body" "$tmp_dir/api_root.body"; then
    fail "/api fell back to the SPA"
fi

request api_proxy \
    --header "Origin: $origin_value" \
    --header "Cookie: edge_smoke=$cookie_marker" \
    --header 'Forwarded: for=198.51.100.10;proto=http' \
    --header 'Proxy: synthetic-proxy' \
    --header 'X-Forwarded-For: 198.51.100.11' \
    --header 'X-Forwarded-Host: attacker.invalid' \
    --header 'X-Forwarded-Port: 81' \
    --header 'X-Forwarded-Proto: http' \
    --header 'X-Real-IP: 198.51.100.12' \
    "https://$public_host:$published_port/api/v1/edge-smoke?query=$finance_marker"
assert_status api_proxy 200
assert_security_headers api_proxy
assert_header_contains api_proxy Cache-Control 'no-store'
assert_header_contains api_proxy X-Smoke-Upstream 'stub'
assert_header_contains api_proxy X-Smoke-Host "$public_host"
assert_header_contains api_proxy X-Smoke-Preserved "$origin_value|edge_smoke=$cookie_marker"
assert_header_contains api_proxy X-Smoke-Stripped '||||||'
grep -Fq 'upstream=stub' "$tmp_dir/api_proxy.body" \
    || fail "API request did not reach the upstream"
if cmp -s "$tmp_dir/index.body" "$tmp_dir/api_proxy.body"; then
    fail "API request fell back to the SPA"
fi

request health_live "https://$public_host:$published_port/health/live?query=$finance_marker"
assert_status health_live 200
assert_header_contains health_live X-Smoke-Upstream 'stub'
if cmp -s "$tmp_dir/index.body" "$tmp_dir/health_live.body"; then
    fail "health request fell back to the SPA"
fi

request health_unknown \
    "https://$public_host:$published_port/health/$finance_marker?query=$finance_marker"
assert_status health_unknown 404
assert_header_contains health_unknown Cache-Control 'no-store'
if cmp -s "$tmp_dir/index.body" "$tmp_dir/health_unknown.body"; then
    fail "unknown health request fell back to the SPA"
fi

request non_get \
    --request POST \
    --header 'Content-Length: 0' \
    "https://$public_host:$published_port/dashboard/$finance_marker?query=$finance_marker"
assert_status non_get 405
assert_security_headers non_get

wrong_host=wrong-edge.smoke.invalid
wrong_host_status=$(curl \
    --disable --silent --show-error --noproxy '*' \
    --connect-timeout 2 --max-time 10 --insecure \
    --resolve "$wrong_host:$published_port:127.0.0.1" \
    --dump-header "$tmp_dir/wrong_host.headers" \
    --output "$tmp_dir/wrong_host.body" \
    --write-out '%{http_code}' \
    "https://$wrong_host:$published_port/$finance_marker?query=$finance_marker")
printf '%s' "$wrong_host_status" > "$tmp_dir/wrong_host.status"
assert_status wrong_host 421
assert_security_headers wrong_host

docker exec "$web_container" nginx -T > "$tmp_dir/effective-nginx.conf" 2>&1
proxy_pass_count=$(awk \
    '$1 == "proxy_pass" && $2 == "http://finbot_api;" { count += 1 }
     END { print count + 0 }' \
    "$tmp_dir/effective-nginx.conf")
proxy_no_retry_count=$(awk \
    '$1 == "proxy_next_upstream" && $2 == "off;" { count += 1 }
     END { print count + 0 }' \
    "$tmp_dir/effective-nginx.conf")
[[ $proxy_pass_count -gt 0 && $proxy_no_retry_count -eq $proxy_pass_count ]] \
    || fail "not every API/health proxy location disables upstream retries"

source_map_path=$(docker exec "$web_container" \
    sh -c "find /usr/share/nginx/html -type f -name '*.map' -print 2>/dev/null | sed -n '1p'")
[[ -z $source_map_path ]] || fail "production image contains a source map"

docker logs "$web_container" > "$tmp_dir/web-edge.log" 2>&1
grep -Fq '"event":"edge_request_completed"' "$tmp_dir/web-edge.log" \
    || fail "web edge did not emit completion logs"
for forbidden_log_fragment in \
    "$finance_marker" \
    "$cookie_marker" \
    "$origin_value" \
    "$public_host" \
    '/ledger/' \
    '/assets/' \
    '/api/' \
    '/health/'; do
    if grep -Fq "$forbidden_log_fragment" "$tmp_dir/web-edge.log"; then
        fail "privacy log contains request data"
    fi
done
if awk '
    {
        line = tolower($0)
        if (line ~ /"(uri|request_uri|args|query|cookie|host|remote_addr)"[[:space:]]*:/) {
            found = 1
        }
    }
    END { exit(found ? 0 : 1) }
    ' "$tmp_dir/web-edge.log"; then
    fail "privacy log contains a forbidden field"
fi

printf 'web-edge smoke: OK\n'
