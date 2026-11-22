# Recipe only. The base is an owner-supplied digest-pinned CUDA runtime image
# (no default). Only ca-certificates, python3 and pip are added from an apt
# snapshot pinned by an owner-supplied timestamp: no ML framework, model or
# experiment code. Everything else is fetched at run time under a reviewed,
# hash-pinned manifest (see README.md).
ARG GPU_RUNTIME_BASE
ARG UBUNTU_SNAPSHOT
FROM --platform=linux/amd64 ${GPU_RUNTIME_BASE}
ARG GPU_RUNTIME_BASE
ARG UBUNTU_SNAPSHOT
RUN printf '%s' "$GPU_RUNTIME_BASE" | grep -Eq '^[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}$' \
 && printf '%s' "$UBUNTU_SNAPSHOT" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$' \
 && test "$(uname -m)" = x86_64 \
 && rm -f /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/nvidia*.list \
 && for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do \
      if [ -f "$f" ]; then \
        sed -i -E "s#https?://(archive|security)\.ubuntu\.com/ubuntu/?#http://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/#g" "$f"; \
      fi; \
    done \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-pip \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache \
 && mkdir -p /tessera/bin /tessera/lib /tessera/work \
 && chown 65532:65532 /tessera/work
COPY run /tessera/bin/run
COPY runtime.py /tessera/lib/runtime.py
RUN chmod 0555 /tessera/bin/run /tessera/lib/runtime.py
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
USER 65532:65532
ENTRYPOINT ["/tessera/bin/run"]
