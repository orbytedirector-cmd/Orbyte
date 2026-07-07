#!/bin/bash

# Music Browser Application
# Flask-based HiRes music browser

cd "$(dirname "$0")"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
pip install -q flask flask-cors

# Start the application
echo "Starting HiRes Browser..."
python app.py