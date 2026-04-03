#!/bin/bash
#
# Ollama Assistant VS Code Extension - Install Script
# This script installs dependencies, builds the extension, and optionally installs it
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║         Ollama Assistant VS Code Extension Installer       ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo

# Check for required tools
check_requirements() {
    echo -e "${YELLOW}Checking requirements...${NC}"

    # Check Node.js
    if ! command -v node &> /dev/null; then
        echo -e "${RED}Error: Node.js is not installed.${NC}"
        echo "Please install Node.js 18+ from https://nodejs.org"
        exit 1
    fi

    NODE_VERSION=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
    if [ "$NODE_VERSION" -lt 18 ]; then
        echo -e "${RED}Error: Node.js 18+ is required. Current version: $(node -v)${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} Node.js $(node -v)"

    # Check npm
    if ! command -v npm &> /dev/null; then
        echo -e "${RED}Error: npm is not installed.${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} npm $(npm -v)"

    # Check VS Code CLI (optional)
    if command -v code &> /dev/null; then
        echo -e "  ${GREEN}✓${NC} VS Code CLI available"
        VSCODE_CLI=true
    else
        echo -e "  ${YELLOW}!${NC} VS Code CLI not found (optional)"
        VSCODE_CLI=false
    fi

    # Check Ollama (optional)
    if command -v ollama &> /dev/null; then
        echo -e "  ${GREEN}✓${NC} Ollama installed"

        # Check if Ollama is running
        if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo -e "  ${GREEN}✓${NC} Ollama is running"
        else
            echo -e "  ${YELLOW}!${NC} Ollama is installed but not running"
            echo -e "      Run: ${BLUE}ollama serve${NC}"
        fi
    else
        echo -e "  ${YELLOW}!${NC} Ollama not installed"
        echo -e "      Install from: ${BLUE}https://ollama.ai${NC}"
    fi

    echo
}

# Install dependencies
install_dependencies() {
    echo -e "${YELLOW}Installing dependencies...${NC}"
    cd "$PROJECT_DIR"
    npm install
    echo -e "  ${GREEN}✓${NC} Dependencies installed"
    echo
}

# Build the extension
build_extension() {
    echo -e "${YELLOW}Building extension...${NC}"
    cd "$PROJECT_DIR"
    npm run compile
    echo -e "  ${GREEN}✓${NC} Extension built successfully"
    echo
}

# Package the extension
package_extension() {
    echo -e "${YELLOW}Packaging extension...${NC}"
    cd "$PROJECT_DIR"

    # Install vsce if not present
    if ! command -v vsce &> /dev/null; then
        echo -e "  Installing vsce..."
        npm install -g @vscode/vsce
    fi

    vsce package --out ollama-assistant.vsix
    echo -e "  ${GREEN}✓${NC} Extension packaged: ollama-assistant.vsix"
    echo
}

# Install the extension in VS Code
install_extension() {
    if [ "$VSCODE_CLI" = true ]; then
        echo -e "${YELLOW}Installing extension in VS Code...${NC}"
        cd "$PROJECT_DIR"
        code --install-extension ollama-assistant.vsix
        echo -e "  ${GREEN}✓${NC} Extension installed in VS Code"
        echo
    else
        echo -e "${YELLOW}VS Code CLI not available. Manual installation required:${NC}"
        echo -e "  1. Open VS Code"
        echo -e "  2. Go to Extensions (Ctrl+Shift+X)"
        echo -e "  3. Click '...' menu > 'Install from VSIX...'"
        echo -e "  4. Select: ${PROJECT_DIR}/ollama-assistant.vsix"
        echo
    fi
}

# Setup recommended Ollama models
setup_models() {
    if command -v ollama &> /dev/null; then
        echo -e "${YELLOW}Setting up recommended models...${NC}"
        echo -e "The following models are recommended for this extension:"
        echo -e "  - ${BLUE}codellama${NC} (7B) - Code completion and generation"
        echo -e "  - ${BLUE}llama3.1${NC} (8B) - Chat and explanations"
        echo

        read -p "Would you like to pull these models now? [y/N] " -n 1 -r
        echo

        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo -e "Pulling codellama..."
            ollama pull codellama
            echo -e "Pulling llama3.1..."
            ollama pull llama3.1
            echo -e "  ${GREEN}✓${NC} Models downloaded"
        else
            echo -e "  Skipped model download"
        fi
        echo
    fi
}

# Development mode setup
dev_setup() {
    echo -e "${YELLOW}Setting up development environment...${NC}"
    cd "$PROJECT_DIR"

    # Install dev dependencies
    npm install

    echo -e "  ${GREEN}✓${NC} Development environment ready"
    echo
    echo -e "To start development:"
    echo -e "  1. Open the project in VS Code: ${BLUE}code ${PROJECT_DIR}${NC}"
    echo -e "  2. Press ${BLUE}F5${NC} to launch the extension in debug mode"
    echo -e "  3. Or run: ${BLUE}npm run watch${NC} for continuous compilation"
    echo
}

# Print usage
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo
    echo "Options:"
    echo "  --install, -i     Full installation (dependencies + build + package + install)"
    echo "  --build, -b       Build the extension only"
    echo "  --package, -p     Package the extension as .vsix"
    echo "  --dev, -d         Setup for development"
    echo "  --models, -m      Setup recommended Ollama models"
    echo "  --help, -h        Show this help message"
    echo
    echo "Examples:"
    echo "  $0 --install      # Full installation"
    echo "  $0 --dev          # Setup for development"
    echo "  $0 --build        # Just build the extension"
    echo
}

# Main
main() {
    if [ $# -eq 0 ]; then
        usage
        exit 0
    fi

    case "$1" in
        --install|-i)
            check_requirements
            install_dependencies
            build_extension
            package_extension
            install_extension
            setup_models
            echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
            echo -e "${GREEN}║              Installation Complete!                        ║${NC}"
            echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
            echo
            echo -e "Next steps:"
            echo -e "  1. Restart VS Code"
            echo -e "  2. Look for the Ollama icon in the activity bar"
            echo -e "  3. Make sure Ollama is running: ${BLUE}ollama serve${NC}"
            echo
            ;;
        --build|-b)
            check_requirements
            install_dependencies
            build_extension
            echo -e "${GREEN}Build complete!${NC}"
            ;;
        --package|-p)
            check_requirements
            install_dependencies
            build_extension
            package_extension
            echo -e "${GREEN}Package created: ollama-assistant.vsix${NC}"
            ;;
        --dev|-d)
            check_requirements
            dev_setup
            ;;
        --models|-m)
            setup_models
            ;;
        --help|-h)
            usage
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            exit 1
            ;;
    esac
}

main "$@"
