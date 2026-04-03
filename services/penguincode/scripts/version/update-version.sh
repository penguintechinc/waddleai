#!/bin/bash
# Version update script for Penguin Code
# Format: vMajor.Minor.Patch.epoch64

set -e

VERSION_FILE=".version"

# Read current version
if [ ! -f "$VERSION_FILE" ]; then
    echo "Error: .version file not found"
    exit 1
fi

CURRENT_VERSION=$(cat "$VERSION_FILE")
echo "Current version: $CURRENT_VERSION"

# Parse version components
VERSION_WITHOUT_V=${CURRENT_VERSION#v}
MAJOR=$(echo "$VERSION_WITHOUT_V" | cut -d. -f1)
MINOR=$(echo "$VERSION_WITHOUT_V" | cut -d. -f2)
PATCH=$(echo "$VERSION_WITHOUT_V" | cut -d. -f3)
OLD_EPOCH=$(echo "$VERSION_WITHOUT_V" | cut -d. -f4)

# Get new epoch timestamp
NEW_EPOCH=$(date +%s)

# Determine what to update
case "${1:-build}" in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        echo "Incrementing MAJOR version"
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        echo "Incrementing MINOR version"
        ;;
    patch)
        PATCH=$((PATCH + 1))
        echo "Incrementing PATCH version"
        ;;
    build|*)
        echo "Updating BUILD timestamp only"
        ;;
esac

# Construct new version
NEW_VERSION="v${MAJOR}.${MINOR}.${PATCH}.${NEW_EPOCH}"

# Update .version file
echo "$NEW_VERSION" > "$VERSION_FILE"

echo "Updated version: $NEW_VERSION"
echo ""
echo "Changes:"
echo "  Major: $MAJOR"
echo "  Minor: $MINOR"
echo "  Patch: $PATCH"
echo "  Build: $NEW_EPOCH ($(date -d @$NEW_EPOCH 2>/dev/null || date -r $NEW_EPOCH))"
echo ""
echo "Version file updated: $VERSION_FILE"
