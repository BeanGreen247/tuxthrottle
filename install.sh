#!/usr/bin/env bash
# TuxThrottle installer: builds the C++ sources and installs them (see cpp/install.sh for the options).
exec "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/cpp/install.sh" "$@"
