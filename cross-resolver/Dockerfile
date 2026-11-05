# The cross resolver — dnsmasq on the cross network (GRILLING G83).
#
# It answers ONLY the service-domain wildcards the gateway writes
# (`*.{domain} -> cross nginx`). There is no upstream: every out-of-zone name is
# REFUSED, so the client falls through to the second nameserver (the Docker
# embedded DNS) — never an NXDOMAIN, which would stop the fall-through.
FROM alpine:3.19

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}

RUN apk add --no-cache dnsmasq

# The empty policy used before the gateway's first write.
RUN mkdir -p /etc/dnsmasq.d \
 && printf 'no-resolv\nno-hosts\n' > /etc/dnsmasq-default.conf

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 53/udp 53/tcp
ENTRYPOINT ["/entrypoint.sh"]
