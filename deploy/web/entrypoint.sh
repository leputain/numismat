#!/bin/sh
set -eu

configuration_failed() {
    printf '%s\n' '{"component":"web_edge","event":"edge_configuration_completed","result":"error"}' >&2
    exit 1
}

public_url=${MINIAPP_PUBLIC_URL-}
case "$public_url" in
    https://*) public_host=${public_url#https://} ;;
    *) configuration_failed ;;
esac

# The edge has one canonical HTTPS origin. Ports, paths, query strings, user-info,
# uppercase/punycode ambiguity, and malformed DNS labels fail closed without being logged.
[ "$public_url" = "https://$public_host" ] || configuration_failed
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
' || configuration_failed

[ -s /run/secrets/miniapp_tls_fullchain ] || configuration_failed
[ -r /run/secrets/miniapp_tls_fullchain ] || configuration_failed
[ -s /run/secrets/miniapp_tls_private_key ] || configuration_failed
[ -r /run/secrets/miniapp_tls_private_key ] || configuration_failed
[ "$(stat -c '%u:%g:%a' /run/secrets/miniapp_tls_private_key 2>/dev/null)" = "101:101:400" ] \
    || configuration_failed

umask 077
mkdir -p \
    /tmp/nginx/client_body \
    /tmp/nginx/conf.d \
    /tmp/nginx/proxy \
    /tmp/nginx/uwsgi \
    /tmp/nginx/scgi \
    /tmp/nginx/fastcgi

export MINIAPP_PUBLIC_HOST=$public_host
envsubst '${MINIAPP_PUBLIC_HOST}' \
    < /etc/nginx/numismat/site.conf.template \
    > /tmp/nginx/conf.d/site.conf

nginx -t >/dev/null 2>&1 || configuration_failed
printf '%s\n' '{"component":"web_edge","event":"edge_configuration_completed","result":"success"}'
exec nginx -g 'daemon off;'
