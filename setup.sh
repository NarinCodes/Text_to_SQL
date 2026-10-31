#!/bin/bash

set -e

echo "Setting up the Text-to-SQL CLI agent..."
echo ""

# Check prerequisites
for cmd in uv curl sqlite3; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "Error: $cmd is not installed."
        if [ "$cmd" = "uv" ]; then
            echo "Please install uv first: https://github.com/astral-sh/uv"
            echo "Quick install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        fi
        exit 1
    fi
done

# Download the Chinook database
echo "Downloading Chinook database..."
mkdir -p data

if [ -f "data/Chinook.db" ]; then
    echo "Removing existing data/Chinook.db..."
    rm data/Chinook.db
fi

curl -s https://raw.githubusercontent.com/lerocha/chinook-database/master/ChinookDatabase/DataSources/Chinook_Sqlite.sql | sqlite3 data/Chinook.db

if [ -f "data/Chinook.db" ]; then
    echo "Successfully created data/Chinook.db"
else
    echo "Error: Failed to create database"
    exit 1
fi

echo ""
echo "Setup complete!"
echo ""
echo "To get started:"
echo "  1. Install dependencies: uv sync"
echo "  2. Set your FIREWORKS_API_KEY environment variable"
echo "  3. Run the CLI: uv run cli"
echo ""
