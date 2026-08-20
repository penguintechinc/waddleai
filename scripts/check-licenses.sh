#!/bin/bash

# pip-licenses OSI gate: fail if any third-party dependency carries forbidden/non-OSI license
# Excludes first-party packages (names starting with waddleai or penguin, or in LICENSE_ALLOW_PACKAGES)

# Check if pip-licenses is available, install if needed
if ! command -v pip-licenses >/dev/null 2>&1; then
    echo "Installing pip-licenses..."
    python3 -m pip install pip-licenses --break-system-packages >/dev/null 2>&1 || python3 -m pip install pip-licenses >/dev/null 2>&1 || { echo "Failed to install pip-licenses" >&2; exit 1; }
    if ! command -v pip-licenses >/dev/null 2>&1; then
        echo "Failed to install pip-licenses" >&2
        exit 1
    fi
fi

# Get the licenses JSON into a temp file
TEMP_JSON=$(mktemp)
trap 'rm -f "$TEMP_JSON"' EXIT

pip-licenses --format=json --with-system 2>/dev/null > "$TEMP_JSON"
if [ $? -ne 0 ]; then
    echo "Failed to run pip-licenses" >&2
    exit 1
fi

# Parse with python3
python3 <<PYTHON_EOF
import json
import sys
import os

# Read the JSON file
try:
    with open('$TEMP_JSON', 'r') as f:
        data = json.load(f)
except (IOError, json.JSONDecodeError) as e:
    print("Error reading or parsing pip-licenses JSON: " + str(e), file=sys.stderr)
    sys.exit(1)

# Forbidden license patterns (case-insensitive substring match)
forbidden = [
    "agpl",
    "sspl",
    "server side public license",
    "business source",
    "busl",
    "elastic license",
    "redis source available",
    "rsal",
    "commons clause",
    "cc-by-nc",
    "creative commons attribution-noncommercial"
]

# First-party prefixes to exclude
first_party_prefixes = ["waddleai", "penguin"]

# Allow packages from env var
allow_packages = os.environ.get("LICENSE_ALLOW_PACKAGES", "").split(",")
allow_packages = [p.strip().lower() for p in allow_packages if p.strip()]

offenders = []

for package in data:
    name = package.get("Name", "")
    version = package.get("Version", "")
    license_text = package.get("License", "")

    # Normalize name: lowercase and replace - with _
    normalized_name = name.lower().replace("-", "_")

    # Skip if first-party (starts with waddleai or penguin)
    is_first_party = False
    for prefix in first_party_prefixes:
        if normalized_name.startswith(prefix):
            is_first_party = True
            break

    # Skip if in allow list
    if normalized_name in allow_packages:
        is_first_party = True

    if is_first_party:
        continue

    # Check for forbidden licenses (case-insensitive substring match)
    license_lower = license_text.lower()
    for forbidden_pattern in forbidden:
        if forbidden_pattern in license_lower:
            offenders.append((name, version, license_text))
            break

# Report results
if offenders:
    for name, version, license_text in offenders:
        print("{0}  {1}  {2}".format(name, version, license_text))
    sys.exit(1)
else:
    print("licenses clean")
    sys.exit(0)
PYTHON_EOF

exit $?
