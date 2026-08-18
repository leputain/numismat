#!/bin/sh
set -eu

preflight_failed() {
    printf '%s\n' '{"component":"web_edge","event":"edge_secret_preflight_completed","result":"error"}' >&2
    exit 1
}

fullchain=secrets/miniapp_tls_fullchain.pem
private_key=secrets/miniapp_tls_private_key.pem
public_url=${MINIAPP_PUBLIC_URL-}

case "$public_url" in
    https://*) public_host=${public_url#https://} ;;
    *) preflight_failed ;;
esac
[ "$public_url" = "https://$public_host" ] || preflight_failed
printf '%s\n' "$public_host" | awk '
    length($0) < 1 || length($0) > 253 { exit 1 }
    /[^a-z0-9.-]/ || /^\./ || /\.$/ || /\.\./ { exit 1 }
    {
        labels = split($0, label, ".")
        for (label_index = 1; label_index <= labels; label_index++) {
            if (length(label[label_index]) < 1 || length(label[label_index]) > 63 ||
                label[label_index] !~ /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/) {
                exit 1
            }
        }
    }
' || preflight_failed

[ -s "$fullchain" ] || preflight_failed
[ -r "$fullchain" ] || preflight_failed
[ -s "$private_key" ] || preflight_failed
[ "$(stat -c '%u:%g:%a' "$private_key" 2>/dev/null)" = "101:101:400" ] \
    || preflight_failed
command -v openssl >/dev/null 2>&1 || preflight_failed
openssl x509 -checkhost "$public_host" -noout -in "$fullchain" >/dev/null 2>&1 \
    || preflight_failed

printf '%s\n' '{"component":"web_edge","event":"edge_secret_preflight_completed","result":"success"}'
