#!/bin/bash

# Build and Deploy Script for the DQ Chatbot (Gradio UI is the only deployable service).
# Usage: ./build_and_deploy.sh <environment> <aws-region> [image-tag]

set -euo pipefail

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/build_gradio_ui.sh" "$@"
