#!/bin/zsh
cd "$(dirname "$0")"
mkdir -p state
nohup .venv/bin/python courtbot_gui.py >> state/gui.log 2>&1 &
