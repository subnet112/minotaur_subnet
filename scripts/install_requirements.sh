#!/bin/bash

# Requirements installation script for the Minotaur project
# This script requires administrator privileges

echo "🚀 Installing requirements for Minotaur..."
echo "================================================"

# Update system packages
echo "📦 Updating system packages..."
sudo apt update

# Install pip and necessary Python tools
echo "🐍 Installing pip and Python tools..."
sudo apt install -y python3-pip python3-venv python3-dev

# Verify pip installation
echo "✅ Verifying pip..."
python3 -m pip --version

# Create virtual environment
echo "🏗️  Creating virtual environment..."
python3 -m venv venv

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Update pip in virtual environment
echo "⬆️  Updating pip..."
pip install --upgrade pip

# Install requirements
echo "📋 Installing requirements..."
pip install -r requirements.txt

# Verify installation
echo "🔍 Verifying installation..."
echo "================================================"
echo "📊 Installed versions:"
echo ""

# Check each package
packages=("bittensor" "torch" "requests" "pytest" "websocket-client" "fastapi" "uvicorn" "pydantic" "docker")

for package in "${packages[@]}"; do
    echo -n "🔸 $package: "
    pip show $package | grep Version | cut -d' ' -f2 || echo "❌ Not installed"
done

echo ""
echo "✅ Installation complete!"
echo "================================================"
echo "💡 To activate the virtual environment in the future:"
echo "   source venv/bin/activate"
echo ""
echo "💡 To deactivate the virtual environment:"
echo "   deactivate"
