#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"
source venv/bin/activate
setterm --cursor off > /dev/tty1
./venv/bin/python3 wifi_setup.py >> /dev/null 2>&1
./venv/bin/python3 display.py >> /dev/null 2>&1 &
./venv/bin/python3 api.py >> /dev/null 2>&1 &
./venv/bin/python3 network_server.py >> /dev/null 2>&1 &
./venv/bin/python3 terminal_server.py >> /dev/null 2>&1 &
./venv/bin/python3 manager.py >> /dev/null 2>&1 &
wait
