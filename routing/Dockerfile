# Tempo routing: official MOTIS release binary, pinned by version AND SHA-256.
FROM debian:bookworm-slim

ARG MOTIS_VERSION=v2.11.4
ARG MOTIS_SHA256=b521bb29c961a3b994ff4dac663778ba64fee7b4cfaf3f2b20ff1d5e1d15e0dc

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl bzip2 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/motis.tar.bz2 \
      "https://github.com/motis-project/motis/releases/download/${MOTIS_VERSION}/motis-linux-amd64.tar.bz2" \
 && echo "${MOTIS_SHA256}  /tmp/motis.tar.bz2" | sha256sum -c - \
 && mkdir -p /opt/motis \
 && tar -xjf /tmp/motis.tar.bz2 -C /opt/motis \
 && rm /tmp/motis.tar.bz2 \
 && useradd --system --uid 10001 --home-dir /data motis \
 && mkdir -p /data && chown motis /data

USER motis
WORKDIR /data
EXPOSE 8080
ENTRYPOINT ["/opt/motis/motis"]
CMD ["server"]
