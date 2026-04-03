#!/usr/bin/env bash
set -euo pipefail

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Error handler
trap 'printf "${RED}✗ Installation failed${NC}\n" >&2; exit 1' ERR

# Helper function for colored output
print_status() {
    printf "${BLUE}ℹ${NC} %s\n" "$1"
}

print_success() {
    printf "${GREEN}✓${NC} %s\n" "$1"
}

print_error() {
    printf "${RED}✗${NC} %s\n" "$1" >&2
}

print_warning() {
    printf "${YELLOW}⚠${NC} %s\n" "$1"
}

# Main installation script
main() {
    print_status "Starting PenguinCode installation..."
    printf "\n"

    # Check Python version
    print_status "Checking Python version..."
    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is not installed"
        exit 1
    fi

    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

    if [[ $PYTHON_MAJOR -lt 3 ]] || [[ $PYTHON_MAJOR -eq 3 && $PYTHON_MINOR -lt 11 ]]; then
        print_error "Python 3.11 or higher is required (found: $PYTHON_VERSION)"
        exit 1
    fi
    print_success "Python $PYTHON_VERSION detected"
    printf "\n"

    # Get project root directory
    SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
    PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

    # Setup virtual environment
    print_status "Setting up virtual environment..."
    VENV_DIR="$PROJECT_ROOT/venv"

    if [[ ! -d "$VENV_DIR" ]]; then
        python3 -m venv "$VENV_DIR"
        print_success "Virtual environment created at $VENV_DIR"
    else
        print_success "Virtual environment already exists at $VENV_DIR"
    fi

    # Activate virtual environment
    source "$VENV_DIR/bin/activate"
    print_success "Virtual environment activated"
    printf "\n"

    # Check for pip or pipx
    print_status "Checking for pip/pipx..."
    HAS_PIP=false
    HAS_PIPX=false
    INSTALLER=""

    if command -v pipx &> /dev/null; then
        HAS_PIPX=true
        INSTALLER="pipx"
        print_success "pipx found"
    elif command -v pip3 &> /dev/null; then
        HAS_PIP=true
        INSTALLER="pip"
        print_success "pip3 found"
    else
        print_error "Neither pip nor pipx is installed"
        exit 1
    fi
    printf "\n"

    # Check if penguincode is already installed
    print_status "Checking if penguincode is installed..."
    if python3 -c "import penguincode" 2>/dev/null; then
        print_success "penguincode is already installed"
    else
        print_warning "penguincode not found, installing..."
        cd "$PROJECT_ROOT"

        if [[ "$INSTALLER" == "pipx" ]]; then
            print_status "Installing penguincode using pipx..."
            pipx install -e . --force
        else
            print_status "Installing penguincode using pip..."
            pip install -e .
        fi

        print_success "penguincode installed successfully"
    fi
    printf "\n"

    # Run penguincode setup
    print_status "Running penguincode setup..."
    if ! penguincode setup; then
        print_error "Failed to run penguincode setup"
        exit 1
    fi
    print_success "Setup completed successfully"
    printf "\n"

    print_success "PenguinCode installation complete!"
    return 0
}

# Run main function
main "$@"
