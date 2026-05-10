#!/bin/bash

# Default values
PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "-------------------------------------"
echo "User uid:    $(id -u)"
echo "User gid:    $(id -g)"
echo "-------------------------------------"
echo "Setting up user with PUID=$PUID and PGID=$PGID"

# Update group ID
groupmod -o -g "$PGID" appuser

# Update user ID  
usermod -o -u "$PUID" appuser

echo "-------------------------------------"
echo "User uid:    $(id -u appuser)"
echo "User gid:    $(id -g appuser)"
echo "-------------------------------------"

# Create all necessary directories
mkdir -p /app/data /app/logs /app/instance

# Set ownership of all app directories
chown -R appuser:appuser /app

# Get version from app.py
VERSION=$(python3 -c "import sys; sys.path.insert(0, '/app'); from app import __version__; print(__version__)" 2>/dev/null || echo "Unknown")
echo "Starting Newspaparr v$VERSION..."
# Switch to appuser and run the main command
exec gosu appuser "$@"