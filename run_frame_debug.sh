#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"
source venv/bin/activate
setterm --cursor off > /dev/tty1
./venv/bin/python3 wifi_setup.py >> logs/network.log 2>&1
./venv/bin/python3 display.py >> logs/display.log 2>&1 &
./venv/bin/python3 api.py >> logs/api.log 2>&1 &
./venv/bin/python3 network_server.py >> logs/network.log 2>&1 &
./venv/bin/python3 terminal_server.py >> logs/terminal.log 2>&1 &
./venv/bin/python3 manager.py >> logs/manager.log 2>&1 &
wait
