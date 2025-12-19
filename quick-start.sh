#!/usr/bin/env bash
set -euo pipefail

echo "🚀 Minotaur Validator - Quick Start"
echo ""

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "✅ Installation complete!"
echo ""
echo "Configuration required:"
echo "  export VALIDATOR_STORAGE_TOKEN=<your-secret-token>"
echo "  export NETUID=<subnet-uid>"
echo "  export WALLET_NAME=<wallet-name>"
echo "  export WALLET_HOTKEY=<hotkey-name>"
echo ""
echo "Start validator:"
echo "  python -m neurons.validator \\"
echo "    --wallet.name \$WALLET_NAME \\"
echo "    --wallet.hotkey \$WALLET_HOTKEY \\"
echo "    --netuid \$NETUID \\"
echo "    --subtensor.network finney"
echo ""
echo "Or run tests:"
echo "  pytest tests/ -v"
echo ""

