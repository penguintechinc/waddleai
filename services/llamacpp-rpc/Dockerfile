# llama.cpp with RPC for multi-node serving (Plan F, F4): llama-server (head) and rpc-server
# (worker) in ONE image per GPU vendor, both built from the SAME commit (LLAMACPP_COMMIT).
# Targets: runtime-cuda | runtime-rocm | runtime-sycl.  amd64 only (owner directive).
# Bases are digest-pinned; the build asserts the checked-out commit equals LLAMACPP_COMMIT.
ARG LLAMACPP_COMMIT

FROM nvidia/cuda:12.6.3-devel-ubuntu24.04@sha256:392c0df7b577ecae17a17f6ba7f2009c217bb4422f8431c053ae9af61a8c148a AS build-cuda
ARG LLAMACPP_COMMIT
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init . && git remote add origin https://github.com/ggml-org/llama.cpp.git \
    && git fetch --depth 1 origin "${LLAMACPP_COMMIT}" && git checkout FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${LLAMACPP_COMMIT}"
RUN cmake -B build -DGGML_RPC=ON -DLLAMA_OPENSSL=ON -DLLAMA_CURL=OFF -DGGML_NATIVE=OFF \
      -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="75;80;86;89;90" \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
    && cmake --build build --config Release -j "$(nproc)" --target llama-server rpc-server
RUN mkdir -p /out/bin /out/lib && cp build/bin/llama-server build/bin/rpc-server /out/bin/ \
    && find build -name '*.so*' -exec cp -P {} /out/lib/ \;

FROM rocm/dev-ubuntu-24.04:6.4.1-complete@sha256:40eb8498dbfd1770da15b811d5fcfdf0341531dd71f87fea92ea6ae6596bafa3 AS build-rocm
ARG LLAMACPP_COMMIT
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init . && git remote add origin https://github.com/ggml-org/llama.cpp.git \
    && git fetch --depth 1 origin "${LLAMACPP_COMMIT}" && git checkout FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${LLAMACPP_COMMIT}"
RUN HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" cmake -B build \
      -DGGML_RPC=ON -DLLAMA_OPENSSL=ON -DLLAMA_CURL=OFF -DGGML_NATIVE=OFF -DGGML_HIP=ON \
      -DAMDGPU_TARGETS="gfx906;gfx908;gfx90a;gfx942;gfx1030;gfx1100;gfx1101;gfx1200;gfx1201" \
      -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --config Release -j "$(nproc)" --target llama-server rpc-server
RUN mkdir -p /out/bin /out/lib && cp build/bin/llama-server build/bin/rpc-server /out/bin/ \
    && find build -name '*.so*' -exec cp -P {} /out/lib/ \;

FROM intel/oneapi-basekit:2025.1.1-0-devel-ubuntu24.04@sha256:81458504ff677db5d880b9893063faef4cb1ddde6d0d06bc1c646980c35600ef AS build-sycl
ARG LLAMACPP_COMMIT
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init . && git remote add origin https://github.com/ggml-org/llama.cpp.git \
    && git fetch --depth 1 origin "${LLAMACPP_COMMIT}" && git checkout FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${LLAMACPP_COMMIT}"
RUN . /opt/intel/oneapi/setvars.sh && cmake -B build \
      -DGGML_RPC=ON -DLLAMA_OPENSSL=ON -DLLAMA_CURL=OFF -DGGML_NATIVE=OFF -DGGML_SYCL=ON \
      -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --config Release -j "$(nproc)" --target llama-server rpc-server
RUN mkdir -p /out/bin /out/lib && cp build/bin/llama-server build/bin/rpc-server /out/bin/ \
    && find build -name '*.so*' -exec cp -P {} /out/lib/ \;

FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04@sha256:92906d87596d638d35015c6353053121bd299d25943b875763321653884ba924 AS runtime-cuda
ARG LLAMACPP_COMMIT
LABEL org.opencontainers.image.revision="${LLAMACPP_COMMIT}"
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build-cuda /out/bin/ /usr/local/bin/
COPY --from=build-cuda /out/lib/ /usr/local/lib/
RUN ldconfig
ENV LD_LIBRARY_PATH=/usr/local/lib
USER 1000:1000

FROM rocm/dev-ubuntu-24.04:6.4.1@sha256:53e07952f5117dd2afc75938978f8ed4947c83a0359f8dfda40f55dc008622a5 AS runtime-rocm
ARG LLAMACPP_COMMIT
LABEL org.opencontainers.image.revision="${LLAMACPP_COMMIT}"
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build-rocm /out/bin/ /usr/local/bin/
COPY --from=build-rocm /out/lib/ /usr/local/lib/
RUN ldconfig
ENV LD_LIBRARY_PATH=/usr/local/lib
USER 1000:1000

FROM intel/oneapi-runtime:2025.1.1-0-devel-ubuntu24.04@sha256:d29dd4b1d26d06d9567034c86c3d55bde3557de11cf42f4f7f0d0d81a9d039bf AS runtime-sycl
ARG LLAMACPP_COMMIT
LABEL org.opencontainers.image.revision="${LLAMACPP_COMMIT}"
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build-sycl /out/bin/ /usr/local/bin/
COPY --from=build-sycl /out/lib/ /usr/local/lib/
RUN ldconfig
ENV LD_LIBRARY_PATH=/usr/local/lib
USER 1000:1000
