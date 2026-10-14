# Recipe only: no default base, package installation, or runtime downloads.
ARG TORCH_CUDA_BASE
FROM --platform=linux/amd64 ${TORCH_CUDA_BASE}
ARG TORCH_CUDA_BASE
RUN python -c 'import os,re,platform,torch; assert re.fullmatch(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}", os.environ["TORCH_CUDA_BASE"]); assert platform.machine() == "x86_64"; assert torch.version.cuda is not None'
COPY run /tessera/bin/run
COPY smoke.py /tessera/lib/smoke.py
RUN chmod 0555 /tessera/bin/run /tessera/lib/smoke.py
USER 65532:65532
ENTRYPOINT ["/tessera/bin/run"]
