#!/bin/bash
# install-sccache-dist.sh - Build and install sccache-dist scheduler from source
# This script clones the sccache repository and builds the sccache-dist binary.
# Installs to user's cargo bin directory (~/.cargo/bin) - no sudo required.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Install to user's cargo bin directory
INSTALL_DIR="${INSTALL_DIR:-$HOME/.cargo/bin}"
BUILD_DIR="${BUILD_DIR:-/tmp/sccache-build}"
SCCACHE_REPO="${SCCACHE_REPO:-https://github.com/mozilla/sccache.git}"
# Use main branch for latest compatible code
SCCACHE_BRANCH="${SCCACHE_BRANCH:-main}"

echo "========================================"
echo "sccache-dist Installer (User Mode)"
echo "========================================"
echo ""
echo "This script will:"
echo "  1. Update Rust toolchain to latest stable"
echo "  2. Install build dependencies (if missing)"
echo "  3. Clone sccache repository"
echo "  4. Build sccache-dist binary"
echo "  5. Install to ${INSTALL_DIR}"
echo ""
echo "No sudo required - installs to user's cargo bin."
echo ""

# Source cargo env
if [ -f "$HOME/.cargo/env" ]; then
    source "$HOME/.cargo/env"
fi

# Ensure cargo is in PATH
export PATH="$HOME/.cargo/bin:$PATH"

# Step 1: Update Rust toolchain
echo "[1/4] Updating Rust toolchain..."
RUST_VERSION=$(rustc --version 2>/dev/null || echo "not installed")
echo "      Current: ${RUST_VERSION}"
echo "      Updating to latest stable for edition2024 support..."
rustup update stable
rustup default stable

# Verify Rust version
RUST_VERSION=$(rustc --version)
CARGO_VERSION=$(cargo --version)
echo "      Using ${RUST_VERSION} and ${CARGO_VERSION}"

# Step 2: Install build dependencies
echo ""
echo "[2/4] Installing build dependencies..."
echo "      Checking for required packages..."

REQUIRED_PACKAGES=""
if ! command -v git &> /dev/null; then
    REQUIRED_PACKAGES="${REQUIRED_PACKAGES} git"
fi
if ! command -v pkg-config &> /dev/null; then
    REQUIRED_PACKAGES="${REQUIRED_PACKAGES} pkg-config"
fi
if ! dpkg -l 2>/dev/null | grep -q libssl-dev; then
    REQUIRED_PACKAGES="${REQUIRED_PACKAGES} libssl-dev"
fi
if ! command -v cmake &> /dev/null; then
    REQUIRED_PACKAGES="${REQUIRED_PACKAGES} cmake"
fi
if ! command -v protoc &> /dev/null; then
    REQUIRED_PACKAGES="${REQUIRED_PACKAGES} protobuf-compiler"
fi

if [ -n "${REQUIRED_PACKAGES}" ]; then
    echo "      Installing: ${REQUIRED_PACKAGES}"
    echo "      (requires sudo for system packages)"
    sudo apt-get update -qq
    sudo apt-get install -y -qq ${REQUIRED_PACKAGES}
else
    echo "      All required packages already installed."
fi

# Step 3: Clone sccache repository
echo ""
echo "[3/4] Cloning sccache repository..."
if [ -d "${BUILD_DIR}" ]; then
    echo "      Removing existing build directory..."
    rm -rf "${BUILD_DIR}"
fi

git clone --depth 1 --branch ${SCCACHE_BRANCH} ${SCCACHE_REPO} "${BUILD_DIR}"
cd "${BUILD_DIR}"
echo "      Cloned ${SCCACHE_BRANCH}"

# Step 4: Build sccache-dist binary
echo ""
echo "[4/4] Building sccache-dist binary..."
echo "      This may take 5-10 minutes..."

# Build with dist-server feature
cargo build --release --bin sccache-dist --features dist-server

# Check build result
if [ -f "target/release/sccache-dist" ]; then
    echo "      Build successful!"
    ls -lh target/release/sccache-dist
else
    echo "      Build failed!"
    exit 1
fi

# Install binary to user's cargo bin
echo ""
echo "Installing sccache-dist to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp target/release/sccache-dist "${INSTALL_DIR}/"
chmod +x "${INSTALL_DIR}/sccache-dist"

# Verify installation
echo ""
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo ""
echo "Verifying installation..."
"${INSTALL_DIR}/sccache-dist" --help 2>&1 | head -5
echo ""
echo "sccache-dist binary installed to: ${INSTALL_DIR}/sccache-dist"
echo ""
echo "To start the scheduler:"
echo "  cd /home/mrwilson/software/gooey"
echo "  ./do sccache-dist start"
echo ""
echo "To check status:"
echo "  ./do sccache-dist status"
echo ""
echo "To view logs:"
echo "  ./do sccache-dist logs"
echo ""
