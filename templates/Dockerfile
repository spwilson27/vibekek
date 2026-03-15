# Weaver Reloaded Dockerfile Template
# 
# Build with: docker build -t <image-name>:<tag> .
# 
# The USERNAME and USER_UID build arguments allow customizing the non-root user.
# AI CLI tools (Claude, Gemini, Qwen) require a non-root user to run with
# --dangerously-skip-permissions for security reasons.
#
# Example:
#   docker build --build-arg USERNAME=myuser --build-arg USER_UID=1000 -t myimage:dev .

FROM ubuntu:24.04

# Build arguments for non-root user configuration
ARG USERNAME=username
ARG USER_UID=1000

# ─── System packages ─────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core build tools
    build-essential \
    pkg-config \
    git \
    curl \
    ca-certificates \
    gnupg \
    # Python
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    # wgpu dependencies (Vulkan, Wayland)
    libvulkan-dev \
    libxkbcommon-dev \
    libwayland-dev \
    # winit dependencies (X11, XCB)
    libxcb-render0-dev \
    libxcb-shape0-dev \
    libxcb-xfixes0-dev \
    libx11-dev \
    # Audio development libraries (ALSA, JACK, PipeWire)
    libasound2-dev \
    libjack-jackd2-dev \
    libpipewire-0.3-dev \
    # SQLite development libraries
    libsqlite3-dev \
    # Misc utilities used by workflow scripts
    sudo \
    jq \
    unzip \
  && rm -rf /var/lib/apt/lists/*

# ─── Node.js (LTS via NodeSource) ────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# ─── Cap'n Proto compiler ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends capnproto \
  && rm -rf /var/lib/apt/lists/*

# ─── AI CLIs (installed globally as root so all users can invoke them) ───────
RUN npm install -g \
    @anthropic-ai/claude-code \
    @google/gemini-cli \
    @qwen-code/qwen-code

# ─── Playwright and browser dependencies for E2E testing ─────────────────────
RUN npx playwright install --with-deps chromium

# ─── Rust (pinned to 1.82.0 as per project spec) ─────────────────────────────
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --default-toolchain 1.82.0 \
  && chmod -R a+w /usr/local/rustup /usr/local/cargo

# ─── WASM Target and wasm-pack ───────────────────────────────────────────────
RUN rustup target add wasm32-unknown-unknown \
  && curl https://rustwasm.github.io/wasm-pack/installer/init.sh -sSf | sh

# ─── wasm-opt (Binaryen v119) ────────────────────────────────────────────────
RUN curl -L https://github.com/WebAssembly/binaryen/releases/download/version_119/binaryen-version_119-x86_64-linux.tar.gz \
    | tar xz -C /usr/local --strip-components=1 \
  && chmod a+x /usr/local/bin/wasm-opt

# ─── Rust testing and dev tools (versions compatible with Rust 1.82.0) ───────
RUN cargo install --locked cargo-llvm-cov --version 0.6.21 \
  && cargo install --locked cargo-nextest --version 0.9.97-b.2 \
  && cargo install --locked cargo-deny --version 0.17.0 \
  && cargo install --locked cargo-watch \
  && echo "cargo-public-api skipped (requires newer Rust)"

# ─── Linting tools (shellcheck, markdownlint) ────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends shellcheck \
  && npm install -g markdownlint-cli \
  && rm -rf /var/lib/apt/lists/*

# ─── Python deps ─────────────────────────────────────────────────────────────
COPY .tools/requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages -r /tmp/requirements.txt \
  && rm /tmp/requirements.txt

# ─── Non-root user for AI CLI tools ──────────────────────────────────────────
# AI CLIs (Claude, Gemini, Qwen) refuse --dangerously-skip-permissions when
# running as root for security reasons. Create a non-root user so the workflow
# can run agents inside Docker containers.
RUN useradd -m -s /bin/bash -u ${USER_UID} ${USERNAME} \
  && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
  && mkdir -p /home/${USERNAME}/.claude /home/${USERNAME}/.gemini /home/${USERNAME}/.qwen \
  && chown -R ${USERNAME}:${USERNAME} /home/${USERNAME}

# ─── Working directory ───────────────────────────────────────────────────────
WORKDIR /workspace
RUN chown ${USERNAME}:${USERNAME} /workspace

# Switch to non-root user for all subsequent operations
USER ${USERNAME}

# Default: drop to bash so the container is usable interactively and as a
# one-shot command runner (docker run --rm -i ... image <cmd>).
CMD ["bash"]
