# v5 edge -nginx-acl image.
#
# The ONLY externally-exposed nginx in fullset (decision 1/6). TLS termination
# via SNI→/certs map (read-only mount, decision 9/11), portal routing, and the
# L7 ACL gate (auth_request → gateway verify, F13).
#
# Non-root (decision 17): UID nginx runs nginx and binds ports 80/443 via the
# default NET_BIND_SERVICE docker capability. The startup placeholder cert is
# baked into the IMAGE at /etc/nginx/acl/placeholder/ (decision 11) so it
# survives the read-only /certs mount (bind-mounting a whole dir would hide the
# image content — the compose file therefore mounts individual files, not dirs).

FROM nginx:alpine

# Placeholder cert (decision 11): nginx requires a loadable cert at startup even
# though every request-time SNI resolves through the dynamic map. The placeholder
# is only ever served to satisfy startup — a real handshake for a domain without
# a /certs entry FAILS (certless-https behavior, decision 9).
RUN apk add --no-cache openssl \
 && mkdir -p /etc/nginx/acl/placeholder \
 && openssl req -x509 -nodes -newkey rsa:2048 -days 36500 \
      -keyout /etc/nginx/acl/placeholder/privkey.pem \
      -out /etc/nginx/acl/placeholder/fullchain.pem \
      -subj "/CN=subnet-acl-placeholder" \
 && rm /etc/nginx/conf.d/default.conf

# Templates + assembler.
COPY edge.conf.template     /etc/nginx/acl/edge.conf.template
COPY acl-helpers.conf.template /etc/nginx/acl/acl-helpers.conf.template
COPY entrypoint.sh          /etc/nginx/acl/entrypoint.sh
RUN chmod +x /etc/nginx/acl/entrypoint.sh

# Non-root (decision 17): nginx needs to write nginx.conf (assembled by the
# entrypoint), /var/run/nginx.pid, and the log/cache dirs.
RUN chown -R nginx:nginx /etc/nginx \
 && mkdir -p /var/cache/nginx /var/log/nginx \
 && chown -R nginx:nginx /var/cache/nginx /var/log/nginx \
 && touch /var/run/nginx.pid \
 && chown nginx:nginx /var/run/nginx.pid

# No ENTRYPOINT here — docker-compose.gateway.yml drives the command
# (entrypoint.sh) so it can also be run with START_NGINX=0 for tests.
USER nginx
